package com.zhixuan.platform.chat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * SSE 流累积器：从透传的事件流中提取落库所需信息。
 *
 * 为什么独立成类：落库时机与内容正确性全部依赖「流内语义」——
 * token 拼接（流式增量 → 完整回答）、meta 携带的检索上下文、
 * done 携带的 session_id。「上游断流（无 done）不落残答」是业务铁律，
 * 必须用纯函数单测锁死，不能埋在 IO 逻辑里。
 */
public final class SseStreamAccumulator {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    public enum FrameType { META, TOKEN, DONE, OTHER }

    private final StringBuilder answer = new StringBuilder();
    private String sessionId;
    private String intent;
    private String contextsJson;
    private String scoresJson;
    private JsonNode donePayload;
    private boolean doneSeen;

    /** 消费一帧，返回该帧的语义类型（DONE 用于触发落库） */
    public FrameType accept(SseFrameParser.Frame frame) {
        if (frame == null || frame.data() == null || frame.data().isBlank()) {
            return FrameType.OTHER;
        }
        JsonNode node;
        try {
            node = MAPPER.readTree(frame.data());
        } catch (Exception e) {
            return FrameType.OTHER;   // 非 JSON 帧（理论上不存在）忽略
        }
        String type = node.path("type").asText("");
        switch (type) {
            case "token" -> {
                answer.append(node.path("text").asText(""));
                return FrameType.TOKEN;
            }
            case "meta" -> {
                intent = node.path("intent").asText(null);
                if (node.has("contexts") && !node.get("contexts").isNull()) {
                    contextsJson = node.get("contexts").toString();
                }
                if (node.has("scores") && !node.get("scores").isNull()) {
                    scoresJson = node.get("scores").toString();
                }
                return FrameType.META;
            }
            case "done" -> {
                sessionId = node.path("session_id").asText(null);
                donePayload = node;
                doneSeen = true;
                return FrameType.DONE;
            }
            default -> {
                return FrameType.OTHER;   // heartbeat / closed / error
            }
        }
    }

    public String answer() {
        return answer.toString();
    }

    public String sessionId() {
        return sessionId;
    }

    public String intent() {
        return intent;
    }

    public String contextsJson() {
        return contextsJson;
    }

    public String scoresJson() {
        return scoresJson;
    }

    public JsonNode donePayload() {
        return donePayload;
    }

    public boolean doneSeen() {
        return doneSeen;
    }

    /** 落库前置条件：正常完成（done）且回答非空——残答禁入 */
    public boolean persistable() {
        return doneSeen
                && sessionId != null && !sessionId.isBlank()
                && !answer.isEmpty();
    }
}
