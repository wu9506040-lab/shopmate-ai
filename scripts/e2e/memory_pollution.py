"""记忆污染体检：失败话术/内部话术有没有被写进历史，下一轮会被当事实复读。"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client import with_safe_session  # noqa: E402

PATTERNS = [
    ("兜底-没查准", "这块资料我没查准"),
    ("兜底-系统繁忙", "系统繁忙"),
    ("内部指令-未登录", "用户尚未登录"),
    ("内部指令-引导登录", "请礼貌引导用户登录"),
]


def main():
    with with_safe_session(commit=False) as db:
        total = db.execute(text("""
            SELECT COUNT(*) FROM messages WHERE role='assistant' AND deleted=0
        """)).scalar()
        print(f"assistant 消息总数: {total}")
        for name, pat in PATTERNS:
            n = db.execute(text("""
                SELECT COUNT(*) FROM messages
                WHERE role='assistant' AND deleted=0 AND content LIKE :p
            """), {"p": f"%{pat}%"}).scalar()
            print(f"  {name}: {n}")

        # 兜底话术后面还有没有下一轮（说明它进了历史、被继续对话使用）
        rows = db.execute(text("""
            SELECT m1.session_id, m1.id, LEFT(m1.content, 40), m2.content
            FROM messages m1
            JOIN messages m2 ON m2.session_id = m1.session_id
                            AND m2.id > m1.id AND m2.role='user' AND m2.deleted=0
            WHERE m1.role='assistant' AND m1.deleted=0
              AND (m1.content LIKE '%我没查准%' OR m1.content LIKE '%系统繁忙%')
            LIMIT 5
        """)).fetchall()
        print(f"\n兜底答案之后仍有后续提问的样本: {len(rows)}")
        for r in rows:
            print(f"  {r[0][:16]}… 兜底={r[2]!r} 下一问={(r[3] or '')[:40]!r}")

        # 同一会话里买家是否改过口（同一槽位出现两个不同值很难查，先看重复提问）
        rows = db.execute(text("""
            SELECT session_id, COUNT(*) c
            FROM messages
            WHERE role='user' AND deleted=0
              AND (content LIKE '%身高%' OR content LIKE '%体重%' OR content LIKE '%尺码%')
            GROUP BY session_id HAVING c > 1
            ORDER BY c DESC LIMIT 5
        """)).fetchall()
        print(f"\n同一会话内反复提尺码/身高的: {len(rows)}")
        for r in rows:
            print(f"  {r[0][:16]}… {r[1]} 次")


if __name__ == "__main__":
    main()
