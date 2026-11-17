"""回答分段器 — 把一整块长回答按句末切成 2-3 段。

为什么需要：
  用户连发「在吗 / 这件什么成分 / 能便宜点吗」，后端合并成一次回答。
  合并是对的（不丢问题），但一条回复几百字堆在一起，用户既读不完，
  也分不清哪段答的是哪句。分段相当于给回答加"呼吸"。

切在哪（这是唯一需要判断的事）：
  只在句末标点后切，绝不在句中切。流式 token 是任意片段，
  所以判定条件是"本片段结尾恰好是句末标点"，而不是"累计里有标点"——
  后者会在别人一句话说到一半时插空行。

为什么不按问题条数切：
  问题条数不可靠（一条消息里可能有两个问题，两条消息可能是同一个问题）。
  按长度切是稳定的：不管几条问题，超过一屏就分。问题对应关系交给
  过渡词（"另外""还有个事"）和内容本身维持。
"""
from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

# 句末标点（含中文省略号、波浪号——客服话术里"~"经常当句尾）
_SENTENCE_END = frozenset("。！？!?…～~")

# 分段分隔符：空行。前端 pre-wrap 直接渲染成两段，闲鱼侧可按 "\n\n" 拆条发送
SEGMENT_SEPARATOR = "\n\n"


class AnswerSegmenter:
    """流式回答分段器（有状态，每轮请求一个实例）"""

    __slots__ = ("_buf_len", "_last_cut_at", "_segments", "_enabled",
                 "_max_chars", "_max_segments")

    def __init__(
        self,
        max_chars: int | None = None,
        max_segments: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._enabled = (
            settings.ANSWER_SEGMENT_ENABLED if enabled is None else enabled
        )
        self._max_chars = (
            settings.ANSWER_SEGMENT_MAX_CHARS if max_chars is None else max_chars
        )
        self._max_segments = (
            settings.ANSWER_SEGMENT_MAX_SEGMENTS
            if max_segments is None else max_segments
        )
        self._buf_len = 0          # 本轮已下发的可见字符数（含分隔符）
        self._last_cut_at = 0      # 上一次切段时的 _buf_len
        self._segments = 1         # 当前是第几段（从 1 开始）

    def feed(self, piece: str) -> str:
        """喂一个流式片段，返回可能要追加分隔符的文本。

        绝不丢弃、绝不改写原片段内容——只在末尾追加，保证
        full_answer 与实际下发文本始终一致（落库/缓存/自检都依赖这点）。
        """
        if not self._enabled or not piece:
            return piece

        self._buf_len += len(piece)

        # 段数已用尽 → 不再切
        if self._segments >= self._max_segments:
            return piece

        # 本段还不够长 → 不切
        if self._buf_len - self._last_cut_at < self._max_chars:
            return piece

        # 片段结尾不是句末标点 → 说明正说到一半，等下一片
        if piece.rstrip()[-1:] not in _SENTENCE_END:
            return piece

        # 已经是换行结尾 → 避免出现连续空行
        if piece.endswith("\n"):
            return piece

        self._segments += 1
        self._last_cut_at = self._buf_len
        logger.debug(
            f"[segmenter] 第 {self._segments} 段开始于 {self._buf_len} 字"
        )
        return piece + SEGMENT_SEPARATOR

    @property
    def segments(self) -> int:
        """已切出的段数（1 表示没切过）"""
        return self._segments


def split_text(text: str, max_chars: int = 120, max_segments: int = 3) -> list[str]:
    """把已完成的长文本按句末切成若干段（非流式场景 / 单测用）。

    与 AnswerSegmenter 同一套规则，但作用在完整文本上：
    逐句累加，超过 max_chars 就在当前句结束处断开，最多切 max_segments 段。
    """
    if not text:
        return []
    # 先按句末标点切句（保留标点）
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENTENCE_END:
            sentences.append(buf)
            buf = ""
    if buf:
        sentences.append(buf)

    segments: list[str] = []
    cur = ""
    for s in sentences:
        if cur and len(cur) + len(s) > max_chars and len(segments) < max_segments - 1:
            segments.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        segments.append(cur)
    return segments
