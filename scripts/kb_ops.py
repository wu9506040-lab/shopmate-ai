"""知识运营闭环 CLI：归因 → 待办清单 → 工单草稿 → 入库 → 回归验证。

用法:
    # 归因（写回 qa_results.root_cause）
    PYTHONPATH=backend python scripts/kb_ops.py --diagnose --limit 100

    # 只看不改（先确认判据对不对再落库）
    PYTHONPATH=backend python scripts/kb_ops.py --diagnose --dry-run --limit 20

    # 出待办清单
    PYTHONPATH=backend python scripts/kb_ops.py --report --top 15

    # 人工发现一条 badcase：开单 + 出草稿
    PYTHONPATH=backend python scripts/kb_ops.py --open "现在有什么优惠活动" \
        --source fashion_promo_2026_double11.md

    # 看工单
    PYTHONPATH=backend python scripts/kb_ops.py --tickets

    # 改完草稿后落盘并入库（默认会调 ingest_kb.py）
    PYTHONPATH=backend python scripts/kb_ops.py --apply TK-001

    # 入库后回归验证，通过则关单并回填 expected_source
    PYTHONPATH=backend python scripts/kb_ops.py --verify TK-001
    PYTHONPATH=backend python scripts/kb_ops.py --verify all

设计说明：diagnose / verify 会真调 embedding + Milvus，按条计费/按秒计延迟，
所以 diagnose 默认 limit=100，不是我小气，是一次跑几千条会把检索服务打满。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

try:
    from dotenv import load_dotenv  # type: ignore

    for env_file in [
        BACKEND_DIR / ".env",
        PROJECT_ROOT / "deploy" / ".env.dev",
        PROJECT_ROOT / ".env",
    ]:
        if env_file.exists():
            load_dotenv(env_file)
            break
except ImportError:
    pass

from app.clients.mysql_client import with_safe_session  # noqa: E402
from app.services.kb_ops import (  # noqa: E402
    ROOT_CAUSE_LABELS,
    apply_ticket,
    draft_ticket,
    list_tickets,
    load_store,
    open_ticket,
    run_batch,
    settle_pending,
    settle_ticket,
    source_report,
)
from app.services.kb_ops.ticket import find_ticket  # noqa: E402

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "kb_ops_report.json"


def _print_diagnose(res: dict) -> None:
    mode = "DRY-RUN（未落库）" if res.get("dry_run") else "已落库"
    print(f"\n归因完成 [{mode}]")
    print(f"  扫描 {res['scanned']} 条，更新 {res['updated']} 条，"
          f"跳过 {res['skipped']} 条（{res.get('pending_review', 0)} 条缺 expected_source）")
    if not res["by_cause"]:
        print("  没有可归因的记录")
        return
    print("\n  根因分布：")
    for cause, n in sorted(res["by_cause"].items(), key=lambda kv: -kv[1]):
        label = ROOT_CAUSE_LABELS.get(cause, cause)
        print(f"    {cause:<15} {n:>4}  {label}")

    unresolved = [d for d in res.get("details", [])
                  if d["root_cause"] in ("no_kb", "retrieval_miss")]
    if unresolved:
        print("\n  前 5 条知识侧缺口：")
        for d in unresolved[:5]:
            print(f"    #{d['id']} {d['root_cause']:<15} {d['reason']}")


def _print_report(rep: dict, top: int) -> None:
    print(f"\n待办清单（共 {rep['total']} 条已归因，"
          f"{rep['source_count']} 个涉及知识源，另有 {rep['pending_review']} 条待标注）")
    if not rep["by_cause"]:
        print("  没有数据，先跑 --diagnose")
        return

    print("\n  根因分布：")
    for cause, n in sorted(rep["by_cause"].items(), key=lambda kv: -kv[1]):
        print(f"    {cause:<15} {n:>4}  {ROOT_CAUSE_LABELS.get(cause, cause)}")

    print(f"\n  问题知识源 TOP {min(top, len(rep['sources']))}：")
    for i, s in enumerate(rep["sources"], 1):
        causes = " / ".join(f"{k}×{v}" for k, v in
                            sorted(s["causes"].items(), key=lambda kv: -kv[1]))
        flag = "库里已有" if s["kb_has_source"] else "库里没有"
        print(f"    {i:>2}. {s['source']}  失败 {s['total']} 次  [{flag}]")
        print(f"        根因：{causes}  → {s['action']}")
        for q in s["queries"]:
            print(f"        例：{q[:60]}")


def _print_ticket(t: dict, verbose: bool = False) -> None:
    print(f"  {t['id']}  {t['status']:<9} {t.get('kind_label', t['kind']):<22} "
          f"{t['source']}")
    print(f"        根因 {t['root_cause']}  ·  问句 {len(t.get('queries') or [])} 条  "
          f"·  更新 {t.get('updated_at', '-')}")
    for q in (t.get("queries") or [])[:5 if verbose else 3]:
        print(f"        · {q[:60]}")
    if verbose:
        if t.get("draft"):
            print(f"        草稿 {t['draft']}")
        if t.get("last_verify"):
            lv = t["last_verify"]
            print(f"        上次验证 {lv['at']}  {lv['hits']}/{lv['total']} 进线")
        for h in (t.get("history") or [])[-5:]:
            print(f"        [{h['at']}] {h['event']} {h.get('note') or ''}")


def _print_verify(res: dict) -> None:
    stamp = "（跳过精排，仅粗排口径）" if res.get("skip_rerank") else ""
    print(f"\n验证 {res['ticket']} → {res['source']}（进线 top-{res['top_n']}）{stamp}")
    baseline = res.get("baseline") or {}
    for d in res["details"]:
        q = d["query"][:40]
        if d.get("error"):
            print(f"    ⚠ {q:<40} 检索不可用，未判定")
            continue
        mark = "✅" if d["hit"] else "❌"
        before = baseline.get(d["query"])
        before_s = f"第{before}位" if before else "未命中"
        after_s = f"第{d['rank']}位" if d["rank"] else "未命中"
        print(f"    {mark} {q:<40} 补前 {before_s:<6} → 补后 {after_s}")
    if res.get("inconclusive"):
        print("  结果：检索不可用，本轮不判定（状态未改动）")
        return
    verdict = "通过，已关单" if res["passed"] else "未通过，退回改稿"
    print(f"  结果：{res['hits']}/{res['total']} 进线 → {verdict}")
    if res.get("backfilled"):
        print(f"  顺带回填 expected_source：{res['backfilled'].get('updated', 0)} 条 badcase")


def _resolve(store: dict, ticket_id: str) -> dict:
    t = find_ticket(store, ticket_id)
    if t is None:
        raise SystemExit(f"找不到工单 {ticket_id}，先跑 --tickets 看看有哪些")
    return t


def _run_ingest(filename: str) -> int:
    """落盘后重灌这一篇。

    必须带 --force：我们是**改了这一篇**才重灌，而这篇的 state 里还留着旧 hash，
    走增量会把"没变的段落"跳掉，偏偏 upsert 前又按 source 前缀删了整篇旧 chunk——
    结果就是改一句话，丢半篇（实盘踩过：5 块变 1 块）。
    只灌指定文件，不扫全库（全扫会重算其它文档的 embedding）。
    """
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "ingest_kb.py"),
           "--file", filename, "--force"]
    print("\n入库：", " ".join(cmd[1:]))
    try:
        return subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    except OSError as e:
        print(f"  入库失败（{e}），请手动执行：python scripts/ingest_kb.py --file {filename} --force")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="知识运营闭环：归因 / 待办 / 工单 / 验证")
    parser.add_argument("--diagnose", action="store_true", help="跑归因并写回库")
    parser.add_argument("--report", action="store_true", help="出待办清单")
    parser.add_argument("--open", default=None, metavar="QUERY",
                        help="人工发现一条 badcase：开单并出草稿")
    parser.add_argument("--source", default=None,
                        help="配合 --open：该问句本该命中的知识源（如 xxx.md）")
    parser.add_argument("--tickets", action="store_true", help="列出工单")
    parser.add_argument("--all", action="store_true", help="配合 --tickets 连已关闭的一起列")
    parser.add_argument("--draft", default=None, metavar="TK-ID",
                        help="（重新）生成草稿")
    parser.add_argument("--apply", default=None, metavar="TK-ID",
                        help="草稿落盘到知识库目录，默认随后调 ingest 入库")
    parser.add_argument("--as", dest="as_name", default=None, metavar="FILENAME",
                        help="配合 --apply：给补新卡换个文件名（默认 xxx_new.md 是占位）")
    parser.add_argument("--verify", default=None, metavar="TK-ID|all",
                        help="回归验证；all = 所有已入库待验证的单")
    parser.add_argument("--no-ingest", action="store_true", help="--apply 时不自动入库")
    parser.add_argument("--force", action="store_true",
                        help="--apply 时放行仍有 TODO 的草稿（默认拦截）")
    parser.add_argument("--allow-partial", action="store_true",
                        help="验证时允许降级为粗排口径（默认走完整链路）")
    parser.add_argument("--limit", type=int, default=100, help="单次归因条数上限")
    parser.add_argument("--top-k", type=int, default=5, help="判定命中用的检索深度")
    parser.add_argument("--top", type=int, default=15, help="清单展示前 N 个知识源")
    parser.add_argument("--cause", default=None, help="只统计某一类根因")
    parser.add_argument("--dry-run", action="store_true", help="只算不写库")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="报告落盘路径")
    parser.add_argument("--no-save", action="store_true", help="不落盘")
    args = parser.parse_args()

    took_action = any([args.diagnose, args.report, args.open, args.tickets,
                       args.draft, args.apply, args.verify])
    if not took_action:
        args.diagnose = True
        args.report = True

    # ---- 不需要数据库的动作 ----
    if args.tickets and not (args.open or args.verify):
        st = load_store()
        rows = list_tickets(st, include_closed=args.all)
        if not rows:
            print("没有未关闭的工单（--all 可看全部）")
        else:
            print(f"\n工单 {len(rows)} 张：")
            for t in rows:
                _print_ticket(t, verbose=True)
        return 0

    if args.draft and not args.apply:
        st = load_store()
        t = _resolve(st, args.draft)
        p = draft_ticket(t, store=st)
        print(f"草稿已生成：{p}\n  改完执行：python scripts/kb_ops.py --apply {t['id']}")
        return 0

    if args.apply:
        # 特意放在 with_safe_session 之外：入库要跑 embedding，可能几分钟，
        # 占着数据库连接会拖累同池的其它请求。
        st = load_store()
        t = _resolve(st, args.apply)
        out = apply_ticket(t, store=st, as_name=args.as_name, force=args.force)
        print(f"{t['id']} 已{('新建' if out['mode'] == 'create' else '写入')}："
              f"{out['path']}（{out['mode']}）")
        if args.no_ingest:
            print("  跳过入库（--no-ingest）。记得手动跑："
                  f"python scripts/ingest_kb.py --file {t['source']}")
            return 0
        code = _run_ingest(t["source"])
        if code != 0:
            print("  入库未完成，先别验证（向量没更新，验了也是白验）")
            return code
        print(f"  入库完成，接着验证：python scripts/kb_ops.py --verify {t['id']}")
        return 0

    # ---- 需要数据库的动作 ----
    payload = {}
    with with_safe_session(commit=False) as db:
        if args.open:
            if not args.source:
                raise SystemExit("--open 必须配 --source（该问句本该命中哪条知识）")
            # 把 store 传进去，让它和 draft_ticket 用同一个对象：
            # 否则 open_ticket 自己存一份、draft_ticket 又存另一份，
            # 后存的那份没有 draft 字段，apply 时会说"还没有草稿"。
            st = load_store()
            res = open_ticket(db, query=args.open, expected_source=args.source,
                              top_k=args.top_k, store=st)
            if res["ticket"] is None:
                print(f"未开单：{res['skipped']}")
                return 1
            t = res["ticket"]
            print(f"{'已开单' if res['created'] else '并入已有单'} {t['id']}  "
                  f"{t['root_cause']} → {t.get('kind_label')}")
            p = draft_ticket(t, store=st)
            print(f"  草稿：{p}")
            print(f"  改完执行：python scripts/kb_ops.py --apply {t['id']}")
            return 0

        if args.verify:
            if args.verify == "all":
                results = settle_pending(db, skip_rerank=args.allow_partial)
                if not results:
                    print("没有待验证的工单（只有 applied 状态才会被巡检）")
                for r in results:
                    _print_verify(r)
            else:
                st = load_store()
                t = _resolve(st, args.verify)
                r = settle_ticket(db, t, store=st, skip_rerank=args.allow_partial)
                _print_verify(r)
            return 0

        if args.diagnose:
            res = run_batch(db, limit=args.limit, top_k=args.top_k,
                            dry_run=args.dry_run)
            _print_diagnose(res)
            payload["diagnose"] = {k: v for k, v in res.items() if k != "details"}

        if args.report:
            rep = source_report(db, cause=args.cause, top=args.top)
            _print_report(rep, args.top)
            payload["report"] = rep

    if not args.no_save and payload:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n报告已写入 {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
