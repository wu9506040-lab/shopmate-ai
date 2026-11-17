package com.zhixuan.platform.handoff;

import java.util.Map;

import com.zhixuan.platform.auth.CurrentUserService;
import com.zhixuan.platform.auth.UserPrincipal;
import com.zhixuan.platform.common.ApiException;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * /api/admin/handoff —— 坐席工作台（全部要求 admin 角色，非 admin 403）。
 *
 * 2026-11：回复与结单拆成两个端点，支持坐席多轮沟通：
 *   GET  /tickets?status=&page=&page_size=
 *   GET  /tickets/{id}              详情（含转人工原因 + 会话时间线 + 自动结单倒计时）
 *   POST /tickets/{id}/take        pending → taken
 *   POST /tickets/{id}/reply       发送一条人工回复（保持 taken，可多轮）
 *   POST /tickets/{id}/resolve     处理完成，结单（reply 可选）
 *   POST /tickets/{id}/close       直接归档
 * 另有定时任务：用户 2 分钟无回复自动结单（HandoffService.autoCloseExpired）。
 */
@RestController
@RequestMapping("/api/admin/handoff")
public class HandoffController {

    private final HandoffService service;
    private final CurrentUserService currentUser;

    public HandoffController(HandoffService service, CurrentUserService currentUser) {
        this.service = service;
        this.currentUser = currentUser;
    }

    @GetMapping("/tickets")
    public Map<String, Object> list(@RequestParam(required = false) String status,
                                    @RequestParam(defaultValue = "1") int page,
                                    @RequestParam(name = "page_size", defaultValue = "20") int pageSize,
                                    HttpServletRequest request) {
        requireAdmin(request);
        if (page < 1) {
            throw new ApiException(422, "page: 必须 ≥ 1");
        }
        if (pageSize < 1 || pageSize > 100) {
            throw new ApiException(422, "page_size: 取值范围 1-100");
        }
        return service.list(status, page, pageSize);
    }

    @GetMapping("/tickets/{id}")
    public Map<String, Object> detail(@PathVariable long id, HttpServletRequest request) {
        requireAdmin(request);
        return service.detail(id);
    }

    @PostMapping("/tickets/{id}/take")
    public Map<String, Object> take(@PathVariable long id, HttpServletRequest request) {
        UserPrincipal admin = requireAdmin(request);
        return service.take(id, admin);
    }

    /** 发送人工回复：工单保持「处理中」，坐席可继续沟通 */
    @PostMapping("/tickets/{id}/reply")
    public Map<String, Object> reply(@PathVariable long id,
                                     @RequestBody(required = false) Map<String, Object> body,
                                     HttpServletRequest request) {
        UserPrincipal admin = requireAdmin(request);
        Object text = body == null ? null : body.get("reply");
        return service.reply(id, admin, text == null ? null : String.valueOf(text));
    }

    /** 处理完成，结单；可带最后一条回复 */
    @PostMapping("/tickets/{id}/resolve")
    public Map<String, Object> resolve(@PathVariable long id,
                                       @RequestBody(required = false) Map<String, Object> body,
                                       HttpServletRequest request) {
        UserPrincipal admin = requireAdmin(request);
        Object reply = body == null ? null : body.get("reply");
        return service.resolve(id, admin, reply == null ? null : String.valueOf(reply));
    }

    @PostMapping("/tickets/{id}/close")
    public Map<String, Object> close(@PathVariable long id, HttpServletRequest request) {
        UserPrincipal admin = requireAdmin(request);
        return service.close(id, admin);
    }

    /** 身份 + 角色校验（403 文案与 Python require_admin 一致） */
    private UserPrincipal requireAdmin(HttpServletRequest request) {
        UserPrincipal user = currentUser.require(request);
        if (!"admin".equals(user.role()) && !"super_admin".equals(user.role())) {
            throw new ApiException(403, "需要管理员权限");
        }
        return user;
    }
}
