package com.zhixuan.platform.analytics;

import com.zhixuan.platform.auth.CurrentUserService;
import com.zhixuan.platform.auth.UserPrincipal;
import com.zhixuan.platform.common.ApiException;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * /api/admin/analytics —— 运营聚合（当前提供数据看板 board）。
 *
 * <p>平台层只做三件事：验管理员身份、校验窗口参数、把 AI 服务的响应原样回给前端。
 * 统计口径全部在 AI 服务侧（一份实现、一套单测），原因见 {@link AnalyticsService}。
 *
 * <p>`/api/admin/analytics`（区间报表）与 `/kpi` 仍由 AI 服务侧直接提供，
 * nginx 仅把 `/api/admin/analytics/board` 精确切到平台层（见 nginx.conf）。
 */
@RestController
public class AnalyticsController {

    private final AnalyticsService service;
    private final CurrentUserService currentUser;

    public AnalyticsController(AnalyticsService service, CurrentUserService currentUser) {
        this.service = service;
        this.currentUser = currentUser;
    }

    @GetMapping("/api/admin/analytics/board")
    public ResponseEntity<byte[]> board(@RequestParam(defaultValue = "14") int days,
                                        HttpServletRequest request) {
        UserPrincipal user = currentUser.require(request);
        if (!"admin".equals(user.role()) && !"super_admin".equals(user.role())) {
            throw new ApiException(403, "需要管理员权限");
        }
        if (days < 1 || days > 90) {
            throw new ApiException(422, "days: 取值范围 1-90");
        }

        AnalyticsService.Upstream upstream = service.board(
                days,
                request.getHeader("Authorization"),
                request.getHeader("Cookie"));

        // 原样回字节：不解 JSON 再拼，避免字段顺序/时间格式/null 处理再分叉一次
        return ResponseEntity.status(upstream.status())
                .contentType(MediaType.APPLICATION_JSON)
                .header("X-Analytics-Source",
                        upstream.status() == 200 ? "ai-service" : "degraded")
                .body(upstream.body());
    }
}
