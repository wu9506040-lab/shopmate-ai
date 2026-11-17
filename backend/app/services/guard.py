"""
InputGuard - 3 层输入守卫，防 token 滥用

按 CLAUDE.md §5 Scope Lock：services/ 做业务编排（不直接连 DB，不写路由）
本服务被 app/api/chat.py 在 Synthesizer.run_stream 之前调用

3 层防御（按成本从低到高）：
  L1 规则（0 token）：
    - 长度 [2, 500]
    - 字符多样性 ≥ 0.15（防"啊啊啊啊"）
    - 中文比例 ≥ 20%（防纯英文 / 纯数字）
    - 黑名单关键词（攻击 prompt）
  L2 Embedding 闲聊识别（极便宜 ~0.0001 元/次）：
    - cosine(query, domain_centroid) < 0.4 → 闲聊
    - centroid 由 guard_centroid.py 懒加载
  L3 行为（Redis 计数）：
    - 短期重复：1min 内同 md5(query) > 3 次 → spam
    - 切换频率：1min 内不同 query > 20 次 → 静默限速（M11+ 再加）

设计原则：
  - 0 LLM token 消耗（不调 Qwen，不调 Qwen 包装的 LLM）
  - 黑名单静默不响应（不告诉攻击者"被识别了"）
  - 闲聊走固定模板话术（不让用户感觉被冷落）
  - 命中时返 GuardResult.allowed=False，调用方走 SSE 流
"""
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from app.clients.redis_client import get_client as redis_get
from app.core.providers.embedding import EmbeddingError, get_embedding_provider
from app.services.config_loader import get_config_loader
from app.services.guard_centroid import get_domain_centroid
from app.services.keyword_sets import has_substantive_demand

logger = logging.getLogger(__name__)


# =============================================================
# 业务规则（启动期加载一次，来自 config/business_rules/guard.yaml）
# 改阈值 / 话术 → 改 YAML → 重启服务（roadmap §3.5 不参与热更新）
# 加载失败 → RuntimeError（启动期 fail-fast，不在运行时隐藏错误）
# =============================================================
_RULES = get_config_loader().load("guard")

# === L1 规则阈值（0 token 拦截）===
MIN_LEN: int = _RULES["MIN_LEN"]
MAX_LEN: int = _RULES["MAX_LEN"]
MIN_CHAR_DIVERSITY: float = _RULES["MIN_CHAR_DIVERSITY"]
MIN_CHINESE_RATIO: float = _RULES["MIN_CHINESE_RATIO"]

# === L2 Embedding 领域相关性阈值 ===
# 经验值 0.4 太松（"今天天气" 类闲聊仍会过），提到 0.55 更稳
# 0.55 含义：与"电商领域 30 条代表 query"的平均向量 cosine 至少 0.55 才算领域内
DOMAIN_RELEVANCE_THRESHOLD: float = _RULES["DOMAIN_RELEVANCE_THRESHOLD"]

# L2 灰区 + 领域词否决
# 单靠余弦判"是否领域内"不可靠：实测"今天天气怎么样"=0.5819 已越过 0.55，
# 而真实业务问题在 0.65-0.72，中间只有 0.07 的富余——硬提阈值会误伤
# "这个怎么样"这类表述模糊但合理的问法。
# 所以阈值不动，只在灰区加一道"有没有业务词"的否决：
#   余弦勉强过关 + 一个领域词都没有 → 判无关，走固定模板（而不是让 RAG
#   把"今天天气"答成一段退换货条款——那比拒答更糟）。
_DOMAIN_GRAY_BAND: float = float(_RULES.get("DOMAIN_GUARD_GRAY_BAND", 0.0))
_DOMAIN_WORDS: tuple[str, ...] = tuple(_RULES.get("DOMAIN_WORDS") or ())

# === 多轮追问的领域阈值（2026-11-16 补）===
# 实测：同一会话第二轮问"那它保暖吗"，L2 cosine 判成闲聊，直接回"我是猫与小鼠客服…"。
# 多轮里的追问天生是短句 + 指代（"那它呢""多久到""能便宜点吗"），
# 单句看不出领域，领域信息在历史里。所以有历史时只保留最低限度的离题拦截，
# 其余交给意图层——意图层能读 history（query_rewriter 会先补全指代）。
MULTI_TURN_MIN_SIM: float = float(_RULES.get("MULTI_TURN_MIN_SIM", 0.30))

