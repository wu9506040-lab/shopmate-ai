"""
向量库客户端 —— 双后端（Milvus standalone / Redis 8 Query Engine）。

业务代码只依赖本模块：ensure_collection / upsert_points / search /
delete_points / delete_source_points / get_collection_info / health_check /
get_client。集合名由 settings.VECTOR_COLLECTION 指定。

后端由 settings.VECTOR_BACKEND 切换（"milvus" 默认 / "redis"），
两后端对外语义完全一致：
- score 一律为**余弦相似度**（1.0=完全相同，越大越像；实测确认 Milvus COSINE
  返回相似度而非距离），score_threshold 是"最低相似度线"，两侧逻辑相同
- payload 为标量字段 dict（Milvus dynamic field / Redis hash field 等价）

Redis 后端物理结构：key = {VECTOR_REDIS_PREFIX}{id}（hash），
索引 = {VECTOR_REDIS_PREFIX}idx（HNSW DIM=1024 COSINE M=16 efC=128，
与 Milvus 参数对齐）。collection_name 仅作逻辑名，任何取值都指向同一物理索引。

Milvus 集合字段：id(varchar pk) / vector(FLOAT_VECTOR 1024 COSINE) / text /
channel / source / page / section / title / doc_type / doc_key / category
（其余键进 dynamic field）。
"""
from __future__ import annotations

import json
import logging
import re
import struct
import time
from typing import Any, Dict, List, Optional
from types import SimpleNamespace

from pymilvus import MilvusClient, DataType

from app.core.config import settings
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

MILVUS_URI = settings.MILVUS_URI
# 集合名由配置指定
COLLECTION_NAME = settings.VECTOR_COLLECTION
VECTOR_SIZE = 1024

_client: Optional[Any] = None

vector_db_breaker = CircuitBreaker(
    name="milvus",
    failure_threshold=3,
    recovery_timeout=30.0,
    expected_exceptions=(Exception,),
)

# Milvus 显式标量字段（其余 payload 键进 dynamic field）
_SCALAR_FIELDS = ("text", "channel", "source", "page", "section", "title",
                  "doc_type", "category")

# Redis 后端建索引时的 TAG 字段（精确匹配用：channel 过滤 / source 删除 /
# doc_key 切片精确读取）
_REDIS_TAG_FIELDS = ("channel", "source", "doc_key", "doc_type", "category")


def _redis_enabled() -> bool:
    return settings.VECTOR_BACKEND == "redis"


# =============================================================
# 公共：断路器 + metrics 包装（两后端共用）
# =============================================================
def _with_breaker(fn, fallback, op: str):
    try:
        result = vector_db_breaker.call(fn)
        metrics.inc_vector_search("success")
        return result
    except CircuitOpenError:
        logger.warning(f"向量库 {op} 降级（断路器开路），返回降级值")
        metrics.inc_vector_search("fallback_open")
        return fallback
    except Exception:
        metrics.inc_vector_search("error")
        raise


# =============================================================
# Redis 8 后端实现
# =============================================================
_REDIS_CONN = None


def _redis():
    """Redis 向量后端专用连接。

    不能复用缓存客户端：那边 decode_responses=True，而向量是二进制。
    """
    global _REDIS_CONN
    if _REDIS_CONN is None:
        import redis as _redis_mod

        _REDIS_CONN = _redis_mod.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,
            socket_timeout=30.0,
            socket_connect_timeout=5.0,
        )
        _REDIS_CONN.ping()
        logger.info(f"redis 向量后端连接: {settings.REDIS_URL} prefix={settings.VECTOR_REDIS_PREFIX}")
    return _REDIS_CONN


def _redis_prefix(collection_name: Optional[str] = None) -> str:
    # 单物理索引：collection_name 仅是逻辑名（对外契约），物理 key 统一前缀
    return settings.VECTOR_REDIS_PREFIX


def _redis_index_name(collection_name: Optional[str] = None) -> str:
    return f"{_redis_prefix(collection_name)}idx"


