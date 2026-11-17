#!/bin/bash
# 本地跑七维评测：cd 到 backend（加载 .env + venv 依赖）再执行评测器
cd "$(dirname "$0")/../backend" || exit 1
exec .venv/Scripts/python.exe ../scripts/eval_seven.py "$@"
