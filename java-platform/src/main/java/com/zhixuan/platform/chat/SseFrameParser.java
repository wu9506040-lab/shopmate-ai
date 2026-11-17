package com.zhixuan.platform.chat;

import java.util.ArrayList;
import java.util.List;

/**
 * SSE 帧解析器 —— 把上游按行到达的字节流解析为完整事件帧。
 *
 * 纯函数式设计（无 IO 依赖）的原因：SSE 分帧逻辑是最容易出边界 bug 的地方
 * （半帧跨包、多事件一包、CRLF、注释行），必须独立单测覆盖。
 *
 * 遵循 W3C SSE 规范：
 * - 空行 = 事件边界，派发已积累的帧
 * - data: 可多行，派发时以 \n 连接
 * - 行内去掉字段冒号后【一个】可选前导空格
 * - 以 ":" 开头 = 注释（keep-alive），忽略
 * - id/event 字段捕获；retry 等未知字段忽略
 */
public final class SseFrameParser {

    /** 一条完整 SSE 事件（data 以 \n 连接多行；无 data 行的帧视为空事件不派发） */
    public record Frame(String id, String event, String data) {
    }

    private String pendingId = null;
    private String pendingEvent = null;
    private final List<String> dataLines = new ArrayList<>();

    /**
     * 喂入一行（不含行终止符；如上游带 \r\n 需先 strip \r）。
     * 返回非空 = 该行触发了一次完整事件派发。
     */
    public Frame feed(String rawLine) {
        String line = rawLine == null ? "" : stripTrailingCr(rawLine);

        // 空行 → 事件边界
        if (line.isEmpty()) {
            return dispatch();
        }
        // 注释行（keep-alive）：忽略，不重置累积状态
        if (line.startsWith(":")) {
            return null;
        }

        String field;
        String value;
        int colon = line.indexOf(':');
        if (colon < 0) {
            field = line;
            value = "";
        } else {
            field = line.substring(0, colon);
            value = line.substring(colon + 1);
            if (value.startsWith(" ")) {
                value = value.substring(1);   // 规范：只去掉一个前导空格
            }
        }

        switch (field) {
            case "id" -> pendingId = value;
            case "event" -> pendingEvent = value;
            case "data" -> dataLines.add(value);
            // retry 等未知字段：忽略（上游 Python 侧目前不发送）
            default -> { }
        }
        return null;
    }

    /** 上游流结束时调用：把未以空行收尾的最后一帧冲出来 */
    public Frame flush() {
        return dispatch();
    }

    private Frame dispatch() {
        if (dataLines.isEmpty()) {
            // 空行但无 data：只重置累积状态，不派发（避免把纯注释块当事件）
            pendingId = null;
            pendingEvent = null;
            return null;
        }
        Frame frame = new Frame(pendingId, pendingEvent, String.join("\n", dataLines));
        pendingId = null;
        pendingEvent = null;
        dataLines.clear();
        return frame;
    }

    private static String stripTrailingCr(String s) {
        return s.endsWith("\r") ? s.substring(0, s.length() - 1) : s;
    }
}