def _encode_vec(v) -> bytes:
    return struct.pack(f"<{len(v)}f", *[float(x) for x in v])


def _decode_vec(b) -> List[float]:
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _tag_escape(s: str) -> str:
    """TAG 精确匹配转义：非 \\w 字符（含 / . - 空格等路径字符）全部加反斜杠。

    source 是文件路径（如 docs/退货政策_v2.pdf），不转义会被 TAG 分隔符切开。
    中文属 \\w 不转义。
    """
    return re.sub(r"(\W)", r"\\\1", str(s))


def _payload_to_hmap(payload: dict) -> Dict[str, str]:
    """payload → hash mapping。None→""（对齐 Milvus nullable 行为），
    嵌套结构 json.dumps 落库。"""
    mapping: Dict[str, str] = {}
    for k, v in (payload or {}).items():
        if v is None:
            v = ""
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        mapping[k] = str(v)
    return mapping


def _doc_to_payload(data: dict, keep_vector: bool = False) -> dict:
    """FT.SEARCH 字段 → payload dict（bytes→str、vec 剔除或反解、page 还原 int）。

    注意 vec 判断必须在 bytes.decode 之前，否则向量二进制被当文本解码炸掉。
    """
    out: dict = {}
    vec = None
    for k, v in data.items():
        if isinstance(k, bytes):
            k = k.decode()
        if k in ("id", "score", "payload"):  # 框架注入键，非业务字段
            continue
        if k == "vec":
            vec = v
            continue
        if isinstance(v, bytes):
            v = v.decode()
        if k == "page" and isinstance(v, str) and v.isdigit():
            v = int(v)
        out[k] = v
    if keep_vector and vec:
        out["vector"] = _decode_vec(vec)
    return out


def _ft_search_raw(idx: str, qstr: str, params: Optional[Dict[str, Any]],
                   limit: int, offset: int = 0):
    """裸调 FT.SEARCH 并解析协议。

    不用 redis-py 的 ft().search()：bytes 连接下它返回的 Document 混入
    int 键与 payload=None，dict(doc) 会炸。协议（实验验证）：
    [total, key1, fields1, key2, fields2, ...]，KNN 结果已按 score 升序。
    """
    conn = _redis()
    argv: List[Any] = ["FT.SEARCH", idx, qstr,
                       "LIMIT", str(offset), str(limit)]
    if params:
        flat: List[Any] = []
        for k, v in params.items():
            flat.extend([k, v])
        argv.extend(["PARAMS", str(len(flat))])
        argv.extend(flat)
    argv.extend(["DIALECT", "2"])
    raw = conn.execute_command(*argv)
    total = int(raw[0])
    out = []
    i = 1
    while i < len(raw):
        key = raw[i].decode() if isinstance(raw[i], bytes) else raw[i]
        fields = raw[i + 1] if i + 1 < len(raw) else []
        d: dict = {}
        if fields:
            for j in range(0, len(fields), 2):
                f = fields[j].decode() if isinstance(fields[j], bytes) else fields[j]
                d[f] = fields[j + 1]
        out.append((key, d))
        i += 2
    return total, out


def _redis_search(query_vector, top_k, name, score_threshold, channel) -> List[Dict[str, Any]]:
    blob = _encode_vec(query_vector)
    # 无过滤时语法必须带 * 前缀；有过滤时 (@field:{val})=>[KNN ...]。
    # EF_RUNTIME 对齐 efConstruction（128）：默认 10 在图新建/小库时召回不稳
    if channel:
        qstr = (f"(@channel:{{{_tag_escape(channel)}}})"
                f"=>[KNN $k @vec $qv EF_RUNTIME 128 AS score]")
    else:
        qstr = "*=>[KNN $k @vec $qv EF_RUNTIME 128 AS score]"
    idx = _redis_index_name(name)
    _, rows = _ft_search_raw(idx, qstr, {"k": str(top_k), "qv": blob},
                             limit=top_k, offset=0)
    prefix = _redis_prefix(name)
    hits_out = []
    for key, fields in rows:
        # 关键语义对齐（实测确认）：Milvus COSINE 返回的是**相似度**（1.0=完全
        # 相同，越大越像），业务阈值 0.28~0.48 是"最低相似度线"；Redis KNN 的
        # score 是**距离**（0=完全相同）。换算 sim = 1 - distance 对齐 Milvus。
        sim = 1.0 - float(fields.get("score", 1.0))
        if score_threshold is not None and sim < score_threshold:
            continue
        rid = key[len(prefix):] if key.startswith(prefix) else key
        hits_out.append({
            "id": rid,
            "score": sim,
            "payload": _doc_to_payload(fields),
        })
    # 相似度语义下越大越像 → 降序（对齐 Milvus top1=最像）
    hits_out.sort(key=lambda h: h["score"], reverse=True)
    return hits_out


