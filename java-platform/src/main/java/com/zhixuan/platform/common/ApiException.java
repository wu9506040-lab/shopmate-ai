package com.zhixuan.platform.common;

/**
 * 业务异常：status + detail。
 *
 * detail 序列化为 FastAPI 风格 {"detail": "..."}，
 * 前端 api.ts 的错误归一化（res.json().detail）无需任何改动即可解析。
 */
public class ApiException extends RuntimeException {

    private final int status;

    public ApiException(int status, String detail) {
        super(detail);
        this.status = status;
    }

    public int getStatus() {
        return status;
    }
}
