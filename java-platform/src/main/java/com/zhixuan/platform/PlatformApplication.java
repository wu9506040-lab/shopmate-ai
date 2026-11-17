package com.zhixuan.platform;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 智能客服中台 - Java 平台层入口
 *
 * 职责边界（Java+Python 混合架构）：
 * - Java：平台业务 —— 鉴权、会话/消息真源、工单、看板聚合、SSE 网关
 * - Python：AI 推理 —— 意图路由、多 Agent、RAG 检索、校验管线
 */
@SpringBootApplication
@ConfigurationPropertiesScan
@EnableScheduling   // 工单「用户 2 分钟未回复自动结单」定时扫描
public class PlatformApplication {

    public static void main(String[] args) {
        SpringApplication.run(PlatformApplication.class, args);
    }
}