def _redis_upsert(points: List[Any], name: str) -> int:
    conn = _redis()
    prefix = _redis_prefix(name)
    idx = _redis_index_name(name)
    # 记录写入前索引 total，区分新增/覆盖（覆盖不增加 total）
    total_before, _ = _ft_search_raw(idx, "*", params=None, limit=0, offset=0)
    pipe = conn.pipeline(transaction=False)
    rows = []
    existing = 0
    for pt in points:
        if isinstance(pt, dict):
            pid, vec = str(pt.get("id")), pt.get("vector")
            payload = pt.get("payload") or {}
        else:  # SimpleNamespace 兼容
            pid, vec = str(pt.id), pt.vector
            payload = getattr(pt, "payload", {}) or {}
        if conn.exists(f"{prefix}{pid}"):
            existing += 1
        mapping = {"vec": _encode_vec(vec), "rid": pid}  # rid: 索引内查 id 用
        mapping.update(_payload_to_hmap(payload))
        pipe.hset(f"{prefix}{pid}", mapping=mapping)
        rows.append(pid)
    pipe.execute()
    logger.info(f"redis upsert: {len(rows)} points to '{name}'")
    # Redis 8 索引更新异步：写后轮询可见性，防"灌库后立即搜不到新点"
    _wait_vector_visible(points, name,
                         total_target=total_before + len(rows) - existing)
    return len(rows)


def _redis_delete_ids(point_ids: List, name: str) -> int:
    conn = _redis()
    prefix = _redis_prefix(name)
    keys = [f"{prefix}{p}" for p in point_ids]
    if keys:
        conn.delete(*keys)
        # 索引异步更新：轮询确认从索引消失，防删除后立即搜到旧点
        _wait_deleted(
            [(f"rid", str(p)) for p in point_ids], name)
    logger.info(f"redis delete: {len(keys)} points from '{name}'")
    return len(point_ids)


def _wait_vector_visible(points: List[Any], name: str,
                         total_target: Optional[int] = None,
                         timeout: float = 5.0) -> None:
    """轮询直到写入的向量被 KNN 索引稳定可见。两段判定：

    1. total 收敛：FT.SEARCH '*' 的 total 达到目标（索引构建完成的硬信号，
       对 DROPINDEX 后重建等场景尤其重要）
    2. score 判定：写入向量 KNN top1 score < 0.05——索引里已存在语义相同的
       点。不校验具体 id：同向量多点（重灌文档场景）互为 top1，校验 id 会
       互相卡死判定。
    """
    idx = _redis_index_name(name)
    batch_vecs = [p.get("vector") if isinstance(p, dict) else p.vector
                  for p in points]
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = True
        if total_target is not None:
            total, _ = _ft_search_raw(idx, "*", params=None, limit=0, offset=0)
            if total < total_target:
                ok = False
        if ok:
            for vec in batch_vecs:
                try:
                    _, rows = _ft_search_raw(
                        idx, "*=>[KNN $k @vec $qv AS score]",
                        {"k": "1", "qv": _encode_vec(vec)}, limit=1, offset=0)
                except Exception:
                    ok = False
                    break
                if not rows or float(rows[0][1].get("score", 1.0)) > 0.05:
                    ok = False
                    break
        if ok:
            return
        time.sleep(0.05)
    logger.warning(f"redis 索引可见性等待超时({timeout}s): {len(batch_vecs)} 点")


