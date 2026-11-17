#!/bin/bash
# daemon 入口：常驻多账号进程，Cookie 由管理台「账号管理」页录入后自动重载连接。
#
# 2026-11：去掉旧版「首次启动阻塞等 QR 扫码」逻辑。
# 旧逻辑在 /root/.goofish-cli/cookies.json 不存在时 while sleep 10 死等，
# 而新版 daemon 的 AccountWorker 本来就能在凭据缺失时进入 error 状态并
# 30s 自愈重试（进程不退出）——阻塞等扫码反而让命令通道永远起不来，
# 管理台所有启停按钮都会「返回 ok 但没人消费」。
# 需要扫码时仍可手动执行：docker exec -it <容器> python /app/do_qr_login.py
set -e

mkdir -p /root/.goofish-cli /app/logs

echo "Starting xianyu daemon (multi-account; cookie via admin console)..." \
  | tee -a /app/logs/xianyu_daemon_qr.log
exec python -u /app/scripts/run_xianyu_daemon.py