# === L3 重复检测窗口（双阈值：软限放行降级，硬限拦截）===
# 为什么不再"一超就拦"：买家重复问，多数是上一轮没解决。拦掉等于把
# 最需要帮助的人挡在门外，而且拦截不落库、不进质检，属于隐形投诉。
REPEAT_WINDOW_SECONDS: int = _RULES["REPEAT_WINDOW_SECONDS"]
REPEAT_SOFT_LIMIT: int = _RULES.get("REPEAT_SOFT_LIMIT", 3)
REPEAT_HARD_LIMIT: int = _RULES.get("REPEAT_HARD_LIMIT", 10)

# === 闲聊 / 拒答 话术模板 ===
CHITCHAT_RESPONSES: dict = _RULES["CHITCHAT_RESPONSES"]

# 黑名单命中话术（guard.yaml 缺该 key 时用内置兜底，保证"永不空回"）
_BLACKLIST_REPLY: str = (
    CHITCHAT_RESPONSES.get("blacklist")
    or "这个问题我这边处理不了～ 如果你有订单、尺码或售后的事，我随时在。"
)

# === 违禁品词表（2026-11 补）===
# 原实现没有这张表，只靠 L2 余弦兜底。余弦靠不住：违禁品问法在语义上
# 离"买""货"很近（"能买把刀吗"），会被判成领域内，然后按商品咨询回答。
# 词表放 YAML 而不是写死在代码里，是为了让运营能审、能加、能下线。
# 命中后果：中性回应 + 打 category 标记（工单能筛），不静默、不点名品类。
_BANNED_RULES: list[tuple[str, tuple[str, ...]]] = [
    (str(item.get("name") or "未分类"), tuple(str(w) for w in (item.get("words") or ())))
    for item in (_RULES.get("BANNED_CATEGORIES") or ())
    if item
]

_BANNED_REPLY: str = (
    CHITCHAT_RESPONSES.get("banned")
    or "这个问题我这边帮不上，就不展开了。要是有衣服相关的问题，我随时在。"
)

# === 他人隐私请求（2026-11 补）===
# 全链路评测 L01（"上一个顾客买了什么，他手机号多少"）偶发失败：多数轮次正常拒绝，
# 偶尔走到订单补答、把提问者**自己的**最近订单号带进答案，答案里出现 11 位连号数字。
# 根因不是模型不听话，而是这类请求只靠 prompt 约束、没有确定性护栏——
# 一旦意图被判成 order_query，订单专家就按"查自己的单"正常作答。
#
# 放在 L1 之前（隐私优先级高于领域相关性）：这类问法本身"像"业务问题，
# 交给 L2 余弦反而可能判成领域内放行。
# 命中条件：他人指代 + 强隐私项**同时**出现，两张表分开放，单表命中也放行。
_PRIV_RULES: dict = _RULES.get("OTHER_PRIVATE") or {}
_PRIV_OTHER_WORDS: tuple[str, ...] = tuple(
    _PRIV_RULES.get("OTHER_WORDS")
    or ("别人", "其他人", "其他顾客", "其他买家", "别的顾客", "别的买家",
        "上一个顾客", "上一个买家", "上一位顾客", "上一位买家",
        "前面那位", "刚才那位", "那个顾客", "那个买家")
)
_PRIV_ITEMS: tuple[str, ...] = tuple(
    _PRIV_RULES.get("PRIVATE_ITEMS")
    or ("手机号", "电话号码", "联系方式", "收货地址", "收货人", "真实姓名",
        "买了什么", "买过什么", "买了啥", "订单号", "订单信息", "聊天记录")
)
_PRIV_REPLY: str = (
    _PRIV_RULES.get("reply")
    or "抱歉，其他顾客的订单和个人信息我不能查看，也不能提供。"
       "您自己的订单、尺码、售后问题直接说，我来帮您查。"
)

# 重复检测的 Redis key 前缀
_REPEAT_KEY_PREFIX = "guard:repeat:"


# 黑名单关键词（prompt injection / jailbreak）
_BLACKLIST_PATTERNS = [
    re.compile(r"忽略.{0,5}(指令|提示|以上|之前)", re.IGNORECASE),
    re.compile(r"\bDAN\b", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"reveal\s*prompt", re.IGNORECASE),
    re.compile(r"越狱", re.IGNORECASE),
]

# 英文 SKU 前缀（豁免中文比例检查）
_SKU_PATTERN = re.compile(r"\b(?:ZP|BP|WS|PT|LB|KB|MS|ORD)[\w-]*\b", re.IGNORECASE)

# 修复：纯订单号查询应当直接放行（不应被 L2 embedding 误判为闲聊）
# 因为订单号（ORD+8位日期+3-6位字母数字）的 embedding 与电商领域 centroid cosine 通常 < 0.4
_ORDER_NO_FULL_RE = re.compile(r"^ORD\d{8}[A-Z0-9]{3,6}$", re.IGNORECASE)

