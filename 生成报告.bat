@echo off
rem 生成一份当前的 Markdown 快照报告，保存到 reports\ 并用记事本打开
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist reports mkdir reports
python "agent_backend_monitor.py" --report > "reports\snapshot.md"
start "" notepad "reports\snapshot.md"
