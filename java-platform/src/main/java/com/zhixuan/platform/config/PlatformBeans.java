package com.zhixuan.platform.config;

import com.zhixuan.platform.chat.AiStreamClient;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.concurrent.ExecutorService;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class PlatformBeans {

    @Bean
    public com.zhixuan.platform.auth.JwtService jwtService(JwtProps props) {
        return new com.zhixuan.platform.auth.JwtService(props);
    }

    /**
     * 全局共享 HttpClient：连接池复用（Python 侧 HTTP/1.1 keep-alive）。
     * 为什么不用 WebClient/WebFlux：透传逻辑用「阻塞读 + 回调写」心智模型
     * 更简单；虚拟线程让阻塞成本归零 —— Java 21 时代对 reactive 的务实替代。
     */
    @Bean
    public HttpClient aiHttpClient(AiGatewayProps aiProps) {
        return HttpClient.newBuilder()
                // 关键：HTTP/1.1。Java HttpClient 默认尝试 h2c 升级，
                // 而 Python 的 uvicorn(h11) 不支持 → 直接返回 400
                // "Invalid HTTP request received"（真集成才暴露的坑）
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(aiProps.connectTimeout())
                .build();
    }

    /**
     * SSE 透传专用线程池（虚拟线程：每个进行中的对话一条，阻塞读不占平台线程）。
     */
    @Bean(destroyMethod = "close")
    public ExecutorService sseRelayExecutor() {
        return java.util.concurrent.Executors.newVirtualThreadPerTaskExecutor();
    }

    @Bean
    public AiStreamClient aiStreamClient(HttpClient aiHttpClient,
                                         ExecutorService sseRelayExecutor,
                                         AiGatewayProps aiProps) {
        return new AiStreamClient(aiHttpClient, sseRelayExecutor,
                aiProps.baseUrl(), aiProps.chatPath(),
                Duration.ofMillis(aiProps.gateway().connectTimeoutMs()));
    }
}
