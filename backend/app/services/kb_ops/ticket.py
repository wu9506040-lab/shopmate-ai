"""知识运营闭环第 3 步：把归因结论变成「可执行的工单」。

为什么不是「都生成一张补卡草稿」
------------------------------
diagnose 把 retrieval 一档拆成三类，正是因为三类的修法完全不同：

    no_kb          库里真没有          → 补一张新卡（add_card）
    retrieval_miss 有，但 top-k 没搜到  → **补卡是错的**，要改现有卡的检索锚点
    ranking_weak   搜到了但排太后      → 同上，且优先级更低
    kb_stale       搜到了但内容冲突     → 改卡（update_card）

如果三种都出「新卡草稿」，运营会照着清单把已经存在的政策再写一遍：
库里出现两张「双 11 活动规则」，检索时互相挤占、引用编号错乱——
这是知识库「越运营越乱」最典型的路径。所以工单类型必须跟着根因走。

工单里必须带「验证问句」
----------------------
工单能关闭的条件不是「人点了完成」，而是「这批问句重新检索后能进 top-n」。
这是回归绑定的落点：同一批问句在补卡前后各跑一次，rank 变化可复核、可追溯。
人手工点「已完成」的闭环是假的——没人知道补完到底有没有用。

为什么草稿不直接写进 docs/
------------------------
客服知识是政策类，编错条款会造成资损和投诉。草稿正文里凡是「结论」位置一律留 TODO，
由人按官方口径填；机器只负责三件它不会错的事：定位（哪条知识）、取证（哪些问句栽在这）、
排版（front matter 和章节骨架）。apply 时再落盘，且必须显式执行。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.kb_ops.diagnose import (
    ROOT_KB_EXPIRED,
    ROOT_KB_STALE,
    ROOT_NO_KB,
    ROOT_RANKING_WEAK,
    ROOT_RETRIEVAL_MISS,
    diagnose_one,
    doc_stem,
)

# 工单类型：决定 apply 时是"新建文件"还是"往现有文件里追加一段"
KIND_ADD = "add_card"
KIND_PATCH = "fix_retrieval"
KIND_UPDATE = "update_card"

KIND_LABELS = {
    KIND_ADD: "补新卡",
    KIND_PATCH: "改检索锚点（不要补新卡）",
    KIND_UPDATE: "改卡（内容冲突）",
}

# 只有知识侧的根因能开知识工单。
# generation 该改 prompt / context_missing 该查订单链路 / unknown 该先标注，
# 都不是"改知识库"能解决的——给它们开工单只会让运营做无用功。
CAUSE_TO_KIND = {
    ROOT_NO_KB: KIND_ADD,
    ROOT_RETRIEVAL_MISS: KIND_PATCH,
    ROOT_RANKING_WEAK: KIND_PATCH,
    ROOT_KB_STALE: KIND_UPDATE,
    # 过期卡补的是**新版本**（新文件、新有效期），不是给旧卡补锚点——
    # 旧卡再怎么写也过不了 _drop_inactive 的过滤。
    ROOT_KB_EXPIRED: KIND_ADD,
}

STATUS_OPEN = "open"        # 已开单，还没草稿
STATUS_DRAFTED = "drafted"  # 草稿已生成，等人改
STATUS_APPLIED = "applied"  # 草稿已落盘入库，待验证
STATUS_CLOSED = "closed"    # 验证通过，已回填 expected_source
STATUS_REOPENED = "reopened"  # 验证没过，退回改稿

# 草稿里用标记包住"将被追加到目标文档的那一段"。
# 靠它做两件事：apply 时精确提取；重复 apply 时替换而不是重复插入。
_MARK_BEGIN = "<!-- kb_ticket:{id} BEGIN -->"
_MARK_END = "<!-- kb_ticket:{id} END -->"

# 默认关单线：验证问句都要进 top-3。
# 定 3 而不是 5，是因为主链路只把 top-3 塞进 prompt（见 policy_service 默认 top_k），
# 排第 4 等于用户拿不到。
DEFAULT_TOP_N = 3


def _proj_root() -> Path:
    """项目根：向上找同时有 backend/ 和 docs/ 的目录。

    不写死 parents[N]——这个文件被脚本、被 pytest、可能被装进容器三种方式加载，
    层级一变就指错地方，而指错地方的后果是草稿写到别的目录去。
    """
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "backend").is_dir() and (p / "docs").is_dir():
            return p
    return here.parents[4]


def kb_dir(root: Optional[Path] = None) -> Path:
    """语料目录：跟 ingest_kb.py 同一个环境变量，避免"草稿放 A、入库扫 B"。"""
    base = root or _proj_root()
    raw = os.getenv("KB_DIR") or "docs/fashion_kb"
    p = Path(raw)
    return p if p.is_absolute() else base / p


def ops_dir(root: Optional[Path] = None) -> Path:
    """工单与草稿的落盘目录（在 data/ 下，已被 .gitignore 忽略）。"""
    return (root or _proj_root()) / "data" / "kb_ops"


def _store_path(root: Optional[Path] = None) -> Path:
    return ops_dir(root) / "tickets.json"


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_store(root: Optional[Path] = None) -> Dict[str, Any]:
    p = _store_path(root)
    if not p.exists():
        return {"next_seq": 1, "tickets": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # 坏了不静默重置：工单记录的是"这条知识补过没补过"，
        # 悄悄清空等于让运营再补一遍已经补过的卡。
        raise RuntimeError(f"工单库损坏，请人工检查 {p}: {e}") from e
    data.setdefault("next_seq", 1)
    data.setdefault("tickets", [])
    return data


def save_store(store: Dict[str, Any], root: Optional[Path] = None) -> Path:
    """原子写：先写 .tmp 再 replace。

    直接 write_text 被打断会留下半个 JSON，下次 load 抛错——
    运营工具最不该在"记账"这件事上丢数据。
    """
    if root is None and os.getenv("PYTEST_CURRENT_TEST"):
        # 踩过一次：单测里只传了内存 store 没传 root，save_store 落到真实
        # data/kb_ops/tickets.json，把假的 TK-001 写进了运营台账。
        # 与其靠每个测试记得传 root，不如在这里硬失败。
        raise RuntimeError("测试里保存工单必须显式传 root，否则会写脏真实 data/kb_ops")
    p = _store_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def _next_id(store: Dict[str, Any]) -> str:
    seq = int(store.get("next_seq", 1))
    store["next_seq"] = seq + 1
    return f"TK-{seq:03d}"


def find_ticket(store: Dict[str, Any], ticket_id: str) -> Optional[Dict[str, Any]]:
    for t in store.get("tickets", []):
        if t.get("id") == ticket_id:
            return t
    return None


def _find_live(store: Dict[str, Any], source: str, kind: str) -> Optional[Dict[str, Any]]:
    """同一知识源 + 同一类型只允许一张未关闭的单。

    为什么必须去重：同一段政策会被十几个问句拖累，每个问句开一张单，
    运营看到的清单是"同一件事重复 12 次"，实际只有一处要改。
    """
    for t in store.get("tickets", []):
        if t.get("source") == source and t.get("kind") == kind \
                and t.get("status") != STATUS_CLOSED:
            return t
    return None


# ---------------------------------------------------------------
# 草稿渲染
# ---------------------------------------------------------------
_TYPE_HINTS = (
    ("promo", "promo", "活动"),
    ("return", "policy", "售后"),
    ("refund", "policy", "售后"),
    ("shipping", "policy", "物流"),
    ("delivery", "policy", "物流"),
    ("size", "guide", "尺码"),
    ("spec", "spec", "商品"),
    ("care", "faq", "养护"),
)
_TAG_HINTS = {
    "promo": ["活动", "优惠", "满减"],
    "policy": ["政策", "规则"],
    "guide": ["尺码", "选码"],
    "spec": ["商品", "参数"],
    "faq": ["常见问题"],
}


def _guess_doc_meta(stem: str) -> Dict[str, Any]:
    low = stem.lower()
    doc_type, category = "policy", "待填"
    for key, dt, cat in _TYPE_HINTS:
        if key in low:
            doc_type, category = dt, cat
            break
    return {
        "doc_type": doc_type,
        "category": category,
        "tags": _TAG_HINTS.get(doc_type, ["待填"]),
    }


def _front_matter(ticket: Dict[str, Any]) -> str:
    stem = ticket["source_stem"]
    meta = _guess_doc_meta(stem)
    tags = ", ".join(meta["tags"])
    lines = [
        "---",
        f"title: TODO 填写标题（{stem}）",
        f"doc_key: {stem}",
        f"revision: '{_dt.datetime.now():%Y.%m}'",
        f"doc_type: {meta['doc_type']}",
        f"category: {meta['category']}",
        f"tags: [{tags}]",
    ]
    # 活动/政策类默认 temporary 且**强制**填有效期：
    # 这个闭环第一张单就是"双 11 卡过期了没人发现"引出来的，
    # 模板要是默认 permanent，等于教运营把同一类错误再犯一遍。
    if meta["doc_type"] == "promo":
        lines += [
            "validity: temporary",
            "effective_from: TODO 如 2026-12-01",
            "effective_to: TODO 如 2026-12-12",
        ]
    else:
        # 政策卡按项目惯例是 permanent（长期有效、靠 revision 换版），
        # 但版本生效日必须人确认——库里每张政策卡都有 effective_from。
        lines += ["validity: permanent", "effective_from: TODO 如 2026-11-01"]
    lines.append("issuer: TODO 填写发布方（如实测/客服中心）")
    lines.append("---")
    return "\n".join(lines)


def _evidence_block(ticket: Dict[str, Any]) -> str:
    ev = ticket.get("evidence") or {}
    lines = [
        f"  工单：{ticket['id']}",
        f"  知识源：{ticket['source']}",
        f"  根因：{ticket['root_cause']}（{ev.get('reason', '-')}）",
        f"  拖累问句数：{len(ticket.get('queries') or [])}",
        f"  生成时间：{ticket.get('created_at', '-')}",
    ]
    top = ev.get("retrieved_sources") or []
    if top:
        lines.append("  开单时 top-k 实际命中：" + " > ".join(str(s) for s in top))
    if ev.get("rank") is not None:
        lines.append(f"  开单时该知识源排位：第 {ev['rank']} 位")
    return "\n".join(lines)


def render_draft(ticket: Dict[str, Any]) -> str:
    """生成草稿文本。三类工单三种骨架。"""
    tid = ticket["id"]
    begin, end = _MARK_BEGIN.format(id=tid), _MARK_END.format(id=tid)
    queries = ticket.get("queries") or []
    head = ["<!--", "  本文件由知识运营闭环自动生成，是**草稿**，不会自动入库。"]
    expired = (ticket.get("evidence") or {}).get("expired_source")
    if expired:
        head.append(f"  替代已过期的 {expired}：原文件不要改也不要删，它是历史依据。")
        head.append("  文件名 xxx_new.md 只是占位，apply 时用 --as 改成当期活动名。")
    head += [
        "  改完执行：python scripts/kb_ops.py --apply " + tid,
        "  入库后执行：python scripts/kb_ops.py --verify " + tid,
        _evidence_block(ticket),
        "-->",
        "",
    ]

    if ticket["kind"] == KIND_ADD:
        # front matter 必须在**文件第一行**：ingest 的 read_front_matter 用
        # re.match 从头匹配，前面多一个注释块，整份 YAML 就不生效——
        # 实盘踩过：第一版把工单注释放最前面，入库后 validity=permanent、
        # doc_type=doc、tags 为空，等于亲手造出下一张"永不失效"的活动卡。
        body = [_front_matter(ticket), ""] + head + [
            f"# TODO 标题（{ticket['source_stem']}）",
            "",
            "> 只写**能对上官方口径**的结论。下面每个小标题对应一条真实问句，",
            "> 逐个填结论和适用条件；填不出来的就删掉那一节，别猜。",
            "",
            begin,
            "",
        ]
        for i, q in enumerate(queries, 1):
            body += [f"## {i}. {q}", "", "TODO 结论 + 适用条件 + 例外情况", ""]
        body += [end, ""]
        return "\n".join(body)

    # 检索修复 / 改卡：都是往现有文档里追加/替换一段，不是新建文件。
    reason = {
        KIND_PATCH: "有这条知识，但检索没把它排到前面——**不要新建卡**，"
                    "在现有文档里补「常见问法」，让这段文本贴近买家的说法",
        KIND_UPDATE: "检索能命中，但答案与资料冲突——确认是条款过期还是表述有歧义，"
                     "直接改上文对应段落，不要新增互相矛盾的第二套说法",
    }[ticket["kind"]]
    body = [
        f"# 补丁草稿：{ticket['source']}",
        "",
        f"> {reason}",
        ">",
        "> `--apply` 会把下面标记之间的内容追加到目标文档末尾（重复执行只替换、不重复插入）。",
        "> 答案要点只能从该文档**已有条款**里摘，不要在这里新增政策——新增政策要走补新卡。",
        "",
        begin,
        "",
        "## 常见问法（客服检索锚点）",
        "",
    ]
    for q in queries:
        body += [f"- **「{q}」**：TODO 从上文摘一句结论", ""]
    body += [
        "<!-- 这一节的作用是让买家说法与条款文本在向量空间里靠近；",
        "     每条只写一句话结论 + 指向上文哪一节，不要写成第二份政策 -->",
        "",
        end,
        "",
    ]
    return "\n".join(head + body)


def write_draft(ticket: Dict[str, Any], root: Optional[Path] = None) -> Path:
    """落草稿文件，并回写 ticket.draft（相对项目根的路径，方便日志里直接显示）。"""
    base = root or _proj_root()
    d = ops_dir(base) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ticket['id']}.md"
    p.write_text(render_draft(ticket), encoding="utf-8")
    try:
        ticket["draft"] = str(p.relative_to(base))
    except ValueError:
        ticket["draft"] = str(p)
    return p


# ---------------------------------------------------------------
# 开单
# ---------------------------------------------------------------
def _norm_source(source: str) -> str:
    s = (source or "").strip()
    if not s:
        return ""
    stem = doc_stem(s)
    return f"{stem}.md" if stem else ""


def open_ticket(
    db: Session,
    *,
    query: str,
    expected_source: str,
    root: Optional[Path] = None,
    top_n: int = DEFAULT_TOP_N,
    top_k: int = 5,
    store: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """人工发现一条 badcase → 归因 → 开单（或并入已有未关闭单）。

    返回 {"ticket": {...}, "created": bool, "skipped": str|None}。
    归因属于非知识侧（generation / context_missing / unknown）时不开单，返回 skipped 原因。
    """
    src = _norm_source(expected_source)
    if not src:
        return {"ticket": None, "created": False,
                "skipped": "必须给出 expected_source（该问句本该命中哪条知识）"}

    res = diagnose_one(db, query=query, expected_source=src, top_k=top_k,
                       answer=None, failure_layer=None)
    cause = res["root_cause"]
    kind = CAUSE_TO_KIND.get(cause)
    if kind is None:
        return {"ticket": None, "created": False,
                "skipped": f"根因 {cause} 不是知识侧问题，不开知识工单"
                           f"（{res['evidence'].get('reason', '')}）"}

    st = store if store is not None else load_store(root)
    existing = _find_live(st, src, kind)
    if existing is not None:
        qs = existing.setdefault("queries", [])
        base = existing.setdefault("baseline", {})
        if query and query not in qs:
            qs.append(query)
        base.setdefault(query, res["evidence"].get("rank"))
        existing["updated_at"] = _now()
        existing.setdefault("history", []).append(
            {"at": _now(), "event": "merged", "note": query[:80]})
        if store is None:
            save_store(st, root)
        return {"ticket": existing, "created": False, "skipped": None}

    ticket_source = src
    extra_evidence: Dict[str, Any] = {}
    if cause == ROOT_KB_EXPIRED:
        # 过期卡的修法是"补一条当前有效的新卡"，绝不能覆盖原文档：
        # 旧文档是历史依据，删了以后查"双 11 当时怎么规定的"就没处可考。
        # 所以工单的知识源另起一个文件名，原名只留在证据里。
        ticket_source = f"{doc_stem(src)}_new.md"
        extra_evidence["expired_source"] = src

    ticket: Dict[str, Any] = {
        "id": _next_id(st),
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "source": ticket_source,
        "source_stem": doc_stem(ticket_source),
        "root_cause": cause,
        "status": STATUS_OPEN,
        "top_n": top_n,
        "queries": [query] if query else [],
        "baseline": {query: res["evidence"].get("rank")} if query else {},
        "evidence": {k: v for k, v in res["evidence"].items() if k != "query"},
        "created_at": _now(),
        "updated_at": _now(),
        "history": [{"at": _now(), "event": "opened", "note": cause}],
    }
    ticket["evidence"].update(extra_evidence)
    st["tickets"].append(ticket)
    if store is None:
        save_store(st, root)
    return {"ticket": ticket, "created": True, "skipped": None}


def draft_ticket(
    ticket: Dict[str, Any],
    *,
    root: Optional[Path] = None,
    store: Optional[Dict[str, Any]] = None,
) -> Path:
    """生成/重生成草稿。已入库(closed)的单不给再出草稿，防止覆盖人工已合入的内容。"""
    if ticket.get("status") == STATUS_CLOSED:
        raise RuntimeError(f"{ticket['id']} 已关闭，不再生成草稿")
    p = write_draft(ticket, root)
    ticket["status"] = STATUS_DRAFTED
    ticket["updated_at"] = _now()
    ticket.setdefault("history", []).append({"at": _now(), "event": "drafted"})
    if store is not None:
        save_store(store, root)
    return p


# ---------------------------------------------------------------
# 落盘（apply）
# ---------------------------------------------------------------
def _extract_block(text: str, ticket_id: str) -> Optional[str]:
    """取出草稿里标记之间的内容（含标记行），没标记返回 None。"""
    begin = _MARK_BEGIN.format(id=ticket_id)
    end = _MARK_END.format(id=ticket_id)
    i, j = text.find(begin), text.find(end)
    if i < 0 or j < 0 or j < i:
        return None
    return text[i:j + len(end)]


def _guard_todos(content: str, tid: str, force: bool) -> None:
    """草稿里还有 TODO 就不许入库。

    机器只会写骨架，结论必须人填。放行未填的草稿，等于让"TODO 结论 + 适用条件"
    这样的句子变成可检索的知识，模型照着它答出来就是一派胡言。
    """
    if force:
        return
    n = content.count("TODO")
    if n:
        raise RuntimeError(
            f"{tid} 的草稿还有 {n} 处 TODO 未填，不入库。"
            f"填完再 apply；确需先入（如只补结构）加 --force"
        )


def apply_ticket(
    ticket: Dict[str, Any],
    *,
    root: Optional[Path] = None,
    store: Optional[Dict[str, Any]] = None,
    kb: Optional[Path] = None,
    as_name: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """把草稿落到知识库目录。

    - add_card：整篇写成新文档（剥掉工单标记，避免注释进 chunk 污染检索）
    - fix_retrieval / update_card：把标记块追加到现有文档末尾；已存在同标记则替换

    as_name：给补新卡换个更像样的文件名（草稿里的 `xxx_new.md` 只是占位）。
    改名会同步改 front matter 的 doc_key——doc_key 是主题键，文件名换了主题也换了。

    返回 {"path": ..., "mode": "create"|"append"|"replace"}。
    不自动跑 ingest：embeddings 要花钱，且入库属于写操作，必须由人显式触发。
    """
    tid = ticket["id"]
    base = root or _proj_root()
    dpath = ticket.get("draft")
    if not dpath:
        # 草稿路径是确定性的（drafts/<id>.md），不依赖工单字段。
        # 老工单没有 draft 字段也能 apply，省得为了补一个字段去重生成草稿、
        # 把人工填的内容冲掉。
        cand = ops_dir(base) / "drafts" / f"{tid}.md"
        if not cand.exists():
            raise RuntimeError(f"{tid} 还没有草稿，先跑 --draft {tid}")
        dpath = str(cand)
    draft_path = Path(dpath)
    if not draft_path.is_absolute():
        draft_path = base / draft_path
    if not draft_path.exists():
        raise RuntimeError(f"草稿文件不存在：{draft_path}")
    text = draft_path.read_text(encoding="utf-8")

    target_dir = kb or kb_dir(base)
    target_dir.mkdir(parents=True, exist_ok=True)
    final_name = (as_name or ticket["source"]).strip()
    if not final_name.endswith(".md"):
        final_name += ".md"
    target = target_dir / final_name

    if ticket["kind"] == KIND_ADD:
        if target.exists():
            # 同一张工单改稿重跑（要重灌）允许覆盖自己的产物；
            # 但首次 apply 就撞上已有文件必须拒绝——补新卡最忌覆盖：
            # 过期卡覆盖掉就查不到"当时是怎么规定的"，两张卡撞名字还会让
            # upsert 把旧 chunk 冲掉一半。
            mine = (ticket.get("applied_name") == final_name
                    or ticket.get("status") == STATUS_APPLIED)
            if not mine:
                raise RuntimeError(
                    f"{final_name} 已存在——补新卡不覆盖已有知识。"
                    f"用 --as 换个文件名（如 fashion_promo_2026_dec.md）"
                )
        begin, end = _MARK_BEGIN.format(id=tid), _MARK_END.format(id=tid)
        clean = text.replace(begin, "").replace(end, "")
        stem = Path(final_name).stem
        clean = re.sub(r"^title: TODO.*$", f"title: {stem}", clean, count=1, flags=re.M)
        clean = re.sub(r"^doc_key: .*$", f"doc_key: {stem}", clean, count=1, flags=re.M)
        _guard_todos(clean, tid, force)
        target.write_text(clean.strip() + "\n", encoding="utf-8")
        mode = "create"
        ticket["applied_name"] = final_name
        if as_name:
            ticket["source"] = final_name
            ticket["source_stem"] = stem
    else:
        block = _extract_block(text, tid)
        if not block:
            raise RuntimeError(f"草稿里找不到 {tid} 的标记块，无法定位要追加的内容")
        if not target.exists():
            # 目标文档不存在说明 expected_source 标错了（或文件名改了），
            # 这时静默新建会造出一张"凭空出现的知识"，必须让人先核对。
            # 放在 TODO 检查之前：标错文件比没填完更致命，先报这个。
            raise RuntimeError(f"目标文档不存在：{target}（expected_source 是否标错？）")
        _guard_todos(block, tid, force)
        old = target.read_text(encoding="utf-8")
        begin, end = _MARK_BEGIN.format(id=tid), _MARK_END.format(id=tid)
        i, j = old.find(begin), old.find(end)
        if i >= 0 and j > i:
            old = old[:i] + block + old[j + len(end):]
            mode = "replace"
        else:
            old = old.rstrip() + "\n\n" + block + "\n"
            mode = "append"
        target.write_text(old, encoding="utf-8")

    ticket["status"] = STATUS_APPLIED
    ticket["applied_at"] = _now()
    ticket["updated_at"] = _now()
    ticket.setdefault("history", []).append(
        {"at": _now(), "event": "applied", "note": f"{mode} {target.name}"})
    if store is not None:
        save_store(store, root)
    return {"path": target, "mode": mode}


def list_tickets(
    store: Dict[str, Any],
    *,
    status: Optional[str] = None,
    include_closed: bool = False,
) -> List[Dict[str, Any]]:
    out = []
    for t in store.get("tickets", []):
        if status and t.get("status") != status:
            continue
        if not include_closed and not status and t.get("status") == STATUS_CLOSED:
            continue
        out.append(t)
    return out
