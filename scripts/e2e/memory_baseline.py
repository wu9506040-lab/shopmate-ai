"""上下文/记忆现状体检：会话长度分布 + 跨会话回访比例。

用途：给"记忆模块"定优先级——先确认"记忆窗口被撑爆"和"跨会话断片"是不是真问题。
排除非业务会话（session_meta.kind <> 'biz'）。
"""
import json
import sys

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client import with_safe_session  # noqa: E402

NON_BIZ = """
    c.session_id NOT IN (SELECT session_id FROM session_meta WHERE kind <> 'biz')
"""


def q(sql, **kw):
    with with_safe_session(commit=False) as db:
        return db.execute(text(sql), kw).fetchall()


def main():
    # 1. 每会话消息数（只算 biz）
    rows = q(f"""
        SELECT c.session_id, COUNT(m.id) AS n
        FROM conversations c
        JOIN messages m ON m.session_id = c.session_id AND m.deleted = 0
        WHERE c.deleted = 0 AND {NON_BIZ}
        GROUP BY c.session_id
    """)
    lens = sorted(int(r[1]) for r in rows)
    total = len(lens)
    if not total:
        print("无业务会话")
        return

    def pct(p):
        return lens[min(total - 1, int(total * p))]

    over20 = sum(1 for n in lens if n > 20)
    print(f"业务会话数: {total}")
    print(f"每会话消息数: p50={pct(0.5)} p90={pct(0.9)} p99={pct(0.99)} max={lens[-1]}")
    print(f"超过 20 条（超出现有记忆窗口）: {over20} ({over20 / total * 100:.1f}%)")
    print(f"超过 40 条（能看出摘要需求）: {sum(1 for n in lens if n > 40)}")

    # 2. 同一用户跨会话回访
    rows = q(f"""
        SELECT c.user_id, COUNT(DISTINCT c.session_id) AS s, COUNT(m.id) AS msgs
        FROM conversations c
        LEFT JOIN messages m ON m.session_id = c.session_id AND m.deleted = 0
        WHERE c.deleted = 0 AND c.user_id IS NOT NULL AND {NON_BIZ}
        GROUP BY c.user_id
    """)
    users = len(rows)
    multi = [r for r in rows if int(r[1]) > 1]
    print(f"\n业务用户数: {users}，多会话用户: {len(multi)} ({len(multi) / users * 100:.1f}%)")
    print(f"单用户最多会话数: {max(int(r[1]) for r in rows)}")

    # 3. 会话是否跨天（判断摘要/长期记忆的价值）
    rows = q(f"""
        SELECT c.session_id,
               COUNT(DISTINCT DATE(m.create_time)) AS days
        FROM conversations c
        JOIN messages m ON m.session_id = c.session_id AND m.deleted = 0
        WHERE c.deleted = 0 AND m.create_time IS NOT NULL AND {NON_BIZ}
        GROUP BY c.session_id
        HAVING days > 1
    """)
    print(f"跨天会话: {len(rows)}")

    # 4. 已有多久的历史（数据跨度）
    rows = q(f"""
        SELECT MIN(m.create_time), MAX(m.create_time)
        FROM messages m JOIN conversations c ON c.session_id = m.session_id
        WHERE c.deleted = 0 AND m.deleted = 0 AND {NON_BIZ}
    """)
    print(f"数据跨度: {rows[0][0]} → {rows[0][1]}")


if __name__ == "__main__":
    main()
