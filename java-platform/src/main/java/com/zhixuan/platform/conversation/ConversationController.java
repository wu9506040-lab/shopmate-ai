package com.zhixuan.platform.conversation;

import java.util.Map;

import com.zhixuan.platform.auth.CurrentUserService;
import com.zhixuan.platform.auth.UserPrincipal;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * /api/conversations —— 与 Python api/conversations.py 路由与响应契约对齐。
 */
@RestController
@RequestMapping("/api/conversations")
public class ConversationController {

    private final ConversationService service;
    private final CurrentUserService currentUser;

    public ConversationController(ConversationService service, CurrentUserService currentUser) {
        this.service = service;
        this.currentUser = currentUser;
    }

    @GetMapping("")
    public Map<String, Object> list(HttpServletRequest request) {
        UserPrincipal user = currentUser.require(request);
        return service.listConversations(user);
    }

    @GetMapping("/{sessionId}/messages")
    public Map<String, Object> messages(
            @PathVariable String sessionId,
            @RequestParam(defaultValue = "20") int limit,
            @RequestParam(required = false) Long cursor,
            HttpServletRequest request) {
        UserPrincipal user = currentUser.require(request);
        // 参数边界：limit 1-100、cursor >= 1（与 Python Query(ge=1, le=100) 一致）
        if (limit < 1 || limit > 100) {
            throw new com.zhixuan.platform.common.ApiException(422, "limit: 取值范围 1-100");
        }
        if (cursor != null && cursor < 1) {
            throw new com.zhixuan.platform.common.ApiException(422, "cursor: 必须为正整数");
        }
        if (sessionId.length() > 64) {
            throw new com.zhixuan.platform.common.ApiException(422, "session_id: 长度 1-64");
        }
        return service.getMessages(user, sessionId, limit, cursor);
    }

    @org.springframework.web.bind.annotation.ResponseStatus(
            org.springframework.http.HttpStatus.OK)
    @DeleteMapping("/{sessionId}")
    public Map<String, Object> delete(@PathVariable String sessionId,
                                      HttpServletRequest request) {
        UserPrincipal user = currentUser.require(request);
        return service.deleteConversation(user, sessionId);
    }

    @PatchMapping("/{sessionId}/title")
    public Map<String, Object> title(@PathVariable String sessionId,
                                     @RequestBody Map<String, Object> body,
                                     HttpServletRequest request) {
        UserPrincipal user = currentUser.require(request);
        String title = body == null ? null : (String) body.get("title");
        return service.updateTitle(user, sessionId, title);
    }
}
