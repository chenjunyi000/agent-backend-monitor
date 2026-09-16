@echo off
rem 启动桌面版（图形界面）。会优先挑选"装了 psutil"的 Python。
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PY="
for %%P in (
  "%USERPROFILE%\miniconda3\pythonw.exe"
  "%USERPROFILE%\anaconda3\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
  "pythonw.exe"
) do call :try_python "%%~P"

if defined PY (
  echo [启动器] 使用解释器: %PY%
  start "AgentBackendMonitor" "%PY%" "agent_backend_monitor.py"
) else (
  echo [启动器] 没找到装了 psutil 的解释器，用默认 python 启动（功能会降级）。
  python "agent_backend_monitor.py"
)
exit /b

:try_python
if defined PY exit /b
if not exist "%~1" exit /b
"%~1" -c "import psutil" >nul 2>nul
if errorlevel 1 exit /b
set "PY=%~1"
exit /b
