package com.zhixuan.platform.chat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 流累积器测试 —— 落库内容与时机的正确性全押在这里。
 *
 * 为什么这么测：
 * - token 是增量片段，落库必须拼成完整回答（拼接错 = 历史记录残缺）
 * - meta 携带 RAG 检索上下文，assistant 行要存（前端展示来源）
 * - **无 done 的流（用户中断/上游断流）绝不能落库**——残答入库会污染历史
 *   并让"按条评价"指向半截回答，这是业务铁律，必须单独断言
 */
class SseStreamAccumulatorTest {

    private static SseFrameParser.Frame frame(String data) {
        return new SseFrameParser.Frame(null, null, data);
    }

    @Test
    @DisplayName("token 增量拼接成完整回答 + done 标记可落库")
    void tokensConcatenatedAndDoneMarks() {
        SseStreamAccumulator acc = new SseStreamAccumulator();
        acc.accept(frame("{\"type\":\"token\",\"text\":\"你好\"}"));
        acc.accept(frame("{\"type\":\"token\",\"text\":\"，请问\"}"));
        acc.accept(frame("{\"type\":\"done\",\"session_id\":\"s1\"}"));
        assertThat(acc.answer()).isEqualTo("你好，请问");
        assertThat(acc.sessionId()).isEqualTo("s1");
        assertThat(acc.persistable()).isTrue();
    }

    @Test
    @DisplayName("meta 捕获检索上下文与分数（JSON 数组原样保留）")
    void metaContextsCaptured() {
        SseStreamAccumulator acc = new SseStreamAccumulator();
        acc.accept(frame("""
                {"type":"meta","intent":"policy_query",
                 "contexts":["退换货政策：7天无理由"],"scores":[0.92]}
                """));
        assertThat(acc.intent()).isEqualTo("policy_query");
        assertThat(acc.contextsJson()).contains("7天无理由");
        assertThat(acc.scoresJson()).contains("0.92");
    }

    @Test
    @DisplayName("★残答禁入：只有 token 没有 done → 不可落库")
    void incompleteStreamNotPersistable() {
        SseStreamAccumulator acc = new SseStreamAccumulator();
        acc.accept(frame("{\"type\":\"token\",\"text\":\"半截回答\"}"));
        assertThat(acc.doneSeen()).isFalse();
        assertThat(acc.persistable()).isFalse();   // 上游断流：不入库
    }

    @Test
    @DisplayName("done 但回答为空（如守卫拦截无 token）→ 不可落库")
    void emptyAnswerNotPersistable() {
        SseStreamAccumulator acc = new SseStreamAccumulator();
        acc.accept(frame("{\"type\":\"done\",\"session_id\":\"s2\"}"));
        assertThat(acc.persistable()).isFalse();
    }

    @Test
    @DisplayName("非 JSON / heartbeat / closed 帧不影响状态")
    void noiseFramesIgnored() {
        SseStreamAccumulator acc = new SseStreamAccumulator();
        assertThat(acc.accept(frame("{\"type\":\"heartbeat\",\"ts\":1}")))
                .isEqualTo(SseStreamAccumulator.FrameType.OTHER);
        assertThat(acc.accept(frame("not-json"))).isEqualTo(SseStreamAccumulator.FrameType.OTHER);
        acc.accept(frame("{\"type\":\"token\",\"text\":\"a\"}"));
        acc.accept(frame("{\"type\":\"done\",\"session_id\":\"s3\"}"));
        assertThat(acc.answer()).isEqualTo("a");
        assertThat(acc.persistable()).isTrue();
    }

    @Test
    @DisplayName("done 帧类型识别正确（触发落库钩子的唯一信号）")
    void doneFrameTypeDetected() {
        SseStreamAccumulator acc = new SseStreamAccumulator();
        assertThat(acc.accept(frame("{\"type\":\"token\",\"text\":\"x\"}")))
                .isEqualTo(SseStreamAccumulator.FrameType.TOKEN);
        assertThat(acc.accept(frame("{\"type\":\"done\",\"session_id\":\"s4\"}")))
                .isEqualTo(SseStreamAccumulator.FrameType.DONE);
    }
}