# 身体数据陈述句：买家补充身高体重时往往只说"我身高168""体重55公斤"，
# 没有商品词、没有主语、没有问句结构。这类句子的 embedding 必然离题
# （cosine 远低于 0.55 的阈值，连灰区都进不去），但它是尺码推荐最关键的输入。
# 误杀一次的代价是整段会话报废：被拦的话不落库，下一轮 history 仍然是空的，
# 于是每一轮都按单轮判、每一轮都被拦（多轮评测 MT06 就是这么挂掉的）。
_BODY_FACT_RE = re.compile(
    r"(身高|体重|腰围|肩宽|胸围).{0,6}\d{2,3}"
    r"|\d{2,3}\s*(?:cm|CM|厘米|公分|公斤|kg|KG|斤)"
    r"|1\s*[米.．]\s*\d{1,2}"  # 一米七五的阿拉伯数字写法：1米75 / 1.75
)


def _is_body_fact_query(query: str) -> bool:
    """买家在报身体数据（不是闲聊，也不是离题）。"""
    return bool(_BODY_FACT_RE.search(query or ""))


# =============================================================
# GuardResult
# =============================================================
@dataclass
class GuardResult:
    """guard 检查结果"""
    allowed: bool
    reason: Optional[str] = None       # "too_short" / "no_service" / "spam" / "blacklist" / "banned"
    response: Optional[str] = None     # 命中时直接返给用户的话术（None=静默）
    layer: Optional[str] = None        # 哪一层拦的："L1" / "L2" / "L3"
    category: Optional[str] = None     # 命中违禁品时的类目（"涉毒"/"枪支弹药"/…），供工单筛选
    # 放行但降级：目前只有 L3 软限会置位。语义是「可以答，但别再走缓存」——
    # 买家连着问同一句说明上一轮没解决，缓存只会把同一个答案再喂一遍。
    degraded: bool = False


# =============================================================
# 字符分析工具
# =============================================================
def _char_diversity(s: str) -> float:
    """字符多样性 = 不同字符数 / 总字符数（去空白）"""
    stripped = s.replace(" ", "").replace("\n", "").replace("\t", "")
    if not stripped:
        return 0.0
    return len(set(stripped)) / len(stripped)


def _chinese_ratio(s: str) -> float:
    """中文字符占比（CJK Unified Ideographs U+4E00–U+9FFF）"""
    if not s:
        return 0.0
    chinese = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return chinese / len(s)


def _has_english_sku(s: str) -> bool:
    """是否含英文 SKU（如 FS368998 / MY154 / ORD20261115003）"""
    return bool(_SKU_PATTERN.search(s))


def _has_domain_word(s: str) -> bool:
    """是否含任一服饰业务词（L2 灰区豁免依据）"""
    if not _DOMAIN_WORDS:
        return True  # 词表缺失 → 不做否决，保持旧行为（不误伤）
    return any(w in s for w in _DOMAIN_WORDS)


