package com.zhixuan.platform.chat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * SSE 分帧边界测试。
 *
 * 为什么这么测：透传的正确性完全取决于分帧器对「不规整流」的处理——
 * 真实网络上 SSE 帧会以任意字节边界到达（半帧跨 TCP 包、多个事件挤一个包、
 * CRLF 与 LF 混用、keep-alive 注释穿插）。这些场景无法靠单一 happy path 覆盖，
 * 逐一用最小输入做确定性断言。
 */
class SseFrameParserTest {

    @Test
    @DisplayName("标准单帧：data 行 + 空行收尾 → 派发一帧")
    void simpleFrame() {
        SseFrameParser p = new SseFrameParser();
        assertThat(p.feed("data: {\"type\":\"token\",\"text\":\"你好\"}")).isNull();
        SseFrameParser.Frame f = p.feed("");
        assertThat(f).isNotNull();
        assertThat(f.data()).isEqualTo("{\"type\":\"token\",\"text\":\"你好\"}");
        assertThat(f.id()).isNull();
    }

    @Test
    @DisplayName("CRLF 行尾：\\r 被剥离（Windows/部分代理会引入 CRLF）")
    void crlfLineEndings() {
        SseFrameParser p = new SseFrameParser();
        assertThat(p.feed("data: hello\r")).isNull();
        SseFrameParser.Frame f = p.feed("\r");
        assertThat(f).isNotNull();
        assertThat(f.data()).isEqualTo("hello");
    }

    @Test
    @DisplayName("多事件挤在一个 readLine 批次里（高频 token 流的真实形态）")
    void multipleEventsInOneBatch() {
        SseFrameParser p = new SseFrameParser();
        SseFrameParser.Frame f1 = p.feed("data: one");
        assertThat(f1).isNull();
        assertThat(p.feed("")).isNotNull().extracting(SseFrameParser.Frame::data)
                .isEqualTo("one");
        assertThat(p.feed("data: two")).isNull();
        SseFrameParser.Frame f2 = p.feed("");
        assertThat(f2.data()).isEqualTo("two");
    }

    @Test
    @DisplayName("多行 data 以 \\n 连接（SSE 规范；长回复分片写入时不丢换行）")
    void multiLineDataJoinedWithNewline() {
        SseFrameParser p = new SseFrameParser();
        p.feed("data: line1");
        p.feed("data: line2");
        SseFrameParser.Frame f = p.feed("");
        assertThat(f.data()).isEqualTo("line1\nline2");
    }

    @Test
    @DisplayName("注释行（: keep-alive）被忽略且不干扰后续事件")
    void commentLinesIgnored() {
        SseFrameParser p = new SseFrameParser();
        assertThat(p.feed(": ping")).isNull();          // 注释：无帧派发
        assertThat(p.feed("")).isNull();                // 空行：无 data 不派发
        p.feed("data: real");
        SseFrameParser.Frame f = p.feed("");
        assertThat(f.data()).isEqualTo("real");         // 注释后事件仍完整
    }

    @Test
    @DisplayName("id 字段捕获与透传（前端 resume 依赖 seq 连续性）")
    void idFieldCaptured() {
        SseFrameParser p = new SseFrameParser();
        p.feed("id: 42");
        p.feed("data: payload");
        SseFrameParser.Frame f = p.feed("");
        assertThat(f.id()).isEqualTo("42");
        assertThat(f.data()).isEqualTo("payload");
    }

    @Test
    @DisplayName("流以非空行结束（上游断流）：flush 冲出最后一帧不丢消息")
    void flushFlushesPartialFrame() {
        SseFrameParser p = new SseFrameParser();
        assertThat(p.feed("data: tail")).isNull();
        SseFrameParser.Frame f = p.flush();
        assertThat(f).isNotNull();
        assertThat(f.data()).isEqualTo("tail");
    }

    @Test
    @DisplayName("帧内帧间状态隔离：前一帧的 id 不泄漏到后一帧")
    void stateResetBetweenFrames() {
        SseFrameParser p = new SseFrameParser();
        p.feed("id: 1");
        p.feed("data: a");
        assertThat(p.feed("").id()).isEqualTo("1");
        p.feed("data: b");
        SseFrameParser.Frame second = p.feed("");
        assertThat(second.id()).isNull();   // 未再发 id → 不应沿用上一帧的 "1"
        assertThat(second.data()).isEqualTo("b");
    }

    @Test
    @DisplayName("无冒号字段名行：按空值处理不抛异常（容错上游畸形行）")
    void fieldWithoutColonTolerated() {
        SseFrameParser p = new SseFrameParser();
        assertThat(p.feed("weirdline")).isNull();
        p.feed("data: ok");
        assertThat(p.feed("").data()).isEqualTo("ok");
    }

    @Test
    @DisplayName("data 行前导空格只去一个（'data:  x' 值为 ' x'，规范行为）")
    void onlyOneLeadingSpaceStripped() {
        SseFrameParser p = new SseFrameParser();
        p.feed("data:  two-spaces");
        assertThat(p.feed("").data()).isEqualTo(" two-spaces");
    }
}
