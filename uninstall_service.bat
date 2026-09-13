@echo off
setlocal

REM ===========================================================================
REM  卸载「远程文件管理」Windows 服务
REM  需要管理员权限运行
REM ===========================================================================

set "SERVICE_NAME=RemoteFileWeb"

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 需要管理员权限，请右键选择"以管理员身份运行"。
    echo.
    pause
    exit /b 1
)

set "NSSM="
for %%i in (nssm.exe) do if not "%%~$PATH:i"=="" set "NSSM=%%~$PATH:i"
if not defined NSSM if exist "%~dp0nssm.exe" set "NSSM=%~dp0nssm.exe"
if not defined NSSM if exist "%~dp0tools\nssm.exe" set "NSSM=%~dp0tools\nssm.exe"

echo.
echo 即将停止并删除服务：%SERVICE_NAME%
choice /c YN /m "确定继续吗"
if errorlevel 2 (
    echo 已取消。
    pause
    exit /b 0
)

if defined NSSM (
    "%NSSM%" stop %SERVICE_NAME% >nul 2>&1
    "%NSSM%" remove %SERVICE_NAME% confirm >nul 2>&1
) else (
    REM 没有 nssm 时退回系统自带的 sc
    net stop %SERVICE_NAME% >nul 2>&1
    sc delete %SERVICE_NAME% >nul 2>&1
)

echo.
echo [完成] 服务已删除（日志文件保留在 logs 目录，可手动清理）。
echo.
pause