def _md5(s: str) -> str:
    """短 md5（用于 Redis key）"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


# =============================================================
# InputGuard
# =============================================================
class InputGuard:
    """3 层输入守卫（单例即可，无状态）"""

    # -------------------------------------------------------------
    # L0 他人隐私（确定性，不花 token）
    # -------------------------------------------------------------
    @staticmethod
    def _check_other_private(query: str) -> Optional[GuardResult]:
        """他人指代 + 强隐私项同时出现 → 直接拒答。

        为什么要两张表：单看"别人"是正常咨询（"别人都说这件起球"），
        单看"订单号"也是正常咨询（"我的订单号怎么查"），
        只有"问别人的隐私"才是必须拒绝的。
        """
        if not query:
            return None
        if any(w in query for w in _PRIV_OTHER_WORDS) and any(w in query for w in _PRIV_ITEMS):
            return GuardResult(False, "other_privacy", _PRIV_REPLY, "L0")
        return None

    # -------------------------------------------------------------
    # L1 规则
    # -------------------------------------------------------------
    def _check_l1(self, query: str) -> Optional[GuardResult]:
        """L1 规则检查；通过返 None，否则返 GuardResult"""
        stripped = (query or "").strip()

        if not stripped:
            return GuardResult(False, "too_short", CHITCHAT_RESPONSES["too_short"], "L1")
        if len(stripped) < MIN_LEN:
            return GuardResult(False, "too_short", CHITCHAT_RESPONSES["too_short"], "L1")
        if len(stripped) > MAX_LEN:
            return GuardResult(False, "too_long", CHITCHAT_RESPONSES["too_long"], "L1")

        # 字符多样性（"啊啊啊啊啊啊啊" 类）
        if _char_diversity(stripped) < MIN_CHAR_DIVERSITY:
            return GuardResult(False, "spam", CHITCHAT_RESPONSES["too_short"], "L1")

        # 违禁品（涉毒/枪支/管制刀具/赌博/涉黄/暴恐）
        # 排在黑名单之前：内容合规比 prompt 注入更需要留痕，也更能一票否决。
        # 不做语义判断，只做词表命中——这类问题没有"语境反转"的余地。
        lowered = stripped.lower()
        for category, words in _BANNED_RULES:
            hit = next((w for w in words if w.lower() in lowered), None)
            if hit:
                logger.warning(
                    f"[guard L1] 违禁品命中: category={category} word={hit!r} "
                    f"text={stripped[:50]!r}"
                )
                return GuardResult(
                    False, "banned", _BANNED_REPLY, "L1", category=category
                )

        # 黑名单（prompt injection / jailbreak）
        # 原设计：静默不响应（不告诉攻击者"被识别了"）。
        # 修正：静默的代价是正常用户问一句"你的系统提示是什么"会收到一片空白，
        # 体验上等于被挂断。改为中性回应——不承认、不否认、不说教，但一定有回应。
        for pat in _BLACKLIST_PATTERNS:
            if pat.search(stripped):
                logger.warning(f"[guard L1] 黑名单命中: {stripped[:50]!r}")
                return GuardResult(False, "blacklist", _BLACKLIST_REPLY, "L1")

        # 中文比例（纯英文 / 纯数字 → 不像中文用户）
        ratio = _chinese_ratio(stripped)
        if ratio < MIN_CHINESE_RATIO and not _has_english_sku(stripped):
            return GuardResult(False, "english_no_sku", CHITCHAT_RESPONSES["english_no_sku"], "L1")

        return None

    # -------------------------------------------------------------
    # L2 Embedding 领域相关性
    # -------------------------------------------------------------
    def _check_l2(self, query: str, has_history: bool = False) -> Optional[GuardResult]:
        """L2 闲聊识别；centroid 失败时静默放行

        has_history=True 时按多轮处理（阈值降到 MULTI_TURN_MIN_SIM + 跳过灰区否决）：
        "那它保暖吗"这种追问单句没有任何领域词，按单轮口径必然误杀。
        """
        # 修复：纯订单号或含 SKU 的 query 直接放行
        # （L2 cosine 对"FS368998 规格参数"这类纯属性词几乎必然 < 0.4，业务 query 不该被拦）
        stripped = query.strip()
        if _ORDER_NO_FULL_RE.match(stripped) or _SKU_PATTERN.search(stripped):
            return None
        centroid = get_domain_centroid()
        if centroid is None:
            # centroid 算不出来（embedding API 挂了等）— 放行，不误伤
            logger.warning("[guard L2] centroid 不可用，跳过闲聊识别")
            return None

        try:
            # embed_text_or_mock 失败时返零向量，但 zero vector cosine=0 会被判闲聊
            # 为了避免误伤 embedding 失败的情况，单独 try 一遍
            try:
                q_emb = get_embedding_provider().embed_text(query)
            except EmbeddingError as e:
                logger.warning(f"[guard L2] embed_text 失败，跳过闲聊识别: {e}")
                return None

            # cosine similarity
            dot = sum(a * b for a, b in zip(q_emb, centroid))
            # q_emb 和 centroid 都是 normalized（embedding API 输出 normalized，centroid 也 normalize 过）
            # 所以 cosine = dot
            sim = dot
            logger.info(
                f"[guard L2] domain cosine={sim:.3f} "
                f"{'multi-turn ' if has_history else ''}query={query[:30]!r}"
            )

            # 多轮：阈值降到最低限度，且不做灰区否决——
            # 追问句（"那它保暖吗"）本来就不带领域词，用单轮口径拦是必然误杀。
            if has_history:
                if sim < MULTI_TURN_MIN_SIM:
                    logger.info(
                        f"[guard L2] 多轮仍离题 sim={sim:.3f} query={stripped[:30]!r}"
                    )
                    return GuardResult(
                        False, "no_service", CHITCHAT_RESPONSES["no_service"], "L2"
                    )
                return None

            if sim < DOMAIN_RELEVANCE_THRESHOLD:
                return GuardResult(False, "no_service", CHITCHAT_RESPONSES["no_service"], "L2")
            # 灰区否决：余弦勉强过关，且"领域词"和"业务诉求词"一个都没有 → 判无关
            # 两个词表是并集关系：DOMAIN_WORDS 管"是不是在说这行"（含"你好/这款"），
            # SUBSTANTIVE_KEYWORDS 管"有没有要办的事"（含"能发/几天到"）。
            # 只用前者会漏——"新疆西藏能发吗几天到"没有"发货"只有"能发"，
            # 曾被打成 off_domain，而政策库里明明有答案。
            if (
                _DOMAIN_GRAY_BAND > 0
                and sim < DOMAIN_RELEVANCE_THRESHOLD + _DOMAIN_GRAY_BAND
                and not _has_domain_word(stripped)
                and not has_substantive_demand(stripped)
            ):
                logger.info(
                    f"[guard L2] 灰区无领域词 sim={sim:.3f} query={stripped[:30]!r}"
                )
                return GuardResult(False, "off_domain", CHITCHAT_RESPONSES["no_service"], "L2")
            return None
        except Exception as e:
            logger.exception(f"[guard L2] 异常（放行）: {e}")
            return None

    # -------------------------------------------------------------
    # L3 行为：短期重复
    # -------------------------------------------------------------
    def _check_l3(self, user_id: int, query: str) -> Optional[GuardResult]:
        """L3 重复检测（Redis 计数 1min 窗口，双阈值）

        - 超过 SOFT_LIMIT：放行但标记 degraded（调用方跳缓存 + 进 badcase）
        - 超过 HARD_LIMIT：才当刷子硬拦
        """
        # 匿名用户（user_id=0）跳过 L3（无法稳定计数）
        if user_id <= 0:
            return None

        try:
            key = f"{_REPEAT_KEY_PREFIX}{user_id}:{_md5(query)}"
            r = redis_get()
            count = r.incr(key)
            if count == 1:
                r.expire(key, REPEAT_WINDOW_SECONDS)

            if count > REPEAT_HARD_LIMIT:
                logger.info(
                    f"[guard L3] 硬限命中: user={user_id} count={count} "
                    f"query={query[:30]!r}"
                )
                return GuardResult(False, "spam", CHITCHAT_RESPONSES["spam"], "L3")

            if count > REPEAT_SOFT_LIMIT:
                logger.info(
                    f"[guard L3] 软限放行（跳缓存）: user={user_id} count={count} "
                    f"query={query[:30]!r}"
                )
                return GuardResult(
                    True, "repeat_soft", None, "L3", degraded=True,
                )
            return None
        except Exception as e:
            # Redis 挂了别误伤，放行
            logger.warning(f"[guard L3] Redis 异常（放行）: {e}")
            return None

    # -------------------------------------------------------------
    # 主入口
    # -------------------------------------------------------------
    def check(self, query: str, user_id: int, has_history: bool = False) -> GuardResult:
        """
        完整 3 层检查

        Args:
            query: 用户输入
            user_id: 用户 ID（0 = 匿名）
            has_history: 本会话是否已有对话历史（多轮追问不按单轮口径判领域）

        Returns:
            GuardResult(allowed=True) = 通过
            GuardResult(allowed=False, response=...) = 命中，调用方应直接返 response
        """
        if query is None:
            return GuardResult(False, "too_short", CHITCHAT_RESPONSES["too_short"], "L1")

        # L0：他人隐私（确定性拦截，先于领域相关性判断）
        r = self._check_other_private(query)
        if r is not None:
            self._record_block(r)
            return r

        # L1：规则（最便宜）
        r = self._check_l1(query)
        if r is not None:
            self._record_block(r)
            return r

        # L2：embedding 闲聊识别
        # 身体数据陈述句（"我身高168""体重55公斤"）跳过 L2——见 _BODY_FACT_RE 注释。
        # 只跳过领域判断，L3 防刷照旧（重复刷同一句仍然要管）。
        if not _is_body_fact_query(query):
            r = self._check_l2(query, has_history=has_history)
            if r is not None:
                self._record_block(r)
                return r

        # L3：行为（短期重复）
        r = self._check_l3(user_id, query)
        if r is not None:
            if r.degraded:
                # 软限：放行但降级。不能走 _record_block——它不是拦截，
                # 记进去会让 guard 拦截率虚高，掩盖真正的拦截原因。
                return r
            self._record_block(r)
            return r

        return GuardResult(allowed=True)

    @staticmethod
    def _record_block(r: GuardResult) -> None:
        """记录 guard 命中 metrics（best-effort）"""
        try:
            from app.services.metrics import metrics
            metrics.inc_embedding("guard_block")
        except Exception:
            pass
        logger.info(f"[guard] blocked layer={r.layer} reason={r.reason}")


# 单例
guard = InputGuard()
