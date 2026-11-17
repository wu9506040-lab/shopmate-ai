package com.zhixuan.platform.chat;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zhixuan.platform.auth.UserPrincipal;
import com.zhixuan.platform.common.ApiException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * SSE 透传客户端：Java HttpClient 读 Python /api/chat 的 SSE 字节流，
 * 逐帧转发到 SseEmitter。
 *
 * 线程模型：阻塞式读上游流跑在虚拟线程上（Java 21），
 * SseEmitter 回调（onCompletion/onError/onTimeout）负责断连联动取消上游。
 */
public class AiStreamClient {

    private static final Logger log = LoggerFactory.getLogger(AiStreamClient.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final HttpClient http;
    private final ExecutorService executor;
    private final String baseUrl;
    private final String chatPath;
    private final Duration connectTimeout;

    public AiStreamClient(HttpClient http, ExecutorService executor,
                          String baseUrl, String chatPath, Duration connectTimeout) {
        this.http = http;
        this.executor = executor;
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.chatPath = chatPath;
        this.connectTimeout = connectTimeout;
    }

    /**
     * done 帧落库钩子：由 Controller 注入（平台层是唯一写入者）。
     * 入参为累积结果，返回 null = 原帧透传；返回字符串 = 用重写后的 data 替换（回填消息 id）。
     */
    @FunctionalInterface
    public interface CompletionHandler {
        String onComplete(SseStreamAccumulator accumulator);
    }

    /**
     * 发起透传。同步部分只做「连接建立 + 状态码校验」，
     * 流式转发提交到虚拟线程异步执行。
     *
     * 边界处理（每条都是真实会发生的故障模式）：
     * - 上游连接失败/非 200 → ApiException(502)，由全局异常处理器返回 JSON 错误体
     * - 客户端提前断开 → emitter 回调触发，取消上游读、关闭连接（防资源泄漏）
     * - 上游中途断流 → 发一条 error 事件后 complete（前端能看到失败原因，
     *   而不是连接突兀断开）；**无 done 则不落库（残答禁入）**
     * - 超时 → emitter 超时回调兜底（对齐 nginx 300s 专线）
     *
     * @param token   已由 JwtService 签发/校验的用户 token（Bearer 透传给 Python）
     * @param handler done 帧钩子（落库 + 回填 id），可为 null（纯透传模式）
     */
    public void relay(UserPrincipal user, String token, String requestBody,
                      SseEmitter emitter, CompletionHandler handler) {
        relay(user, token, null, requestBody, emitter, handler);
    }

    /**
     * 同上，额外透传流量分类头。
     *
     * <p>为什么这个头必须透传：Python 侧在会话第一轮读 `X-Traffic-Kind` 落
     * session_meta，统计（看板/KPI/质检）只认 kind='biz'。网关是 /api/chat 的
     * 唯一入口，吞掉这个头等于让所有评测脚本的声明失效——评测流量会重新混进
     * 业务指标，而这正是本次要根治的问题。
     */
    public void relay(UserPrincipal user, String token, String trafficKind, String requestBody,
                      SseEmitter emitter, CompletionHandler handler) {
        HttpRequest upstreamReq;
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + chatPath))
                    .timeout(connectTimeout)
                    .header("Content-Type", "application/json")
                    // 关键：把 Java 校验过的身份以 Bearer 头透传给 Python。
                    // Python deps._extract_token 会优先取 Bearer —— AI 服务无需
                    // 重复解析 Cookie，且请求显式携带身份（无状态服务的前提）
                    .header("Authorization", "Bearer " + token)
                    .header("Accept", "text/event-stream")
                    // P3：告知 Python 本请求的落库权在平台层（单一写入者），
                    // Python 跳过 persist_to_mysql，避免双写
                    .header("X-Platform-Persist", "java");
            if (trafficKind != null && !trafficKind.isBlank()) {
                builder.header("X-Traffic-Kind", trafficKind.trim());
            }
            upstreamReq = builder
                    .POST(HttpRequest.BodyPublishers.ofString(requestBody,
                            StandardCharsets.UTF_8))
                    .build();
        } catch (Exception e) {
            throw new ApiException(502, "AI 服务请求构造失败: " + e.getMessage());
        }

        HttpResponse<java.io.InputStream> resp;
        try {
            resp = http.send(upstreamReq, HttpResponse.BodyHandlers.ofInputStream());
        } catch (Exception e) {
            log.warn("AI 上游连接失败: {}", e.getMessage());
            throw new ApiException(502, "AI 服务不可用");
        }

        if (resp.statusCode() != 200) {
            // 错误透传策略（实测教训：越权会话上游 404 被吞成 502，前端文案失真）：
            // - 4xx = 客户端侧问题（会话不存在/越权/参数错误）：透传状态码，
            //   并解析上游 {"detail": ...} 给出可读文案
            // - 5xx = AI 服务内部故障：收敛为 502，不泄漏内部错误码
            // 必须消费掉错误体，否则 HttpClient 连接无法复用（泄漏）
            String upstreamBody = "";
            try (java.io.InputStream es = resp.body()) {
                upstreamBody = new String(es.readAllBytes(), StandardCharsets.UTF_8);
            } catch (Exception ignore) {
                // 错误体读不到时用空文案兜底
            }
            String detail = "";
            try {
                detail = MAPPER.readTree(upstreamBody).path("detail").asText("");
            } catch (Exception ignore) {
                // 非 JSON 错误体 → 保留原文
                detail = upstreamBody;
            }
            if (resp.statusCode() >= 500) {
                throw new ApiException(502, "AI 服务返回 " + resp.statusCode());
            }
            throw new ApiException(resp.statusCode(),
                    detail.isBlank() ? ("AI 服务拒绝请求(" + resp.statusCode() + ")") : detail);
        }

        // 流式转发（虚拟线程）
        CompletableFuture<Void> relayFuture = CompletableFuture.runAsync(() -> {
            SseFrameParser parser = new SseFrameParser();
            SseStreamAccumulator accumulator = new SseStreamAccumulator();
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(resp.body(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    SseFrameParser.Frame frame = parser.feed(line);
                    if (frame != null) {
                        SseStreamAccumulator.FrameType type = accumulator.accept(frame);
                        if (type == SseStreamAccumulator.FrameType.DONE && handler != null) {
                            // 落库并回填 assistant_message_id（前端按条评价依赖）
                            String rewritten = handler.onComplete(accumulator);
                            if (rewritten != null) {
                                frame = new SseFrameParser.Frame(
                                        frame.id(), frame.event(), rewritten);
                            }
                        }
                        sendFrame(emitter, frame);
                    }
                }
                SseFrameParser.Frame last = parser.flush();
                if (last != null) {
                    accumulator.accept(last);
                    sendFrame(emitter, last);
                }
                emitter.complete();
            } catch (Exception e) {
                // 上游异常或转发失败：尽力发一条 error 事件让前端感知
                log.warn("SSE 透传中断: {}", e.getMessage());
                try {
                    sendError(emitter, "AI 流式响应中断，请稍后重试");
                    emitter.complete();
                } catch (Exception ignore) {
                    // 客户端已断开：静默
                }
            }
        }, executor);

        // 断连联动：客户端断开/超时 → 取消上游读取 + 关流（防连接泄漏）
        Runnable cancel = () -> {
            relayFuture.cancel(true);
            try {
                resp.body().close();
            } catch (java.io.IOException ignore) {
                // 上游流已断时 close 仍可能抛错，静默（目标只是释放连接）
            }
        };
        emitter.onCompletion(cancel);
        emitter.onTimeout(() -> {
            try {
                sendError(emitter, "AI 响应超时");
            } catch (Exception ignore) { }
            emitter.complete();
            cancel.run();
        });
        emitter.onError(t -> cancel.run());
    }

    // ------------------------------------------------------------------

    private static void sendFrame(SseEmitter emitter, SseFrameParser.Frame frame) {
        SseEmitter.SseEventBuilder builder = SseEmitter.event();
        if (frame.id() != null) {
            builder = builder.id(frame.id());
        }
        if (frame.event() != null) {
            builder = builder.name(frame.event());
        }
        builder = builder.data(frame.data());
        try {
            emitter.send(builder);
        } catch (java.io.IOException e) {
            // 客户端已断开（write 失败）→ 抛非受检异常终止转发线程，
            // 由 onError 回调联动取消上游
            throw new IllegalStateException("SSE 写入失败（客户端断开）", e);
        }
    }

    private static void sendError(SseEmitter emitter, String detail) {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("type", "error");
        err.put("detail", detail);
        try {
            emitter.send(SseEmitter.event().data(MAPPER.writeValueAsString(err)));
        } catch (java.io.IOException ignore) {
            // 断连场景下 error 事件发不出去属正常，静默
        }
    }
}
