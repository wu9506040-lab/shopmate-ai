package com.zhixuan.platform.common;

import java.io.IOException;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import com.fasterxml.jackson.databind.ObjectMapper;

import jakarta.servlet.http.HttpServletResponse;

/**
 * 全局异常 → FastAPI 风格错误体 {"detail": "..."}。
 *
 * 为什么统一模仿 FastAPI 错误格式：前端 api.ts 已实现
 * 「读 body.detail → 展示」的归一化逻辑，平台层换 Java 后
 * 前端零改动（架构重构的第一原则：对外契约不变）。
 *
 * 为什么直接写 HttpServletResponse 而不是返回 ResponseEntity：
 * 浏览器 EventSource/fetch-SSE 发的 Accept 是 text/event-stream，
 * 返回 ResponseEntity<Map> 会走内容协商，JSON 错误体无法满足该 Accept
 * → HttpMediaTypeNotAcceptableException → 500 空包。实测：
 * 401/422/404 在 SSE Accept 下全部变成 500 空 body，前端的
 * detail 展示逻辑全失效。直接写 response 绕过协商，Accept 无关。
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @ExceptionHandler(ApiException.class)
    public void handleApi(ApiException e, HttpServletResponse response) {
        writeError(response, e.getStatus(), e.getMessage());
    }

    @ExceptionHandler(DataAccessException.class)
    public void handleDb(DataAccessException e, HttpServletResponse response) {
        log.error("数据库访问异常", e);
        writeError(response, 500, "数据库错误");
    }

    /**
     * 未知路径 → 404（不是 500）。
     *
     * Spring Boot 对未匹配的请求抛 NoResourceFoundException，若不单独处理会落到
     * handleGeneric 变成「服务器内部错误」——前端会误判为后端故障、监控也会误报。
     * 实测：/api/admin/handoff（正确路径是 /tickets）曾因此返回 500。
     */
    @ExceptionHandler(org.springframework.web.servlet.resource.NoResourceFoundException.class)
    public void handleNotFound(
            org.springframework.web.servlet.resource.NoResourceFoundException e,
            HttpServletResponse response) {
        writeError(response, 404, "接口不存在");
    }

    /**
     * Content-Type 不匹配 → 415（不是 500）。
     * 例如 login 只收 form-urlencoded（对齐 Python OAuth2PasswordRequestForm），
     * 客户端误发 JSON 时应明确告知格式问题，而不是「服务器内部错误」。
     * 实测：JSON 打 /api/auth/login 曾返回 500。
     */
    @ExceptionHandler(org.springframework.web.HttpMediaTypeNotSupportedException.class)
    public void handleMediaType(
            org.springframework.web.HttpMediaTypeNotSupportedException e,
            HttpServletResponse response) {
        writeError(response, 415, "请求格式不支持，请使用 " + e.getSupportedMediaTypes());
    }

    // ------------------------------------------------------------------
    // V26 边界补齐：三类客户端错误原先都落到 handleGeneric → 500。
    // 客户端错误必须是 4xx，否则前端会误判为后端故障、监控误报。
    // ------------------------------------------------------------------

    /** 请求体不是合法 JSON / 类型反序列化失败 → 400（原先 500） */
    @ExceptionHandler(org.springframework.http.converter.HttpMessageNotReadableException.class)
    public void handleUnreadable(
            org.springframework.http.converter.HttpMessageNotReadableException e,
            HttpServletResponse response) {
        writeError(response, 400, "请求体不是合法的 JSON");
    }

    /** 路径/查询参数类型不匹配（如 /tickets/abc、page=abc）→ 400（原先 500） */
    @ExceptionHandler(org.springframework.web.method.annotation.MethodArgumentTypeMismatchException.class)
    public void handleTypeMismatch(
            org.springframework.web.method.annotation.MethodArgumentTypeMismatchException e,
            HttpServletResponse response) {
        String name = e.getName() == null ? "参数" : e.getName();
        writeError(response, 400, "参数类型不正确: " + name);
    }

    /** 缺少必填参数（如 login 不带 username）→ 400（原先 500） */
    @ExceptionHandler(org.springframework.web.bind.MissingServletRequestParameterException.class)
    public void handleMissingParam(
            org.springframework.web.bind.MissingServletRequestParameterException e,
            HttpServletResponse response) {
        writeError(response, 400, "缺少必填参数: " + e.getParameterName());
    }

    @ExceptionHandler(Exception.class)
    public void handleGeneric(Exception e, HttpServletResponse response) {
        log.error("未处理异常", e);
        writeError(response, 500, "服务器内部错误");
    }

    /**
     * 绕过内容协商直接写错误体：对任何 Accept 头都返回
     * application/json 的 {"detail": ...}，与 FastAPI 行为一致。
     */
    private static void writeError(HttpServletResponse response, int status, String detail) {
        response.setStatus(status);
        response.setContentType("application/json");
        response.setCharacterEncoding("UTF-8");
        try {
            response.getWriter().write(MAPPER.writeValueAsString(
                    Map.of("detail", detail == null ? "" : detail)));
        } catch (IOException e) {
            // 响应流已断（客户端早退）写不进去，静默
            log.debug("错误响应写出失败（客户端可能已断开）: {}", e.getMessage());
        }
    }
}
