@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PY=%~dp0本地模型配置与日志\Strata验证\runtime\control-0.1.52\python\python.exe"
if not exist "%PY%" (
  echo 请下载 Release 中的 windows-x64 便携包，不要只下载源码 ZIP。
  if not defined TOOLBOX_NO_PAUSE pause
  exit /b 1
)
"%PY%" -I -X utf8 -u "%~dp0bootstrap.py" ui %*
set "RC=%ERRORLEVEL%"
echo.
if not defined TOOLBOX_NO_PAUSE pause
exit /b %RC%
