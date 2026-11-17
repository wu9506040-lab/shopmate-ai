package com.zhixuan.platform.chat;

import com.zhixuan.platform.auth.CurrentUserService;
import com.zhixuan.platform.auth.UserPrincipal;
import com.zhixuan.platform.common.ApiException;
import com.zhixuan.platform.config.AiGatewayProps;
import com.zhixuan.platform.config.JwtProps;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.Map;

/**
 * POST /api/chat —— SSE 网关（P2 核心）。
 *
 * 路径：浏览器 → Java 网关（鉴权+参数校验）→ Python AI 服务（SSE 流）
 *        → Java 逐帧转发 → 浏览器。前端零改动（同一路径同一种事件协议）。
 *
 * 为什么网关强制登录（Python 侧是 optional auth）：
 * 平台层是身份的唯一权威。AI 服务未来要无状态水平扩展，
 * 「每个请求显式携带身份」是前提；匿名透传会把这个约束破坏掉。
 */
@RestController
public class ChatGatewayController {

    private static final Logger log = LoggerFactory.getLogger(ChatGatewayController.class);
    private static final com.fasterxml.jackson.databind.ObjectMapper MAPPER =
            new com.fasterxml.jackson.databind.ObjectMapper();

    private final CurrentUserService currentUser;
    private final AiStreamClient aiClient;
    private final JwtProps jwtProps;
    private final com.zhixuan.platform.auth.JwtService jwtService;
    private final ChatMessagePersister persister;

    public ChatGatewayController(CurrentUserService currentUser,
                                 AiStreamClient aiClient,
                                 JwtProps jwtProps,
                                 com.zhixuan.platform.auth.JwtService jwtService,
                                 ChatMessagePersister persister) {
        this.currentUser = currentUser;
        this.aiClient = aiClient;
        this.jwtProps = jwtProps;
        this.jwtService = jwtService;
        this.persister = persister;
    }

    @PostMapping("/api/chat")
    public SseEmitter chat(@RequestBody Map<String, Object> body,
                           HttpServletRequest request) {
        // 1. 身份：网关强制登录（fail-closed）
        UserPrincipal user = currentUser.require(request);

        // 2. 参数校验（对齐 Python ChatRequest 的边界）
        Object queryObj = body == null ? null : body.get("query");
        String query = queryObj == null ? "" : String.valueOf(queryObj);
        if (query.isBlank()) {
            throw new ApiException(422, "query: 不能为空");
        }
        if (query.length() > 2000) {
            throw new ApiException(422, "query: 长度上限 2000 字符");
        }
        Object sidObj = body.get("session_id");
        if (sidObj != null && String.valueOf(sidObj).length() > 64) {
            throw new ApiException(422, "session_id: 长度上限 64");
        }

        // 3. 身份 token（同一 JWT_SECRET，Python 侧直接互认）
        String token = jwtService.issue(user.id(), user.role());

        // 4. 建立下游 SSE 通道（0 = 不由容器超时，由 onTimeout 逻辑自管）
        SseEmitter emitter = new SseEmitter(0L);

        log.info("chat relay: user={} queryLen={} sessionId={}",
                user.id(), query.length(), sidObj);
        // 流量分类透传：评测/smoke/排障脚本用 X-Traffic-Kind 声明自己不是业务流量，
        // Python 据此落 session_meta。网关吞掉它，评测就会污染看板。
        aiClient.relay(user, token, request.getHeader("X-Traffic-Kind"),
                serializeBody(body), emitter, accumulator -> {
            // P3：done 帧 → 平台层落库（单一写入者），回填 assistant_message_id
            Long assistantId = persister.persist(user.id(), query, accumulator);
            if (assistantId == null) {
                return null;   // 落库失败/残答：原帧透传，不影响前端
            }
            try {
                com.fasterxml.jackson.databind.node.ObjectNode done =
                        (com.fasterxml.jackson.databind.node.ObjectNode) accumulator.donePayload();
                done.put("assistant_message_id", assistantId);
                return MAPPER.writeValueAsString(done);
            } catch (Exception e) {
                return null;
            }
        });
        return emitter;
    }

    /** 原样透传请求体（extra="allow"：channel/buyer_unb 等扩展字段不丢弃） */
    private static String serializeBody(Map<String, Object> body) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(body);
        } catch (Exception e) {
            throw new ApiException(422, "请求体序列化失败");
        }
    }
}
