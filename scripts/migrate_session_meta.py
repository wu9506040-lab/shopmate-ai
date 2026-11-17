"""回填 session_meta：把历史会话的流量分类一次性补齐

为什么要回填：看板/KPI/质检改成「只认 kind='biz'」之后，历史会话没有这行记录，
要么被当成业务流量（等于没修），要么被全部排除（看板归零）。所以迁移分两步走：
先按唯一一份前缀正则把历史会话刷成 eval/debug，其余刷成 biz，再切读侧口径。

用法：
    backend/.venv/Scripts/python.exe scripts/migrate_session_meta.py [--dry-run]

幂等：已存在的 session_id 不动（避免覆盖上线后由请求头声明的真实 kind）。
"""
import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# 配置从 backend/.env 读（pydantic-settings 用相对 cwd 的 env_file），
# 所以必须先切到 backend，否则会因 JWT_SECRET 未设置直接报错。
os.chdir(ROOT / "backend")

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client import get_engine  # noqa: E402
from app.models.session_meta import KIND_BIZ, SessionMeta  # noqa: E402
from app.services.session_kind import TEST_SESSION_RE  # noqa: E402


def main(dry_run: bool = False) -> int:
    engine = get_engine()
    SessionMeta.__table__.create(bind=engine, checkfirst=True)

    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT m.session_id, MIN(m.create_time) AS first_at
            FROM messages m
            LEFT JOIN session_meta s ON s.session_id = m.session_id
            WHERE s.session_id IS NULL
            GROUP BY m.session_id
        """)).fetchall()

        if not rows:
            print("nothing to backfill")
        else:
            payload = []
            stat = {}
            for sid, first_at in rows:
                if TEST_SESSION_RE.match(sid or ""):
                    # 统一记成 eval：历史前缀里混着 debug/smoke，读侧只关心「非 biz」
                    payload.append({"sid": sid, "kind": "eval", "src": "backfill:prefix-regex"})
                    stat["eval"] = stat.get("eval", 0) + 1
                else:
                    payload.append({"sid": sid, "kind": KIND_BIZ, "src": "backfill:default"})
                    stat["biz"] = stat.get("biz", 0) + 1
            print(f"to insert: {len(payload)} sessions  {stat}")
            if not dry_run:
                conn.execute(
                    text("""
                        INSERT INTO session_meta (session_id, kind, source)
                        VALUES (:sid, :kind, :src)
                        ON DUPLICATE KEY UPDATE session_id = session_id
                    """),
                    payload,
                )

        total = conn.execute(text("SELECT COUNT(*) FROM session_meta")).scalar()
        by_kind = conn.execute(text(
            "SELECT kind, COUNT(*) FROM session_meta GROUP BY kind"
        )).fetchall()
        print(f"session_meta total={total} by_kind={dict(by_kind)}")

        # 一致性自检：业务会话里是否还残留明显的评测会话（前缀命中却记成 biz）
        leak = conn.execute(text("""
            SELECT COUNT(*) FROM session_meta
            WHERE kind = 'biz' AND (session_id LIKE 'eval%' OR session_id LIKE 'diag%'
                  OR session_id LIKE 'chk%' OR session_id LIKE 'smoke%' OR session_id LIKE 'qa-%')
        """)).scalar()
        print(f"biz-but-looks-like-test: {leak}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(main(ap.parse_args().dry_run))