def _wait_deleted(field_pairs: List, name: str, timeout: float = 3.0) -> None:
    """轮询直到删除的条目从索引消失。field_pairs: [(tag_field, value), ...]"""
    idx = _redis_index_name(name)
    deadline = time.time() + timeout
    pending = list(field_pairs)
    while pending and time.time() < deadline:
        still = []
        for field, val in pending:
            try:
                _, rows = _ft_search_raw(
                    idx, f"(@{field}:{{{_tag_escape(val)}}})",
                    params=None, limit=1, offset=0)
                if rows:
                    still.append((field, val))
            except Exception:
                still.append((field, val))
        pending = still
        if pending:
            time.sleep(0.05)
    if pending:
        logger.warning(f"redis 删除可见性等待超时({timeout}s): {pending}")


def _redis_delete_source(source: str, name: str) -> int:
    """delete-before-write 原语：按 source 精确圈 key → 批量删。"""
    _, rows = _ft_search_raw(
        _redis_index_name(name),
        f"(@source:{{{_tag_escape(source)}}})",
        params=None, limit=10000, offset=0,
    )
    keys = [k for k, _ in rows]
    deleted = 0
    if keys:
        deleted = _redis().delete(*keys)
        # 索引异步更新：轮询确认该 source 从索引消失，防删旧后立即搜到旧点
        _wait_deleted([("source", source)], name)
        logger.info(f"redis delete_source: source={source} 预清旧点 {deleted} 个")
    return int(deleted or 0)


def _redis_info(name: str) -> Dict[str, Any]:
    conn = _redis()
    raw = conn.execute_command("FT.INFO", _redis_index_name(name))
    d = {raw[i].decode(): raw[i + 1] for i in range(0, len(raw), 2)}
    num_docs = d.get(b"num_docs") or d.get("num_docs") or 0
    num_docs = int(num_docs)
    return {
        "name": name,
        "vectors_count": num_docs,
        "points_count": num_docs,
        "status": "loaded",
        "vector_size": VECTOR_SIZE,
    }


