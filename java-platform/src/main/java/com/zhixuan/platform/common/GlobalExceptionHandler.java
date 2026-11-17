package com.zhixuan.platform.common;

import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 全局异常 → FastAPI 风格错误体 {"detail": "..."}。
 *
 * 为什么统一模仿 FastAPI 错误格式：前端 api.ts 已实现
 * 「读 body.detail → 展示」的归一化逻辑，平台层换 Java 后
 * 前端零改动（架构重构的第一原则：对外契约不变）。
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(ApiException.class)
    public ResponseEntity<Map<String, String>> handleApi(ApiException e) {
        return ResponseEntity.status(e.getStatus())
                .body(Map.of("detail", e.getMessage() == null ? "" : e.getMessage()));
    }

    @ExceptionHandler(DataAccessException.class)
    public ResponseEntity<Map<String, String>> handleDb(DataAccessException e) {
        log.error("数据库访问异常", e);
        return ResponseEntity.internalServerError()
                .body(Map.of("detail", "数据库错误"));
    }

    /**
     * 未知路径 → 404（不是 500）。
     *
     * Spring Boot 对未匹配的请求抛 NoResourceFoundException，若不单独处理会落到
     * handleGeneric 变成「服务器内部错误」——前端会误判为后端故障、监控也会误报。
     * 实测：/api/admin/handoff（正确路径是 /tickets）曾因此返回 500。
     */
    @ExceptionHandler(org.springframework.web.servlet.resource.NoResourceFoundException.class)
    public ResponseEntity<Map<String, String>> handleNotFound(
            org.springframework.web.servlet.resource.NoResourceFoundException e) {
        return ResponseEntity.status(404).body(Map.of("detail", "接口不存在"));
    }

    /**
     * Content-Type 不匹配 → 415（不是 500）。
     * 例如 login 只收 form-urlencoded（对齐 Python OAuth2PasswordRequestForm），
     * 客户端误发 JSON 时应明确告知格式问题，而不是「服务器内部错误」。
     * 实测：JSON 打 /api/auth/login 曾返回 500。
     */
    @ExceptionHandler(org.springframework.web.HttpMediaTypeNotSupportedException.class)
    public ResponseEntity<Map<String, String>> handleMediaType(
            org.springframework.web.HttpMediaTypeNotSupportedException e) {
        return ResponseEntity.status(415)
                .body(Map.of("detail", "请求格式不支持，请使用 " + e.getSupportedMediaTypes()));
    }

    // ------------------------------------------------------------------
    // V26 边界补齐：三类客户端错误原先都落到 handleGeneric → 500。
    // 客户端错误必须是 4xx，否则前端会误判为后端故障、监控误报。
    // ------------------------------------------------------------------

    /** 请求体不是合法 JSON / 类型反序列化失败 → 400（原先 500） */
    @ExceptionHandler(org.springframework.http.converter.HttpMessageNotReadableException.class)
    public ResponseEntity<Map<String, String>> handleUnreadable(
            org.springframework.http.converter.HttpMessageNotReadableException e) {
        return ResponseEntity.badRequest()
                .body(Map.of("detail", "请求体不是合法的 JSON"));
    }

    /** 路径/查询参数类型不匹配（如 /tickets/abc、page=abc）→ 400（原先 500） */
    @ExceptionHandler(org.springframework.web.method.annotation.MethodArgumentTypeMismatchException.class)
    public ResponseEntity<Map<String, String>> handleTypeMismatch(
            org.springframework.web.method.annotation.MethodArgumentTypeMismatchException e) {
        String name = e.getName() == null ? "参数" : e.getName();
        return ResponseEntity.badRequest()
                .body(Map.of("detail", "参数类型不正确: " + name));
    }

    /** 缺少必填参数（如 login 不带 username）→ 400（原先 500） */
    @ExceptionHandler(org.springframework.web.bind.MissingServletRequestParameterException.class)
    public ResponseEntity<Map<String, String>> handleMissingParam(
            org.springframework.web.bind.MissingServletRequestParameterException e) {
        return ResponseEntity.badRequest()
                .body(Map.of("detail", "缺少必填参数: " + e.getParameterName()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, String>> handleGeneric(Exception e) {
        log.error("未处理异常", e);
        return ResponseEntity.internalServerError()
                .body(Map.of("detail", "服务器内部错误"));
    }
}
