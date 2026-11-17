package com.zhixuan.platform.analytics;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

import com.zhixuan.platform.config.AiGatewayProps;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

/**
 * 数据看板：转发到 AI 服务的统计实现，平台层不再自己算。
 *
 * <p>为什么删掉原来那 224 行聚合：它和 Python 的 {@code analytics_board} 是逐行
 * 翻译关系（连「热门问题截断 14 字」「老客 = 会话消息 ≥ 20 条」「响应时间超 1 小时
 * 不计」都一模一样），但两边各写各的、谁都没测对方。结果是改一边没人知道另一边
 * 漏了——例如评测会话前缀，Python 补了 {@code eval|diag|chk}，Java 靠人肉同步；
 * 真漏了也不会报错，只会让看板数字悄悄变脏。
 *
 * <p>现在职责切清楚：**AI 服务是统计的唯一实现**，平台层只做鉴权 + 转发 +
 * 保留原始字节。不重新序列化是有意的——一旦 Java 把 JSON 解了再拼，
 * 字段顺序、时间格式、null 处理都会再分叉一次，等于把刚拆掉的第二份实现换了个形式。
 */
@Service
public class AnalyticsService {

    /** 上游响应：状态码 + 原始字节（透传，不解 JSON） */
    public record Upstream(int status, byte[] body) {
    }

    private static final Logger log = LoggerFactory.getLogger(AnalyticsService.class);
    private static final String BOARD_PATH = "/api/admin/analytics/board";

    /** 看板是低频管理操作，不值得等太久；超时宁可降级也不要挂住后台页面 */
    private static final Duration READ_TIMEOUT = Duration.ofSeconds(5);

    private static final byte[] DEGRADED_BODY =
            ("{\"error\":\"看板数据暂不可用（统计服务未响应）\",\"degraded\":true}")
                    .getBytes(StandardCharsets.UTF_8);

    private final HttpClient http;
    private final String baseUrl;

    public AnalyticsService(HttpClient aiHttpClient, AiGatewayProps props) {
        this.http = aiHttpClient;
        this.baseUrl = props.baseUrl();
    }

    /**
     * 拉取看板数据。
     *
     * @param days          统计窗口（1-90），由 Controller 校验
     * @param authorization 原始 Authorization 头（Bearer），可为空
     * @param cookie        原始 Cookie 头，可为空
     */
    public Upstream board(int days, String authorization, String cookie) {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + BOARD_PATH + "?days=" + days))
                .timeout(READ_TIMEOUT)
                .header("Accept", "application/json")
                .GET();
        // 身份原样透传：Python 用同一份 JWT 密钥验签（字节级互通），
        // 平台层已经验过一次，这里只是把「谁在看」传给统计服务用于审计留痕。
        if (authorization != null && !authorization.isBlank()) {
            builder.header("Authorization", authorization);
        }
        if (cookie != null && !cookie.isBlank()) {
            builder.header("Cookie", cookie);
        }

        try {
            HttpResponse<byte[]> resp = http.send(builder.build(),
                    HttpResponse.BodyHandlers.ofByteArray());
            if (resp.statusCode() != 200) {
                // 4xx 透传（越权/参数错误要能被前端看见），5xx 也如实回，
                // 但都记一条日志：看板坏了不该只表现为「页面空着」。
                log.warn("看板上游返回 {} days={}", resp.statusCode(), days);
            }
            return new Upstream(resp.statusCode(), resp.body());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.warn("看板转发被中断 days={}", days);
            return new Upstream(503, DEGRADED_BODY);
        } catch (IOException e) {
            log.warn("看板转发失败 days={}: {}", days, e.getMessage());
            return new Upstream(503, DEGRADED_BODY);
        }
    }
}