class RedisVectorClient:
    """get_client() 的 Redis 后端透传层。

    方法签名与 MilvusStoreClient 对齐：scroll / retrieve / get_collections /
    create_collection_if_missing / query（原 _raw.query 的等价封装）。
    """

    def __init__(self):
        self._collection = COLLECTION_NAME
        self._dim = VECTOR_SIZE

    def scroll(self, collection_name: str, limit: int = 100,
               offset=None, with_payload: bool = True,
               with_vectors: bool = False):
        start = offset if isinstance(offset, int) else 0
        _, rows = _ft_search_raw(_redis_index_name(collection_name), "*",
                                 params=None, limit=limit, offset=start)
        prefix = _redis_prefix(collection_name)
        records = []
        for key, fields in rows:
            rid = key[len(prefix):] if key.startswith(prefix) else key
            payload = _doc_to_payload(fields, keep_vector=with_vectors)
            vec = payload.pop("vector", None)
            records.append(SimpleNamespace(id=rid, payload=payload, vector=vec))
        next_offset = start + len(records) if len(records) == limit else None
        return records, next_offset

    def retrieve(self, collection_name: str, ids: List[str],
                 with_payload: bool = True):
        conn = _redis()
        prefix = _redis_prefix(collection_name)
        records = []
        for i in ids:
            data = conn.hgetall(f"{prefix}{i}")
            if not data:
                continue
            payload = _doc_to_payload(data, keep_vector=False)
            records.append(SimpleNamespace(id=i, payload=payload, vector=None))
        return records

    def query(self, collection_name: str, filter: str,
              output_fields=None, limit: int = 100):
        """Milvus query 等价：支持单字段精确匹配 'field == "value"'。

        业务里仅 policy_service 用它按 doc_key 取切片；TAG 精确匹配语义相同。
        """
        m = re.match(r'(\w+)\s*==\s*"([^"]*)"', filter or "")
        if not m:
            raise ValueError(f"redis query 仅支持 field == \"value\" 形式: {filter!r}")
        field, val = m.group(1), m.group(2)
        if field not in _REDIS_TAG_FIELDS:
            raise ValueError(f"redis query 字段 {field} 未建 TAG 索引: {_REDIS_TAG_FIELDS}")
        _, rows = _ft_search_raw(
            _redis_index_name(collection_name),
            f"(@{field}:{{{_tag_escape(val)}}})",
            params=None, limit=limit, offset=0,
        )
        prefix = _redis_prefix(collection_name)
        out_rows = []
        for key, fields in rows:
            rid = key[len(prefix):] if key.startswith(prefix) else key
            row = {"id": rid}
            row.update(_doc_to_payload(fields))
            out_rows.append(row)
        return out_rows

    def get_collections(self):
        return [SimpleNamespace(name=settings.VECTOR_COLLECTION)]

    def create_collection_if_missing(self, collection_name: str, dim: int) -> None:
        from redis.commands.search.field import TagField, TextField, NumericField
        from redis.commands.search.indexDefinition import IndexDefinition, IndexType
        from redis.commands.search.field import VectorField

        conn = _redis()
        idx = _redis_index_name(collection_name)
        try:
            conn.ft(idx).info()
            return  # 已存在
        except Exception:
            pass
        fields = [
            VectorField("vec", "HNSW", {
                "TYPE": "FLOAT32", "DIM": dim,
                "DISTANCE_METRIC": "COSINE", "M": 16, "EF_CONSTRUCTION": 128,
            }),
            TagField("rid"),  # id 入索引：删除后按 rid 轮询确认从索引消失
            *[TagField(f) for f in _REDIS_TAG_FIELDS],
            TextField("title"), TextField("section"), TextField("text"),
            NumericField("page"),
        ]
        conn.ft(idx).create_index(
            fields,
            definition=IndexDefinition(prefix=[_redis_prefix(collection_name)],
                                       index_type=IndexType.HASH),
        )
        logger.info(f"redis 向量索引 '{idx}' 创建成功 (dim={dim})")


# =============================================================
# 连接管理
# =============================================================
def get_client():
    """获取向量库客户端（单例，按 VECTOR_BACKEND 分发）"""
    global _client
    if _client is None:
        if _redis_enabled():
            _client = RedisVectorClient()
            logger.info(f"向量后端: redis, collection={COLLECTION_NAME}")
        else:
            raw = MilvusClient(uri=MILVUS_URI, timeout=30.0)
            _client = MilvusStoreClient(raw, COLLECTION_NAME, VECTOR_SIZE)
            logger.info(f"milvus client 初始化: {MILVUS_URI}, collection={COLLECTION_NAME}")
    return _client


