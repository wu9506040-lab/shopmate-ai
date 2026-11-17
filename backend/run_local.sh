#!/bin/bash
# 本地直跑入口（非 Docker）：cd 到 backend 再起 uvicorn，保证 .env 与 app 模块可见
cd "$(dirname "$0")" || exit 1
exec .venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
