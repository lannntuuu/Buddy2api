@echo off
chcp 65001 >nul 2>&1
title Buddy 2 API

cd /d "%~dp0"

echo.
echo  ========================================
echo   Buddy 2 API
echo  ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo  [错误] Python 版本过低，请安装 Python 3.10+
    pause
    exit /b 1
)

REM 使用项目隔离环境，避免修改系统 Python
if not exist ".venv\Scripts\python.exe" (
    echo  [安装] 创建项目虚拟环境...
    python -m venv .venv
    if errorlevel 1 (
        echo  [错误] 无法创建虚拟环境
        pause
        exit /b 1
    )
)

.venv\Scripts\python.exe -c "import fastapi, uvicorn, httpx, cryptography" >nul 2>&1
if errorlevel 1 (
    echo  [安装] 安装锁定依赖...
    .venv\Scripts\python.exe -m pip install -r requirements.txt -q
    if errorlevel 1 (
        echo  [错误] 依赖安装失败
        pause
        exit /b 1
    )
)

REM 启动
echo  [启动] http://127.0.0.1:8787
echo  [停止] Ctrl+C
echo.
.venv\Scripts\python.exe server.py --port 8787 %*

pause