class MilvusStoreClient:
    """向量库操作方法封装（scroll / retrieve / upsert / delete / search）"""

    def __init__(self, raw: MilvusClient, collection: str, dim: int):
        self._raw = raw
        self._collection = collection
        self._dim = dim

    # ---------- scroll 分页（BM25 全量拉取 / source 统计） ----------
    def scroll(self, collection_name: str, limit: int = 100,
               offset=None, with_payload: bool = True,
               with_vectors: bool = False):
        """scroll → Milvus query 分页。返回 (records, next_offset)"""
        start = offset if isinstance(offset, int) else 0
        if with_vectors:
            output_fields = ["vector"]
        else:
            output_fields = ["*"]
        rows = self._raw.query(
            collection_name=collection_name,
            filter="",
            output_fields=output_fields,
            offset=start,
            limit=limit,
        )
        records = [self._row_to_record(r) for r in rows]
        next_offset = start + len(rows) if len(rows) == limit else None
        return records, next_offset

    @staticmethod
    def _row_to_record(row: dict) -> SimpleNamespace:
        """Milvus row dict → 统一 record（.id / .payload 属性）"""
        row = dict(row)
        vec = row.pop("vector", None)
        rid = row.pop("id", None)
        return SimpleNamespace(id=rid, payload=row, vector=vec)

    # ---------- retrieve 批量查询（get_document 反查） ----------
    def retrieve(self, collection_name: str, ids: List[str],
                 with_payload: bool = True):
        filter_expr = " || ".join([f'id == "{i}"' for i in ids]) if len(ids) <= 20 \
            else ""
        rows: List[dict] = []
        if filter_expr:
            rows = self._raw.query(
                collection_name=collection_name,
                filter=filter_expr,
                output_fields=["*"],
            )
        else:  # id 太多退化为逐个查
            for i in ids:
                rows.extend(self._raw.query(
                    collection_name=collection_name,
                    filter=f'id == "{i}"', output_fields=["*"],
                ))
        return [self._row_to_record(r) for r in rows]

    # ---------- query 透传（policy_service 切片精确读取） ----------
    def query(self, collection_name: str, filter: str,
              output_fields=None, limit: int = 100):
        return self._raw.query(
            collection_name=collection_name,
            filter=filter,
            output_fields=output_fields or ["*"],
            limit=limit,
        )

    # ---------- 原生客户端透传（ingest 特殊需求） ----------
    def get_collections(self):
        """返回 [SimpleNamespace(name=...)]"""
        cols = self._raw.list_collections()
        return [SimpleNamespace(name=c) for c in cols]

    def create_collection_if_missing(self, collection_name: str, dim: int) -> None:
        if self._raw.has_collection(collection_name):
            return
        schema = self._raw.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        for f in _SCALAR_FIELDS:
            if f == "page":
                schema.add_field(f, DataType.INT64, nullable=True)
            else:
                schema.add_field(f, DataType.VARCHAR, max_length=8192,
                                 nullable=True)
        index_params = self._raw.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="HNSW",
            metric_type="COSINE", params={"M": 16, "efConstruction": 128},
        )
        self._raw.create_collection(collection_name=collection_name, dimension=dim, schema=schema, index_params=index_params, id_type="varchar", primary_field_name="id", vector_field_name="vector", metric_type="COSINE")
        logger.info(f"milvus collection '{collection_name}' 创建成功 (dim={dim})")


# =============================================================
# Collection 管理（集合创建）
# =============================================================
def ensure_collection(collection_name: Optional[str] = None,
                      vector_size: int = VECTOR_SIZE) -> bool:
    name = collection_name or COLLECTION_NAME
    client = get_client()
    try:
        client.create_collection_if_missing(name, vector_size)
        return True
    except Exception:
        logger.exception(f"向量 collection '{name}' 初始化失败")
        raise


# =============================================================
# 内部工具：PointStruct-like → Milvus row
# =============================================================
def _points_to_rows(points: List[Any]) -> List[Dict[str, Any]]:
    rows = []
    for pt in points:
        if isinstance(pt, dict):
            pid, vec, payload = pt.get("id"), pt.get("vector"), pt.get("payload", {})
        row = {"id": str(pid), "vector": list(vec)}
        for k, v in (payload or {}).items():
            row[k] = v if v is not None else ""
        rows.append(row)
    return rows


# =============================================================
# 向量读写（向量读写）
# =============================================================
def upsert_points(points: List[Any], collection_name: Optional[str] = None) -> int:
    name = collection_name or COLLECTION_NAME
    return _with_breaker(
        lambda: (
            _redis_upsert(points, name)
            if _redis_enabled()
            else _milvus_upsert(points, name)
        ),
        fallback=0,
        op="upsert",
    )


def _milvus_upsert(points: List[Any], name: str) -> int:
    client = get_client()
    rows = _points_to_rows(points)
    res = client._raw.upsert(collection_name=name, data=rows)
    logger.info(f"milvus upsert: {len(rows)} points to '{name}', {res}")
    return len(rows)


