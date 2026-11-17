"""多渠道账号管理 API（管理台）

2026-11 调整目标：
1. 账号按【平台】分组展示，管理员可自行新增不同平台的账号；
2. 每个平台声明自己需要什么凭据，凭据由管理员在页面填写，不落前端、不回显；
3. 闲鱼用 Cookie 连接且会过期：
   - 保存 Cookie 即自动重载注册表 + 重启对应 worker（真连接）
   - 掉线后 daemon worker 30s 自愈重试（自动重连）
   - 持续失败超过阈值 → 标记「凭据已过期」，页面红标提醒重新填写
4. 状态诚实：daemon 心跳超时就显示离线，命令无人消费时直接 409，
   不再出现「按钮返回 ok 但什么都没发生」。

技术边界（重要）：
各平台的 API 并不相同，不能只换 key。闲鱼无官方开放 API（Cookie + WebSocket），
淘宝/京东/抖音是 OAuth 开放平台（app_key/secret/token + 签名），自建商城直连本库。
本模块统一的是【上层适配器契约】：平台声明 + 凭据结构 + connect/status 状态，
未实现适配器的平台明确返回 pending_adapter，绝不伪造「已连接」。

存储：
  backend/config/channel_accounts.json   账号注册表（管理台真相源）
  backend/config/credentials/{id}.json   各平台凭据（敏感，仅服务端读写）
  backend/config/cookies/{id}.txt        闲鱼 Cookie 文本（daemon 直接读）
  backend/config/accounts.json          旧 daemon 注册表的兼容投影（仅闲鱼账号）
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import require_admin
from app.core.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/channels", tags=["admin-channels"])

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
REGISTRY_FILE = CONFIG_DIR / "channel_accounts.json"
CRED_DIR = CONFIG_DIR / "credentials"
COOKIE_DIR = CONFIG_DIR / "cookies"
# 旧 daemon 注册表：镜像内 ACCOUNTS_FILE 指向 /app/backend/config/accounts.json
LEGACY_REGISTRY = CONFIG_DIR / "accounts.json"

COMMAND_CHANNEL = "xianyu:commands"
WORKERS_KEY = "xianyu:workers"
ERROR_SINCE_PREFIX = "xianyu:error_since:"

HEARTBEAT_TIMEOUT_SEC = 45      # 超过 45s 没上报 → daemon 离线
EXPIRED_AFTER_SEC = 300         # 连续 error 5 分钟仍重连不上 → 判定凭据过期
COOKIE_MAX_LEN = 16384          # Cookie 上限 16KB，防异常超大 body 写盘

# 纵深防御：账号 ID 是凭据/Cookie 文件名的组成部分
# （credentials/{id}.json、cookies/{id}.txt）。id 来自注册表，而注册表
# 理论上可被旧 accounts.json 迁移污染 —— 文件操作前强制白名单，
# 路径遍历（../、绝对路径、隐藏字符）在写盘前被拒。
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _ensure_safe_account_id(aid: str) -> str:
    if not _ACCOUNT_ID_RE.match(str(aid)):
        raise HTTPException(status_code=400, detail="账号 ID 格式非法")
    return str(aid)

# =============================================================
# 平台声明（新增平台只需在这里加一项 + 实现对应适配器）
# =============================================================
PLATFORMS: list[dict[str, Any]] = [
    {
        "key": "xianyu", "label": "闲鱼", "auth": "Cookie + WebSocket",
        "connector": "cookie_ws", "implemented": True,
        "note": "无官方开放 API，靠登录态 Cookie 维持长连接；Cookie 会过期，过期后需重新粘贴。",
        "fields": [
            {"key": "cookie", "label": "Cookie", "type": "textarea", "required": True,
             "placeholder": "从浏览器复制完整 Cookie（含 _m_h5_tk / cookie2 等字段）"},
        ],
    },
    {
        "key": "taobao", "label": "淘宝", "auth": "开放平台 OAuth",
        "connector": "open_api", "implemented": False,
        "note": "需淘宝开放平台应用授权，接口为 app_key + app_secret + access_token + 签名。",
        "fields": [
            {"key": "app_key", "label": "App Key", "type": "text", "required": True},
            {"key": "app_secret", "label": "App Secret", "type": "password", "required": True},
            {"key": "access_token", "label": "Access Token", "type": "password", "required": True},
        ],
    },
    {
        "key": "jd", "label": "京东", "auth": "宙斯开放平台 OAuth",
        "connector": "open_api", "implemented": False,
        "note": "需京东宙斯平台应用授权，凭据与签名体系独立于淘宝。",
        "fields": [
            {"key": "app_key", "label": "App Key", "type": "text", "required": True},
            {"key": "app_secret", "label": "App Secret", "type": "password", "required": True},
            {"key": "access_token", "label": "Access Token", "type": "password", "required": True},
        ],
    },
    {
        "key": "douyin", "label": "抖音电商", "auth": "开放平台 OAuth",
        "connector": "open_api", "implemented": False,
        "note": "需抖音开放平台店铺授权，token 有独立刷新周期。",
        "fields": [
            {"key": "app_key", "label": "App Key / Client Key", "type": "text", "required": True},
            {"key": "app_secret", "label": "App Secret", "type": "password", "required": True},
            {"key": "access_token", "label": "Access Token", "type": "password", "required": True},
        ],
    },
    {
        "key": "mall", "label": "自建商城", "auth": "内置通道（直连本系统）",
        "connector": "internal", "implemented": True,
        "note": "本系统自有商城，数据直连本地库，无需外部凭据。",
        "fields": [],
    },
]
PLATFORM_MAP = {p["key"]: p for p in PLATFORMS}


# =============================================================
# 存储
# =============================================================
def _read_registry() -> list[dict]:
    seeded = _ensure_seeded()   # 首次访问时迁移旧账号，保证任何入口都不会绕过初始化
    if seeded is not None:
        return seeded
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        return data.get("accounts", [])
    except Exception as e:
        logger.warning(f"channel_accounts.json 解析失败: {e}")
        return []


def _write_registry(accounts: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(
        json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ensure_seeded() -> list[dict] | None:
    """首次使用时把旧 accounts.json 里的闲鱼账号迁移过来，并登记内置商城通道。

    为什么不直接留空：管理员打开页面要能立刻看到「现在管理着哪些账号」，
    且旧账号 ID（a1/a2）是 daemon worker 与 Redis 状态的主键，不能丢。
    此处只迁移账号元数据，不搬 Cookie（旧 cookie_path 指向宿主机 Windows 路径，
    容器内读不到），凭据由管理员在页面重新填写。
    """
    if REGISTRY_FILE.exists():
        return
    accounts: list[dict] = []
    if LEGACY_REGISTRY.exists():
        try:
            legacy = json.loads(LEGACY_REGISTRY.read_text(encoding="utf-8"))
            for a in legacy.get("accounts", []):
                accounts.append({
                    "id": str(a.get("id")),
                    "platform": "xianyu",
                    "name": a.get("name") or str(a.get("id")),
                    "enabled": bool(a.get("enabled", True)),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            logger.warning(f"旧账号表迁移失败: {e}")
    accounts.append({
        "id": "mall-internal",
        "platform": "mall",
        "name": "猫与小鼠官方商城",
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        _write_registry(accounts)
    except OSError as e:
        # 只读挂载下降级为内存注册表：列表仍可展示，但保存会明确报错
        logger.warning(f"账号注册表不可写，降级为内存态: {e}")
    return accounts


def _read_credentials(aid: str) -> dict:
    if not _ACCOUNT_ID_RE.match(str(aid)):
        return {}
    p = CRED_DIR / f"{aid}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_credentials(aid: str, values: dict) -> Path:
    _ensure_safe_account_id(aid)
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    p = CRED_DIR / f"{aid}.json"
    p.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _credential_summary(aid: str, platform: str) -> dict:
    """只返回可展示的元信息，绝不回显凭据内容。"""
    creds = _read_credentials(aid)
    fields = [f["key"] for f in PLATFORM_MAP.get(platform, {}).get("fields", [])]
    if not fields:
        return {"configured": True, "reason": "内置通道，无需凭据", "fields": []}
    filled = [k for k in fields if str(creds.get(k, "")).strip()]
    return {
        "configured": len(filled) == len(fields),
        "filled": filled,
        "missing": [k for k in fields if k not in filled],
        "updated_at": creds.get("_updated_at"),
    }


def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _worker_states() -> dict[str, dict]:
    """读 daemon 上报的 worker 状态（含新鲜度判定）。"""
    try:
        raw = _redis().hgetall(WORKERS_KEY)
    except Exception as e:
        logger.warning(f"读取 worker 状态失败: {e}")
        return {}
    now = time.time()
    out: dict[str, dict] = {}
    for aid, payload in raw.items():
        try:
            d = json.loads(payload)
        except Exception:
            continue
        reported = float(d.get("reported_at") or 0)
        age = now - reported if reported else None
        d["online"] = bool(reported and age is not None and age <= HEARTBEAT_TIMEOUT_SEC)
        d["last_seen_sec"] = round(age, 1) if age is not None else None
        out[aid] = d
    return out


def _daemon_alive(states: dict[str, dict]) -> bool:
    return any(d.get("online") for d in states.values())


def _track_error(aid: str, status: str) -> float | None:
    """记录连续 error 的起点，用于区分「自动重连中」与「凭据已过期」。"""
    r = _redis()
    key = ERROR_SINCE_PREFIX + aid
    try:
        if status == "error":
            v = r.get(key)
            if v is None:
                r.set(key, str(time.time()), ex=3600)
                return time.time()
            return float(v)
        r.delete(key)
    except Exception:
        pass
    return None


def _publish(action: str, account_id: str) -> None:
    try:
        _redis().publish(COMMAND_CHANNEL, json.dumps(
            {"action": action, "account_id": account_id}, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"发布命令失败 {action}: {e}")


# =============================================================
# 闲鱼兼容投影：把新注册表同步成旧 daemon 能读的 accounts.json
# =============================================================
def _sync_legacy_registry(accounts: list[dict]) -> None:
    """只投影闲鱼账号；cookie_path 用容器内路径（两个容器都挂了 /app/config）。"""
    legacy = []
    for a in accounts:
        if a.get("platform") != "xianyu" or not a.get("enabled", True):
            continue
        aid = str(a.get("id"))
        cookie_file = COOKIE_DIR / f"{aid}.txt"
        legacy.append({
            "id": aid,
            "name": a.get("name") or aid,
            "cookie_path": f"/app/config/cookies/{aid}.txt" if cookie_file.exists() else "",
            "enabled": True,
        })
    try:
        LEGACY_REGISTRY.write_text(
            json.dumps({"accounts": legacy}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"同步旧注册表失败: {e}")


def _validate_cookie(cookie: str) -> str:
    """闲鱼 Cookie 基本校验：去掉 'Cookie:' 前缀，确认含登录态关键字段。"""
    c = re.sub(r"^\s*cookie\s*:\s*", "", cookie or "", flags=re.I).strip()
    c = c.replace("\r\n", "; ").replace("\n", "; ").strip()
    if len(c) < 30:
        raise HTTPException(status_code=400, detail="Cookie 内容过短，请复制完整登录态 Cookie")
    # 上限 16KB —— 正常闲鱼 Cookie 数 KB；超大输入多为误粘贴/滥用
    if len(c) > COOKIE_MAX_LEN:
        raise HTTPException(status_code=400, detail="Cookie 内容过大（上限 16KB），请检查是否粘贴了多余内容")
    hints = ("_m_h5_tk", "cookie2", "sgcookie", "unb=", "havana_lgc")
    if not any(h in c for h in hints):
        raise HTTPException(
            status_code=400,
            detail="未检测到闲鱼登录态字段（_m_h5_tk / cookie2 / sgcookie / unb），请确认复制的是完整 Cookie",
        )
    return c


def _save_cookie_file(aid: str, cookie: str) -> None:
    _ensure_safe_account_id(aid)
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    (COOKIE_DIR / f"{aid}.txt").write_text(cookie, encoding="utf-8")


# =============================================================
# 请求体
# =============================================================
class NewAccountReq(BaseModel):
    platform: str = Field(..., description="平台 key")
    name: str = Field(..., min_length=1, max_length=64)
    credentials: dict[str, str] | None = Field(None, description="创建时一并填写的凭据")


class CredentialReq(BaseModel):
    credentials: dict[str, str] = Field(..., description="按平台声明的字段填写")


# =============================================================
# 端点
# =============================================================
@router.get("/accounts", summary="多渠道账号列表（按平台分组 + 连接状态）")
def list_accounts(_admin: User = Depends(require_admin)) -> dict:
    accounts = _read_registry()
    states = _worker_states()
    alive = _daemon_alive(states)
    now = time.time()

    items: list[dict] = []
    for a in accounts:
        aid = str(a.get("id"))
        platform = str(a.get("platform") or "xianyu")
        pdef = PLATFORM_MAP.get(platform, {})
        cred = _credential_summary(aid, platform)
        item: dict[str, Any] = {
            "id": aid,
            "platform": platform,
            "platform_label": pdef.get("label", platform),
            "connector": pdef.get("connector"),
            "adapter_implemented": bool(pdef.get("implemented")),
            "name": a.get("name"),
            "enabled": bool(a.get("enabled", True)),
            "created_at": a.get("created_at"),
            "credential": cred,
            "connection": "offline",
            "status_label": "离线",
            "reconnecting": False,
            "expired": False,
            "last_error": "",
        }

        if pdef.get("connector") == "internal":
            item.update(connection="internal", status_label="内置通道 · 无需连接")
        elif not pdef.get("implemented"):
            item.update(connection="pending_adapter",
                        status_label="待接入适配器（凭据已登记）" if cred["configured"] else "待接入适配器")
        else:
            st = states.get(aid)
            if not st or not st.get("online"):
                item.update(connection="offline",
                            status_label="连接服务未运行" if not alive else "账号未上报状态")
            else:
                status = str(st.get("status") or "")
                err = str(st.get("last_error") or "")
                error_since = _track_error(aid, status)
                duration = (now - error_since) if error_since else 0
                if status == "running":
                    item.update(connection="online", status_label="已连接")
                elif status == "error":
                    expired = bool(cred["configured"] and duration >= EXPIRED_AFTER_SEC)
                    item.update(
                        connection="expired" if expired else "reconnecting",
                        status_label="凭据已过期，请重新填写" if expired else "连接中断 · 自动重连中",
                        reconnecting=not expired,
                        expired=expired,
                        last_error=err[:160],
                    )
                elif status == "starting":
                    item.update(connection="starting", status_label="连接中")
                else:
                    item.update(connection="offline", status_label="已停止", last_error=err[:160])
                item["error_duration_sec"] = round(duration, 1) if error_since else 0
            item["daemon_alive"] = alive
        items.append(item)

    groups = []
    for p in PLATFORMS:
        members = [x for x in items if x["platform"] == p["key"]]
        groups.append({
            **{k: p[k] for k in ("key", "label", "auth", "connector", "implemented", "note", "fields")},
            "account_count": len(members),
            "accounts": members,
        })
    return {
        "platforms": PLATFORMS,
        "groups": groups,
        "accounts": items,
        "total": len(items),
        "daemon_alive": alive,
        "heartbeat_timeout_sec": HEARTBEAT_TIMEOUT_SEC,
        "expired_after_sec": EXPIRED_AFTER_SEC,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/accounts", summary="新增平台账号（可同时填凭据并自动连接）")
def create_account(req: NewAccountReq, _admin: User = Depends(require_admin)) -> dict:
    if req.platform not in PLATFORM_MAP:
        raise HTTPException(status_code=400, detail=f"未知平台：{req.platform}")
    accounts = _read_registry()
    aid = "acc-" + uuid.uuid4().hex[:8]
    account = {
        "id": aid,
        "platform": req.platform,
        "name": req.name.strip(),
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    accounts.append(account)
    _write_registry(accounts)
    _sync_legacy_registry(accounts)

    connected = False
    if req.credentials:
        _apply_credentials(aid, req.platform, req.credentials, accounts)
        connected = True
    logger.info("新增渠道账号: id=%s platform=%s", aid, req.platform)
    return {"ok": True, "account": account, "auto_connect_triggered": connected}


@router.post("/accounts/{account_id}/credentials", summary="保存凭据并自动连接")
def save_credentials(account_id: str, req: CredentialReq,
                     _admin: User = Depends(require_admin)) -> dict:
    accounts = _read_registry()
    acc = next((a for a in accounts if str(a.get("id")) == account_id), None)
    if acc is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    result = _apply_credentials(account_id, str(acc.get("platform")), req.credentials, accounts)
    return {
        "ok": True,
        "account_id": account_id,
        "platform": acc.get("platform"),
        "daemon_alive": result["daemon_alive"],
        "auto_connect_triggered": result["commands_issued"] > 0,
        "message": (
            "凭据已保存并触发自动连接，稍后页面会刷新连接状态"
            if result["daemon_alive"]
            else "凭据已保存；连接服务（daemon）当前未运行，启动后会自动连接，无需再次填写"
        ),
    }


def _apply_credentials(aid: str, platform: str, values: dict, accounts: list[dict]) -> dict:
    pdef = PLATFORM_MAP.get(platform)
    if not pdef:
        raise HTTPException(status_code=400, detail=f"未知平台：{platform}")
    required = [f["key"] for f in pdef["fields"] if f.get("required")]
    missing = [k for k in required if not str(values.get(k, "")).strip()]
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少必填凭据：{', '.join(missing)}")

    clean = {k: str(v).strip() for k, v in values.items() if k in [f["key"] for f in pdef["fields"]]}
    clean["_updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_credentials(aid, clean)

    commands = 0
    if platform == "xianyu":
        _save_cookie_file(aid, _validate_cookie(clean["cookie"]))
        _sync_legacy_registry(accounts)
        # reload 让 daemon 读到新注册表（cookie_path 变化会重启 worker），
        # restart 兜底确保立即用新 Cookie 重连，而不是等 30s 自愈周期。
        _publish("account_reload", aid)
        _publish("account_restart", aid)
        commands = 2
    states = _worker_states()
    return {"commands_issued": commands, "daemon_alive": _daemon_alive(states)}


@router.post("/accounts/{account_id}/{action}", summary="启动/停止/重启账号连接")
def account_action(account_id: str, action: str,
                   _admin: User = Depends(require_admin)) -> dict:
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="非法操作")
    accounts = _read_registry()
    acc = next((a for a in accounts if str(a.get("id")) == account_id), None)
    if acc is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    platform = str(acc.get("platform"))
    if not PLATFORM_MAP.get(platform, {}).get("implemented"):
        raise HTTPException(status_code=409, detail=f"{PLATFORM_MAP[platform]['label']} 适配器尚未接入，无法{action}")
    states = _worker_states()
    if not _daemon_alive(states):
        raise HTTPException(
            status_code=409,
            detail="连接服务（daemon）未运行，命令无人消费；请先启动连接服务或在凭据保存后等待自动连接",
        )
    _publish(f"account_{action}", account_id)
    return {"ok": True, "action": action, "account_id": account_id}


@router.delete("/accounts/{account_id}", summary="移除账号（禁用并清理凭据）")
def remove_account(account_id: str, _admin: User = Depends(require_admin)) -> dict:
    _ensure_safe_account_id(account_id)  # unlink 前置白名单（纵深防御）
    accounts = _read_registry()
    kept = [a for a in accounts if str(a.get("id")) != account_id]
    if len(kept) == len(accounts):
        raise HTTPException(status_code=404, detail="账号不存在")
    _write_registry(kept)
    _sync_legacy_registry(kept)
    for p in (CRED_DIR / f"{account_id}.json", COOKIE_DIR / f"{account_id}.txt"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    _publish("account_stop", account_id)
    _publish("account_reload", account_id)
    return {"ok": True, "removed": account_id}
