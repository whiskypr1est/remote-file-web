@echo off
setlocal enabledelayedexpansion

REM ===========================================================================
REM  把「远程文件管理」注册为 Windows 服务（开机自启）
REM
REM  前置条件：
REM    1. 以【管理员身份】运行本脚本（右键 -> 以管理员身份运行）
REM    2. 下载 NSSM： https://nssm.cc/download
REM       解压后把 win64\nssm.exe 复制到本项目目录，或加入系统 PATH
REM
REM  执行后会：
REM    * 注册服务 RemoteFileWeb
REM    * 设置工作目录、日志文件、崩溃自动重启、开机自动启动
REM    * 立即启动服务
REM ===========================================================================

set "SERVICE_NAME=RemoteFileWeb"
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

REM ---- 管理员权限检查 ----
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 需要管理员权限。
    echo        请右键本文件，选择"以管理员身份运行"。
    echo.
    pause
    exit /b 1
)

REM ---- 定位 nssm.exe ----
set "NSSM="
for %%i in (nssm.exe) do if not "%%~$PATH:i"=="" set "NSSM=%%~$PATH:i"
if not defined NSSM if exist "%APP_DIR%\nssm.exe" set "NSSM=%APP_DIR%\nssm.exe"
if not defined NSSM if exist "%APP_DIR%\tools\nssm.exe" set "NSSM=%APP_DIR%\tools\nssm.exe"
if not defined NSSM (
    echo.
    echo [错误] 未找到 nssm.exe
    echo.
    echo        请从 https://nssm.cc/download 下载 NSSM，
    echo        解压后把 win64\nssm.exe 放到：
    echo            %APP_DIR%\
    echo        或把 nssm.exe 所在目录加入系统 PATH 后重新运行本脚本。
    echo.
    pause
    exit /b 1
)

REM ---- 选择 Python 解释器（优先项目内虚拟环境）----
set "PY=%APP_DIR%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    set "PY="
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY set "PY=%%i"
    )
)
if not defined PY (
    echo.
    echo [错误] 未找到 python.exe，请先安装 Python 并确保已加入 PATH。
    echo.
    pause
    exit /b 1
)

if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"

echo.
echo ============================================================
echo   服务名称 : %SERVICE_NAME%
echo   项目目录 : %APP_DIR%
echo   Python   : %PY%
echo   NSSM     : %NSSM%
echo ============================================================
echo.

REM ---- 已存在则先删除 ----
"%NSSM%" status %SERVICE_NAME% >nul 2>&1
if not errorlevel 1 (
    echo [信息] 检测到同名服务，先停止并删除...
    "%NSSM%" stop %SERVICE_NAME% >nul 2>&1
    "%NSSM%" remove %SERVICE_NAME% confirm >nul 2>&1
)

REM ---- 注册服务 ----
"%NSSM%" install %SERVICE_NAME% "%PY%" "app.py"
if errorlevel 1 (
    echo [错误] 服务注册失败。
    pause
    exit /b 1
)

"%NSSM%" set %SERVICE_NAME% AppDirectory "%APP_DIR%"
"%NSSM%" set %SERVICE_NAME% DisplayName "远程文件管理服务"
"%NSSM%" set %SERVICE_NAME% Description "Windows 桌面风格的局域网文件管理服务（FastAPI）"
"%NSSM%" set %SERVICE_NAME% Start SERVICE_AUTO_START

REM 日志：stdout/stderr 分别落盘，超过 10MB 自动轮转
"%NSSM%" set %SERVICE_NAME% AppStdout "%APP_DIR%\logs\service_out.log"
"%NSSM%" set %SERVICE_NAME% AppStderr "%APP_DIR%\logs\service_err.log"
"%NSSM%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM%" set %SERVICE_NAME% AppRotateOnline 1
"%NSSM%" set %SERVICE_NAME% AppRotateBytes 10485760

REM 进程异常退出时自动重启
"%NSSM%" set %SERVICE_NAME% AppExit Default Restart
"%NSSM%" set %SERVICE_NAME% AppRestartDelay 5000

REM 停止时先发 Ctrl+C，5 秒内没退出再强杀（让 lifespan 有机会清理临时文件）
"%NSSM%" set %SERVICE_NAME% AppStopMethodConsole 5000
"%NSSM%" set %SERVICE_NAME% AppStopMethodWindow 2000
"%NSSM%" set %SERVICE_NAME% AppStopMethodThreads 2000
"%NSSM%" set %SERVICE_NAME% AppThrottle 3000

echo.
echo [完成] 正在启动服务...
"%NSSM%" start %SERVICE_NAME%

echo.
echo ============================================================
echo   常用命令：
echo     查看状态 : sc query %SERVICE_NAME%
echo     启动服务 : net start %SERVICE_NAME%
echo     停止服务 : net stop  %SERVICE_NAME%
echo     删除服务 : uninstall_service.bat
echo.
echo   日志文件：
echo     %APP_DIR%\logs\service_out.log
echo     %APP_DIR%\logs\service_err.log
echo.
echo   别忘了放行防火墙端口（默认 8000）：
echo     netsh advfirewall firewall add rule name="FileWeb 8000" dir=in action=allow protocol=TCP localport=8000
echo ============================================================
echo.
pause