def search(query_vector: List[float], top_k: int = 5,
           collection_name: Optional[str] = None,
           score_threshold: Optional[float] = None,
           channel: Optional[str] = None) -> List[Dict[str, Any]]:
    name = collection_name or COLLECTION_NAME
    return _with_breaker(
        lambda: (
            _redis_search(query_vector, top_k, name, score_threshold, channel)
            if _redis_enabled()
            else _milvus_search(query_vector, top_k, name, score_threshold, channel)
        ),
        fallback=[],
        op="search",
    )


def _milvus_search(query_vector, top_k, name, score_threshold, channel) -> List[Dict[str, Any]]:
    filter_expr = f'channel == "{channel}"' if channel else ""
    client = get_client()
    res = client._raw.search(
        collection_name=name,
        data=[list(query_vector)],
        limit=top_k,
        filter=filter_expr or "",
        output_fields=["*"],
        search_params={"metric_type": "COSINE"},
    )
    hits_out = []
    # pymilvus 返回 [ [ {id, distance, entity}, ... ] ]
    for hit in (res[0] if res else []):
        score = float(hit.get("distance", 0.0))
        if score_threshold is not None and score < score_threshold:
            continue
        entity = hit.get("entity", {}) or {}
        entity.pop("vector", None)
        hits_out.append({"id": str(hit.get("id", "")), "score": score,
                         "payload": entity})
    return hits_out


def delete_points(point_ids: List, collection_name: Optional[str] = None) -> int:
    name = collection_name or COLLECTION_NAME
    if _redis_enabled():
        return _redis_delete_ids(point_ids, name)
    client = get_client()
    expr = " || ".join([f'id == "{p}"' for p in point_ids])
    client._raw.delete(collection_name=name, filter=expr)
    logger.info(f"milvus delete: {len(point_ids)} points from '{name}'")
    return len(point_ids)


def delete_source_points(source: str, collection_name: Optional[str] = None) -> int:
    """delete-before-write 原语：按 source 清旧世代（ 2.1'）"""
    name = collection_name or COLLECTION_NAME
    if _redis_enabled():
        try:
            return _redis_delete_source(source, name)
        except Exception as e:
            logger.warning(f"redis delete_source 失败（可能无存量）: {e}")
            return 0
    client = get_client()
    try:
        res = client._raw.delete(collection_name=name, filter=f'source == "{source}"')
        deleted = res.get("delete_count", 0) if isinstance(res, dict) else 0
        if deleted:
            logger.info(f"milvus delete_source: source={source} 预清旧点 {deleted} 个")
        return int(deleted or 0)
    except Exception as e:
        logger.warning(f"milvus delete_source 失败（可能无存量）: {e}")
        return 0


# =============================================================
# 信息查询 / 健康检查（向量读写）
# =============================================================
def get_collection_info(collection_name: Optional[str] = None) -> Dict[str, Any]:
    name = collection_name or COLLECTION_NAME
    if _redis_enabled():
        return _redis_info(name)
    client = get_client()
    stats = client._raw.get_collection_stats(name)
    desc = client._raw.describe_collection(name)
    dim = None
    for f in desc.get("fields", []):
        if f.get("name") == "vector":
            dim = f.get("params", {}).get("dim")
    return {
        "name": name,
        "vectors_count": stats.get("row_count"),
        "points_count": stats.get("row_count"),
        "status": "loaded",
        "vector_size": int(dim) if dim else None,
    }


def health_check(timeout: float = 2.0) -> Dict[str, Any]:
    result = {"ok": False, "latency_ms": 0.0, "points_count": None,
              "circuit": vector_db_breaker.stats(), "error": None}
    t0 = time.perf_counter()
    try:
        info = get_collection_info()
        result["ok"] = True
        result["points_count"] = info["points_count"]
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


__all__ = ["vector_db_breaker", "get_client", "ensure_collection", "upsert_points", "search",
           "delete_points", "delete_source_points", "get_collection_info",
           "health_check", "COLLECTION_NAME", "VECTOR_SIZE"]
