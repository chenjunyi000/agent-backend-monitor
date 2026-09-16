@echo off
rem 启动网页版：本机 127.0.0.1:8737，自动打开浏览器。关掉这个黑窗口即停止服务。
rem 会优先挑选"装了 psutil"的 Python：缺 psutil 时采集会降级（丢失内存/启动时间等信息）。
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
title Agent Backend Monitor - Web (Ctrl+C to stop)

set "PY="
for %%P in (
  "%USERPROFILE%\miniconda3\python.exe"
  "%USERPROFILE%\anaconda3\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "python.exe"
) do call :try_python "%%~P"

if not defined PY set "PY=python"
echo [启动器] 使用解释器: %PY%
"%PY%" "agent_backend_web.py" %*
pause
exit /b

:try_python
if defined PY exit /b
if not exist "%~1" exit /b
"%~1" -c "import psutil" >nul 2>nul
if errorlevel 1 exit /b
set "PY=%~1"
exit /b
