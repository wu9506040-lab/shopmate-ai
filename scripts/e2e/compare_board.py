"""看板对账：直连 AI 服务 vs 经 Java 网关，逐字段比对

存在意义：`/api/admin/analytics/board` 曾在 Python 与 Java 各有一份实现，
改一份等于没改（踩过一次：改完 Python、重启、直连 8000 正确，浏览器看到的还是旧数字）。
单源之后这个脚本有两个用途：

1. **迁移护栏**：删掉 Java 聚合前后各跑一次，数字必须逐字段一致；
2. **回归检查**：以后谁再在 Java 里加统计、或者转发漏了透传参数，这里会红。

用法：
    backend/.venv/Scripts/python.exe scripts/e2e/compare_board.py \
        --user <管理员账号> --password <密码> --days 14
    # 可选 --ai-base / --gw-base 指定服务地址

退出码：0 = 完全一致；1 = 有差异（打印路径与两边值）；2 = 请求失败。
"""
import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

IGNORE_KEYS = {"generated_at"}  # 时间戳必然不同


def _login(base, user, password):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        base + "/api/auth/login", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{base} 登录失败: HTTP {resp.status}")
        resp.read()
    return opener


def _get_board(opener, base, days):
    req = urllib.request.Request(base + f"/api/admin/analytics/board?days={days}")
    with opener.open(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def _diff(a, b, path="$", out=None):
    out = out if out is not None else []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in IGNORE_KEYS:
                continue
            if k not in a:
                out.append((f"{path}.{k}", "<缺失>", b[k]))
            elif k not in b:
                out.append((f"{path}.{k}", a[k], "<缺失>"))
            else:
                _diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((f"{path}.length", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        # 数值容忍：SQL 聚合返回 Decimal / int，序列化后可能 12 与 12.0
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                and abs(float(a) - float(b)) < 1e-6:
            return out
        out.append((path, a, b))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--ai-base", default="http://127.0.0.1:8000")
    ap.add_argument("--gw-base", default="http://127.0.0.1:8081")
    args = ap.parse_args()

    try:
        op_ai = _login(args.ai_base, args.user, args.password)
        st_ai, ai = _get_board(op_ai, args.ai_base, args.days)
        op_gw = _login(args.gw_base, args.user, args.password)
        st_gw, gw = _get_board(op_gw, args.gw_base, args.days)
    except Exception as e:  # noqa: BLE001
        print(f"请求失败: {e}")
        return 2

    print(f"AI 服务 {args.ai_base}: HTTP {st_ai}")
    print(f"Java 网关 {args.gw_base}: HTTP {st_gw}")
    if st_ai != st_gw:
        print(f"状态码不一致: {st_ai} vs {st_gw}")
        return 1

    diffs = _diff(ai, gw)
    if not diffs:
        print(f"一致：days={args.days}，全部字段逐项相同")
        return 0
    print(f"发现 {len(diffs)} 处差异：")
    for path, a, b in diffs:
        print(f"  {path}: AI={a!r}  GW={b!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
