@echo off
setlocal

REM ===========================================================================
REM  远程文件管理 —— 一键启动脚本
REM  双击本文件即可启动服务（默认读取同目录的 config.json）
REM  也可以带参数：start.bat --port 9000
REM ===========================================================================

cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    echo [信息] 使用项目内置虚拟环境 .venv
)

echo.
echo ============================================================
echo   正在启动远程文件管理服务...
echo   停止服务请按 Ctrl+C
echo ============================================================
echo.

"%PY%" app.py %*

echo.
echo 服务已停止。
pause
