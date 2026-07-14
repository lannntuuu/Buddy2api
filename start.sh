#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

echo ""
echo "  ========================================"
echo "   Buddy 2 API"
echo "  ========================================"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "  [错误] 未找到 python3"
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "  [错误] Python 版本过低，请安装 Python 3.10+"
    exit 1
fi

# 使用项目隔离环境，避免修改系统 Python
venv_dir=".venv"
if [ ! -x "$venv_dir/bin/python" ]; then
    echo "  [安装] 创建项目虚拟环境..."
    python3 -m venv "$venv_dir"
fi

if ! "$venv_dir/bin/python" -c "import fastapi, uvicorn, httpx, cryptography" 2>/dev/null; then
    echo "  [安装] 安装锁定依赖..."
    "$venv_dir/bin/python" -m pip install -r requirements.txt -q
fi

echo "  [启动] http://127.0.0.1:8787"
echo "  [停止] Ctrl+C"
echo ""

exec "$venv_dir/bin/python" server.py --port 8787 "$@"
