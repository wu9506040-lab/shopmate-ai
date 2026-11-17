"""用户自己的长期记忆（查看权 + 删除权）

GET    /api/me/profile          我存了什么、来自哪次会话
DELETE /api/me/profile?key=...  删一条；不带 key = 全量删除并写墓碑（不会复活）

为什么必须先有这两个接口再开 L3 写入：
原先 profile_service.clear() 只是把行软删，下一轮 get_or_create() 会立刻
重建空行——用户以为删了，实际上只是"这次看不见了"。以那条为基础做长期记忆，
等于把泄露面按会话数放大。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.models.user import User
from app.services import long_term_memory as l3

router = APIRouter(prefix="/api/me", tags=["my_profile"])


@router.get("/profile", summary="查看我的长期记忆")
def my_profile(user: User = Depends(get_current_user)):
    facts = l3.list_facts(user.id)
    return {"user_id": user.id, "count": len(facts), "facts": facts}


@router.delete("/profile", summary="删除我的长期记忆（可单条）")
def delete_my_profile(
    key: Optional[str] = Query(None, description="不传则删除全部，并阻止后续写入"),
    user: User = Depends(get_current_user),
):
    removed = l3.delete_fact(user.id, key)
    return {"deleted": removed, "key": key or "all"}
