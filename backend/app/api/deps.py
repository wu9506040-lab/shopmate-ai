"""
API 层 Depends - get_current_user / require_admin

按 §6 规则：api/ 层依赖注入，把 User 注入到 endpoint 签名里
"""
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.clients.mysql_client import get_db
from app.core.config import settings
from app.core.security import decode_access_token
from app.models.user import User
from app.services.auth_service import get_user_by_id


def _extract_token(request: Request) -> Optional[str]:
    """取 JWT token：Authorization Bearer 优先，回落 httpOnly Cookie

    Bearer 通道专供管理台（sessionStorage 存 token，与商城 Cookie 身份
    隔离实现双端共存）；商城前端从不发送 Authorization 头 → 行为不变。
    """
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    return request.cookies.get(settings.COOKIE_NAME)


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    获取当前登录用户（可选）
    未登录 / token 无效 / 用户不存在 / 已禁用 → 返回 None
    """
    token = _extract_token(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        return None

    # P4（AI-only 模式）：不查 MySQL —— 身份来自网关签发的 JWT claims。
    # 前提：token 由 Java 中台用同一 JWT_SECRET 签发且网关已校验；
    # 这样 AI 服务完全无状态（可水平扩展），代价是「用户禁用即时生效」
    # 依赖 token 过期（网关侧已拦截，属可接受的取舍，README 有记录）。
    if settings.PLATFORM_AI_ONLY:
        from app.models.user import User as _User
        return _User(
            id=user_id_int,
            username=f"user_{user_id_int}",
            password_hash="",           # 不参与任何校验，占位
            role=str(payload.get("role") or "user"),
            status=1,
            deleted=0,
        )

    user = get_user_by_id(db, user_id_int)
    if user is None or user.status != 1:
        return None
    return user


def get_current_user(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    """获取当前登录用户（必填，未登录 401）"""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
        )
    return user


def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """要求 admin 角色（非 admin 返回 403）"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user


def require_super_admin(
    user: User = Depends(get_current_user),
) -> User:
    """要求 super_admin 角色（仅 P4-3 replay 等高权限操作）

    与 require_admin 的区别：
    - admin：可读 + 不可写破坏性操作（export / list / get_messages）
    - super_admin：可触发 replay（会调 LLM，成本 + 副作用）
    """
    if user.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要超级管理员权限（super_admin）",
        )
    return user
