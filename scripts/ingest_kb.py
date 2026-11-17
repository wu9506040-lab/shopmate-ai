#!/usr/bin/env python3
"""
通用知识库入库流水线

支持格式与解析（doc_parsers.py）：
  .pdf   页眉/页脚剔除、字号层级识别章节、表格转 markdown、图片引用导出、脚注成块
  .md    标题层级感知（章节路径保留）
  .txt   段落
  .json  原 FAQ 格式

流程：扫描 docs/ecommerce_kb/*
  → 解析（多格式 → 统一 items，带 page/section/kind 元数据）
  → 增量判断（source+content_hash，未变更零成本跳过）
  → 语义切分（句子边界贪心打包 ≤600 字 + 尾句重叠，绝不截断句中）
  → 批量向量化（text-embedding-v3）
  → Milvus upsert（payload: channel/category/section/page/kind/content_hash）
  → MySQL knowledge_documents 镜像

渠道隔离：文件名/category 含 "xianyu" → channel="xianyu"，否则 "mall"
幂等：重复跑零成本；失败不清 state（下次重灌）

使用：
  python scripts/ingest_kb.py                 # 全量扫描（增量）
  python scripts/ingest_kb.py --file xx.pdf   # 指定文件
  python scripts/ingest_kb.py --force         # 忽略 hash 强制重灌
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from doc_parsers import parse_file, semantic_chunks  # noqa: E402

ENV_FILE = Path(__file__).resolve().parents[1] / "deploy" / ".env.dev"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import OpenAI  # noqa: E402

KB_DIR = Path(__file__).resolve().parents[1] / "docs" / "ecommerce_kb"
# 语料目录可由 KB_DIR 环境变量覆盖（相对路径基于项目根）——
# 换业务域时只改 .env 的 KB_DIR，不动脚本
_kb_env = os.getenv("KB_DIR")
if _kb_env:
    _p = Path(_kb_env)
    KB_DIR = _p if _p.is_absolute() else Path(__file__).resolve().parents[1] / _p
STATE_FILE = Path(__file__).resolve().parents[1] / "data" / ".ingest_state.json"
COLLECTION = os.getenv("COLLECTION", os.environ.get("COLLECTION_NAME", "knowledge_base"))
EMBEDDING_MODEL = "text-embedding-v3"

CHUNK_MAX_CHARS = 600


def content_hash(title: str, text: str) -> str:
    return hashlib.sha1(f"{title}\n{text}".encode("utf-8")).hexdigest()[:16]


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def embed_texts(texts: list[str]) -> list[list[float]]:
    # embedding 独立凭据优先（EMBEDDING_API_KEY=DashScope，与 chat 的
    # QWEN_API_KEY=MiniMax 分离——chat 可切 mock，embedding 必须真实）
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("QWEN_API_KEY", "")
    base_url = os.getenv(
        "EMBEDDING_BASE_URL",
        os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
    last_err = None
    for attempt in range(3):
        try:
            r = client.embeddings.create(
                model=EMBEDDING_MODEL, input=texts, encoding_format="float", timeout=30
            )
            return [d.embedding for d in r.data]
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 1.0 * (2 ** attempt)
            print(f"  [embed] 重试 {attempt+1}/3: {type(e).__name__}, waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"embedding 失败 3 次: {last_err}")


MILVUS_URI = os.getenv("MILVUS_URI", "http://127.0.0.1:19530")


def _milvus_ensure(collection: str) -> None:
    """确保 collection 存在（varchar pk + 1024 维 COSINE + dynamic field）"""
    from pymilvus import MilvusClient, DataType
    mc = MilvusClient(uri=MILVUS_URI)
    if mc.has_collection(collection):
        return
    schema = mc.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=1024)
    for f in ("text", "channel", "source", "section", "title", "doc_type", "category",
              # 2026-11 知识库元信息：标签 + 有效期（政策分长期/临时）
              "tags", "validity", "issuer", "version",
              "doc_key", "revision", "doc_source"):
        schema.add_field(f, DataType.VARCHAR, max_length=8192, nullable=True)
    schema.add_field("page", DataType.INT64, nullable=True)
    # 生效/失效时间用秒级时间戳（INT64）：Milvus 标量不支持 DATE，
    # 存字符串排序/比较都会踩坑（"2026-9-1" vs "2026-10-01"）
    schema.add_field("effective_from", DataType.INT64, nullable=True)
    schema.add_field("effective_to", DataType.INT64, nullable=True)
    idx = mc.prepare_index_params()
    idx.add_index(field_name="vector", index_type="HNSW", metric_type="COSINE",
                  params={"M": 16, "efConstruction": 128})
    mc.create_collection(collection_name=collection, dimension=1024, schema=schema, index_params=idx, id_type="varchar", primary_field_name="id", vector_field_name="vector", metric_type="COSINE")
    print(f"  [milvus] collection '{collection}' 创建成功")


def vector_upsert(points: list[dict]) -> bool:
    """把 chunk 向量写入向量库（Milvus standalone）。"""
    try:
        from pymilvus import MilvusClient
        _milvus_ensure(COLLECTION)
        mc = MilvusClient(uri=MILVUS_URI)
        rows = []
        for pt in points:
            payload = dict(pt.get("payload") or {})
            # nullable 字段缺省补全（Milvus 对缺失/None 的标量字段严格）
            for f in ("text", "channel", "source", "section", "title", "doc_type", "category",
                      "tags", "validity", "issuer", "version",
                      "doc_key", "revision", "doc_source"):
                payload.setdefault(f, "")
            if not isinstance(payload.get("page"), int):
                payload["page"] = 0
            for f in ("effective_from", "effective_to"):
                if not isinstance(payload.get(f), int):
                    payload[f] = 0
            payload["id"] = str(pt["id"])
            payload["vector"] = pt["vector"]
            rows.append(payload)
        mc.upsert(collection_name=COLLECTION, data=rows)
        return True
    except Exception as e:
        print(f"  [milvus] upsert 失败: {e}")
        return False


def mirror_mysql(source: str, title: str, doc_type: str,
                 total_chunks: int, total_chars: int, channel: str,
                 doc_key: str = "", revision: str = "", validity: str = "permanent",
                 effective_from: int = 0, effective_to: int = 0) -> None:
    """镜像 knowledge_documents（source 唯一键 upsert；缺列自动补）

    2026-11：本表同时充当**版本清单**——一行一个文档版本，运行期据此按时间
    选出每个 doc_key 的当前版本。不额外建表，避免"向量库与版本表"两处维护。
    """
    try:
        import pymysql
        conn = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("MYSQL_PORT", "3307")),  # host 映射端口（容器内 3306）
            user=os.environ.get("MYSQL_USER", "cs_user"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "customer_service"),
            charset="utf8mb4",
        )
    except Exception as e:
        print(f"  [mysql] 连接失败（跳过镜像）: {e}")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name='knowledge_documents' "
                "AND column_name='channel'"
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "ALTER TABLE knowledge_documents ADD COLUMN channel VARCHAR(20) "
                    "NOT NULL DEFAULT 'mall'"
                )
            # 版本字段缺失时自动补（老库没跑迁移脚本也能入库，不静默丢版本信息）
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name='knowledge_documents' "
                "AND column_name='doc_key'"
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "ALTER TABLE knowledge_documents "
                    "ADD COLUMN doc_key VARCHAR(64) DEFAULT NULL, "
                    "ADD COLUMN revision VARCHAR(32) DEFAULT NULL, "
                    "ADD COLUMN validity VARCHAR(16) NOT NULL DEFAULT 'permanent', "
                    "ADD COLUMN effective_from BIGINT NOT NULL DEFAULT 0, "
                    "ADD COLUMN effective_to BIGINT NOT NULL DEFAULT 0, "
                    "ADD INDEX idx_knowledge_doc_key_time (doc_key, effective_from)"
                )
            cur.execute(
                """
                INSERT INTO knowledge_documents
                    (source, title, doc_type, total_chunks, total_chars, channel,
                     doc_key, revision, validity, effective_from, effective_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    title=VALUES(title), doc_type=VALUES(doc_type),
                    total_chunks=VALUES(total_chunks), total_chars=VALUES(total_chars),
                    channel=VALUES(channel), doc_key=VALUES(doc_key),
                    revision=VALUES(revision), validity=VALUES(validity),
                    effective_from=VALUES(effective_from), effective_to=VALUES(effective_to),
                    update_time=NOW()
                """,
                (source, title, doc_type, total_chunks, total_chars, channel,
                 doc_key, revision, validity, effective_from, effective_to),
            )
        conn.commit()
    except Exception as e:
        print(f"  [mysql] 镜像失败: {e}")
    finally:
        conn.close()


def _date_ts(value, end_of_day: bool = False) -> int:
    """日期 → 秒级时间戳（东八区）。空/非法 → 0（= 未标注，运行期按长期处理）"""
    import datetime as _dt

    if value in (None, "", 0):
        return 0
    tz = _dt.timezone(_dt.timedelta(hours=8))
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        dt = _dt.datetime(value.year, value.month, value.day, tzinfo=tz)
    else:
        s2 = str(value).strip().replace("/", "-").replace(".", "-")[:10]
        try:
            dt = _dt.datetime.strptime(s2, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            return 0
    ts = int(dt.timestamp())
    return ts + 86399 if end_of_day else ts


def channel_of(category: str, filename: str) -> str:
    """渠道自动判定：xianyu 系 → 闲鱼专属 KB；其余 → 商城"""
    return "xianyu" if ("xianyu" in category or "xianyu" in filename) else "mall"


def main() -> int:
    force = "--force" in sys.argv
    # schema 变更（新增标签/有效期字段）后旧 collection 不兼容，需显式重建
    recreate = "--recreate" in sys.argv
    extract_images = os.environ.get("INGEST_EXTRACT_IMAGES", "1") == "1"
    only_file = None
    if "--file" in sys.argv:
        only_file = sys.argv[sys.argv.index("--file") + 1]

    if recreate:
        try:
            from pymilvus import MilvusClient

            _mc = MilvusClient(uri=MILVUS_URI)
            if _mc.has_collection(COLLECTION):
                _mc.drop_collection(COLLECTION)
                print(f"[recreate] 已删除旧 collection '{COLLECTION}'（字段结构变更）")
        except Exception as e:
            print(f"[recreate] 删除失败: {e}")
        state_reset = {}
    else:
        state_reset = None

    state = state_reset if state_reset is not None else load_state()
    files = sorted(KB_DIR.glob("*.*")) if KB_DIR.exists() else []
    if only_file:
        files = [KB_DIR / only_file]
    files = [f for f in files if f.suffix.lower() in (".json", ".pdf", ".md", ".markdown", ".txt")]
    # README / 说明文档是给人看的，不入库（否则会变成可检索的"维护手册"污染答案）
    files = [f for f in files if f.name.lower() not in ("readme.md", "readme.markdown", "readme.txt")]
    if not files:
        print(f"无可入库文件: {KB_DIR}")
        return 1

    total_points = total_skip = 0
    for f in files:
        if not f.exists():
            print(f"跳过（不存在）: {f.name}")
            continue
        try:
            category, items = parse_file(f, extract_images=extract_images)
        except Exception as e:
            print(f"{f.name}: 解析失败 {type(e).__name__}: {e}")
            continue
        from doc_parsers import read_front_matter

        fm = read_front_matter(f) if f.suffix.lower() in (".md", ".markdown") else {}
        doc_type = str(fm.get("doc_type") or "doc")
        doc_tags = fm.get("tags") or []
        if isinstance(doc_tags, str):
            doc_tags = [t.strip() for t in doc_tags.replace("，", ",").split(",")]
        validity = str(fm.get("validity") or ("temporary" if fm.get("effective_to") else "permanent"))
        eff_from = _date_ts(fm.get("effective_from"))
        # 只存当天 00:00；"截止日含当天"的 +86399 由运行期 kb_meta 统一处理，
        # 两边都加会多出 1 天（曾出现"有效期至 2026-12-01"的错位）
        eff_to = _date_ts(fm.get("effective_to"))
        doc_title = str(fm.get("title") or f.stem)
        issuer = str(fm.get("issuer") or "")
        version = str(fm.get("version") or "")
        # 主题键：同一政策/商品的多版本共用；不填则用文件名（等于"各自独立"）
        doc_key = str(fm.get("doc_key") or f.stem)
        revision = str(fm.get("revision") or version)
        # 未标注有效期时打印出来，避免"忘了标"被当成长期政策
        if not fm:
            print(f"{f.name}: 无 front matter（按长期有效处理，建议补标签）")

        channel = channel_of(str(category), f.name)
        new_points: list[dict] = []
        doc_hashes: list[str] = []

        # 增量判断以**整篇文档**为单位，不是按 item。
        # 为什么：下面 upsert 前会 delete(source like "stem%") 把整篇旧 chunk 删掉，
        # 若让"内容未变的 item"走跳过，它对应的旧 chunk 已被删、新的又没插 ——
        # 静默丢块。实盘：只改 front matter + 加一节，入库后 5 块变 1 块。
        changed = force or any(
            state.get(it["source"]) != content_hash(it.get("title", ""), it.get("text", ""))
            for it in items
        )
        if not changed:
            total_skip += len(items)
            print(f"{f.name}: 无新增（channel={channel}）")
            continue

        for it in items:
            source = it["source"]
            title = it.get("title", "")
            text = it.get("text", "")
            meta = it.get("meta", {}) or {}
            chash = content_hash(title, text)
            doc_hashes.append(chash)
            chunks = semantic_chunks(text, max_chars=CHUNK_MAX_CHARS)
            for ci, piece in enumerate(chunks):
                new_points.append({
                    "id": str(uuid4()),
                    "payload": {
                        "channel": channel,
                        "category": str(category),
                        "doc_type": doc_type,
                        "source": source,
                        # 有 front matter 的标题时用文档级标题（section 另有字段），
                        # 否则沿用"文件名 + 章节"的老格式（历史文档兼容）
                        "title": doc_title if fm.get("title") else title,
                        # 2026-11 标签 + 有效期（政策分长期/临时；检索按此过滤过期条目）
                        # str() 防御：YAML 裸数字 tag（如 618）会被解析成 int
                        "tags": ",".join(str(t) for t in doc_tags),
                        "validity": validity,
                        "effective_from": eff_from,
                        "effective_to": eff_to,
                        "issuer": issuer,
                        "version": version,
                        # 版本化：doc_key 定位主题，doc_source 定位具体版本文档
                        "doc_key": doc_key,
                        "revision": revision,
                        "doc_source": f.name,
                        "text": piece,
                        "chunk_index": ci,
                        "section": str(meta.get("section", "")),
                        "page": meta.get("page"),
                        "kind": str(meta.get("kind", "text")),
                        "content_hash": chash,
                    },
                })
                state[source] = chash

        if not new_points:
            print(f"{f.name}: 无新增（channel={channel}）")
            continue

        print(f"{f.name}: 新增 {len(new_points)} points，channel={channel} "
              f"doc_type={doc_type} validity={validity} "
              f"tags={doc_tags or '-'} "
              f"(kinds: {sorted({p['payload']['kind'] for p in new_points})})")
        texts = [p["payload"]["text"] for p in new_points]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 10):
            vectors.extend(embed_texts(texts[i:i+10]))
        for p, v in zip(new_points, vectors):
            p["vector"] = v

        # 内容变更后重灌：先删该文档的旧 chunk 再插新的。
        # 为什么必须有这一步：chunk id 是每次生成的 uuid，upsert 只做"新增或覆盖同 id"，
        # 同一文档改动后会留下旧 chunk —— 检索时同一段条款出现两次，引用编号错乱。
        try:
            from pymilvus import MilvusClient

            _mc = MilvusClient(uri=MILVUS_URI)
            _mc.delete(collection_name=COLLECTION, filter=f'source like "{f.stem}%"')
        except Exception as e:
            print(f"  [milvus] 清理旧 chunk 失败（可能出现重复）: {e}")

        if vector_upsert(new_points):
            total_points += len(new_points)
            print(f"  ✅ upsert {len(new_points)} points → {COLLECTION}")
            mirror_mysql(
                source=f.name, title=doc_title, doc_type=doc_type,
                total_chunks=len(new_points), total_chars=sum(len(t) for t in texts),
                channel=channel, doc_key=doc_key, revision=revision, validity=validity,
                effective_from=eff_from, effective_to=eff_to,
            )
        else:
            for it2 in items:
                state.pop(it2["source"], None)  # 失败不清 state（下次重灌）
            _ = doc_hashes

    save_state(state)
    print(f"\n完成: 新入库 points={total_points}, 跳过(未变更)={total_skip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
