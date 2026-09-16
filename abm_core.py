#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 后端监控器 (Agent Backend & Port Monitor)
================================================

用途
----
很多 AI Agent 工具（Claude Code / Claude Desktop / Codex / DSH / Cursor / VS Code 扩展 ...）
会在自己的进程下面再启动"后端"：本地 HTTP 服务、MCP server、语言服务器、数据库代理等。
这类后端是 Agent 的子进程，Agent 一退出，后端往往被一起收走，端口随即失效。

本工具把这种"谁在谁的进程下面、开了哪个端口、用什么命令和目录启动的"完整展示出来：

  * Agent 与后端：按真实父子关系展开的进程树，标注角色（Agent 主体 / 后端 / 辅助进程）
  * 端口总览：所有监听端口 → 占用进程 → 归属哪个 Agent
  * 事件时间线：后端何时出现、何时消失，以及"Agent 退出 → 连带后端一起终止"的关联事件
  * 进程详情：完整命令行、工作目录、启动时间、父进程链、监听端口、内存/线程、可执行文件路径
  * 操作：结束进程树、复制启动命令、脱离 Agent 独立重启、导出 Markdown/JSON 报告

本模块是纯采集内核（无 GUI 依赖），被两个前端共用：

    * agent_backend_monitor.py   桌面版（tkinter 图形界面）
    * agent_backend_web.py       网页版（本地 HTTP 服务 + 浏览器界面）

依赖: psutil（推荐）；无 psutil 时自动降级用 PowerShell CIM 采集。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

# --------------------------------------------------------------------------- #
# 常量与路径
# --------------------------------------------------------------------------- #

APP_NAME = "Agent 后端监控器"
VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
RULES_FILE = os.path.join(CONFIG_DIR, "agents.json")

IS_WINDOWS = os.name == "nt"

# 深色主题（桌面版配色，与网页版观感保持一致）
C_BG = "#101319"
C_PANEL = "#191d26"
C_PANEL2 = "#1f2431"
C_PANEL3 = "#272d3c"
C_LINE = "#2b3242"
C_FG = "#e8ecf4"
C_DIM = "#9aa3b4"
C_DIM2 = "#6f7a8d"
C_ACCENT = "#5aa9ff"
C_GREEN = "#45d6b0"
C_ORANGE = "#f0b060"
C_RED = "#ff6b7a"
C_PURPLE = "#c98cff"
C_CYAN = "#5ad2e6"

ROLE_AGENT = "agent"
ROLE_CORE = "core"
ROLE_BACKEND = "backend"
ROLE_CHILD = "child"

ROLE_LABEL = {
    ROLE_AGENT: "Agent 主体",
    ROLE_CORE: "辅助进程",
    ROLE_BACKEND: "后端服务",
    ROLE_CHILD: "子进程",
}

ROLE_ICON = {
    ROLE_AGENT: "◆",
    ROLE_CORE: "·",
    ROLE_BACKEND: "▲",
    ROLE_CHILD: "○",
}

# 已知 Agent / IDE 识别规则（首次运行会写入 config/agents.json，可自由增删）
DEFAULT_RULES = [
    dict(id="claude-desktop", label="Claude Desktop", category="agent", color="#d97757",
         proc=[r"^claude(\.exe)?$"],
         cmd=[r"WindowsApps[\\/]Claude_", r"AnthropicClaude", r"[\\/]Claude[\\/]app-"]),
    dict(id="claude-code", label="Claude Code (CLI)", category="agent", color="#e08a6a",
         proc=[r"^(claude|node|bun|deno)(\.exe)?$"],
         cmd=[r"@anthropic-ai[\\/]claude-code", r"claude-code[\\/]", r"\.local[\\/]bin[\\/]claude"]),
    dict(id="codex", label="Codex CLI", category="agent", color="#10a37f",
         proc=[r"^(codex|node|bun)(\.exe)?$"],
         cmd=[r"@openai[\\/]codex", r"codex-cli", r"codex\.js", r"[\\/]codex(\.exe)?$"]),
    dict(id="gemini-cli", label="Gemini CLI", category="agent", color="#4285f4",
         proc=[r"^(gemini|node|bun)(\.exe)?$"],
         cmd=[r"@google[\\/]gemini-cli", r"gemini-cli"]),
    dict(id="qwen-code", label="Qwen Code", category="agent", color="#615ced",
         proc=[r"^(qwen|node)(\.exe)?$"],
         cmd=[r"@qwen-code", r"qwen-code"]),
    dict(id="dsh", label="DSH / DeepSeek Harness", category="agent", color="#4d6bfe",
         proc=[r"^(dsh|node|bun)(\.exe)?$"],
         cmd=[r"@deepseek-ai[\\/]dsh", r"[\\/]dsh[\\/]", r"deepseek-ai[\\/]dsh"]),
    dict(id="cursor", label="Cursor", category="ide", color="#7c8cff",
         proc=[r"^Cursor(\.exe)?$"], cmd=[]),
    dict(id="vscode", label="VS Code", category="ide", color="#3aa0f3",
         proc=[r"^Code(- Insiders)?(\.exe)?$", r"^code(\.exe)?$"], cmd=[]),
    dict(id="windsurf", label="Windsurf", category="ide", color="#09b6a2",
         proc=[r"^Windsurf(\.exe)?$"], cmd=[]),
    dict(id="trae", label="Trae", category="ide", color="#e0533d",
         proc=[r"^Trae(\.exe)?$"], cmd=[]),
    dict(id="zed", label="Zed", category="ide", color="#9c8cff",
         proc=[r"^zed(\.exe)?$"], cmd=[]),
    dict(id="aider", label="Aider", category="agent", color="#f0a13a",
         proc=[r"^(python|pythonw|aider)(\.exe)?$"], cmd=[r"aider"]),
    dict(id="opencode", label="OpenCode", category="agent", color="#8a8f98",
         proc=[r"^(opencode|node|bun)(\.exe)?$"], cmd=[r"opencode"]),
    dict(id="crush", label="Crush", category="agent", color="#c586c0",
         proc=[r"^(crush|node)(\.exe)?$"], cmd=[r"@charmland[\\/]crush", r"[\\/]crush[\\/]"]),
    dict(id="goose", label="Goose", category="agent", color="#4ec9b0",
         proc=[r"^(goose|node)(\.exe)?$"], cmd=[r"block[\\/]goose", r"[\\/]goose[\\/]"]),
    dict(id="cline", label="Cline / Roo Code", category="agent", color="#f0a13a",
         proc=[r"^node(\.exe)?$"], cmd=[r"cline", r"roo-code", r"roo-cline"]),
    dict(id="copilot-cli", label="GitHub Copilot CLI", category="agent", color="#7c8cff",
         proc=[r"^(copilot|node)(\.exe)?$"], cmd=[r"@github[\\/]copilot", r"copilot-cli"]),
    dict(id="continue", label="Continue", category="agent", color="#56b6c2",
         proc=[r"^node(\.exe)?$"], cmd=[r"@continuedev", r"continue[\\/]extensions"]),
]

# 后端（服务）识别
BACKEND_NAME_RE = re.compile(
    r"^(node|nodejs|python|pythonw|python3|python3\.\d+|deno|bun|java|javaw|dotnet|php|ruby|"
    r"go|uvicorn|gunicorn|hypercorn|nginx|caddy|httpd|apache|ollama|llama-server|"
    r"redis-server|mysqld|mariadbd|postgres|mongod|memcached|nats-server|"
    r"vite|next|esbuild|tsx|ts-node|nodemon|npm|npx|pnpm|yarn|watchman|"
    r"language_server|gopls|rust-analyzer|clangd|pylsp|jdtls|OmniSharp)(\.exe|\.cmd)?$",
    re.I)
HTTP_HINT_RE = re.compile(
    r"(uvicorn|gunicorn|hypercorn|flask|fastapi|django|runserver|vite|next\s+dev|nuxt|webpack|"
    r"esbuild|nodemon|http-server|serve\b|server\.js|server\.py|app\.py|main\.py|"
    r"node\s+\S*server|deno\s+run|bun\s+run|dotnet\s+\S+\.dll|java\s+-jar|php\s+-S|"
    r"rails|puma|spring-boot|ollama|llama-server|nginx|caddy|"
    r"--port|--listen|--host|-p\s*\d+|\bPORT=)",
    re.I)
MCP_HINT_RE = re.compile(
    r"(?<![A-Za-z0-9])mcp(?![A-Za-z0-9])|mcp[-_]?server|mcpservers|modelcontextprotocol|"
    r"model-context-protocol|@modelcontextprotocol", re.I)
DB_HINT_RE = re.compile(
    r"(redis|mysql|mariadb|postgres|mongod|sqlite|elasticsearch|clickhouse|qdrant|milvus|chroma|weaviate)",
    re.I)
HELPER_HINT_RE = re.compile(
    r"--type=(renderer|gpu-process|utility|crashpad-handler|zygote|ppapi|broker)|"
    r"crashpad|--utility-sub-type|--in-process-gpu", re.I)
# 这些进程本身是系统/基础设施，不算某个 Agent 的后端
SYSTEM_PROC_RE = re.compile(
    r"^(system|registry|smss|csrss|wininit|winlogon|services|lsass|svchost|fontdrvhost|"
    r"dwm|spoolsv|explorer|runtimebroker|sihost|taskhostw|ctfmon|conhost|searchindexer|"
    r"securityhealthservice|audiodg|msmpeng|nissrv|wmiprvse|dllhost|shellexperiencehost|"
    r"startmenuexperiencehost|textinputhost|applicationframehost|systemsettings|"
    r"lockapp|useroobebroker|sppsvc|trustedinstaller|memcompression)(\.exe)?$",
    re.I)

# 常见的系统 / 基础设施端口（"隐藏系统端口"过滤用）
SYSTEM_PORTS = {135, 139, 445, 5040, 5353, 5355, 5357, 7680, 1900, 3702, 137, 138,
                49664, 49665, 49666, 49667, 49668, 49669, 49670, 49671, 49672, 49673,
                49674, 49675, 49676, 49677, 49678, 49679, 49680}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def human_size(nbytes) -> str:
    if not nbytes:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(nbytes)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return "-"


def human_duration(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def fmt_time(ts) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def fmt_clock(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def short(text, limit=200) -> str:
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quote_ps(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def split_cmdline(cmdline: str):
    """把 Windows 命令行拆成参数列表（用系统自身的解析器，路径带空格也不会错）。"""
    cmdline = cmdline or ""
    if not cmdline.strip():
        return []
    if IS_WINDOWS:
        try:
            argc = ctypes.c_int(0)
            fn = ctypes.windll.shell32.CommandLineToArgvW
            fn.restype = ctypes.POINTER(ctypes.c_wchar_p)
            fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            argv = fn(cmdline, ctypes.byref(argc))
            if argv:
                out = [argv[i] for i in range(argc.value)]
                ctypes.windll.kernel32.LocalFree(argv)
                if out:
                    return out
        except Exception:
            pass
    import shlex
    try:
        return shlex.split(cmdline, posix=not IS_WINDOWS)
    except Exception:
        m = re.match(r'^("[^"]+"|\S+)\s*(.*)$', cmdline)
        return [m.group(1).strip('"')] + ([m.group(2)] if m.group(2) else [])


def compile_any(patterns):
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.I))
        except re.error:
            pass
    return out


# --------------------------------------------------------------------------- #
# 直接读目标进程 PEB（不依赖 psutil 也能拿到工作目录 / 命令行）
# --------------------------------------------------------------------------- #

class _UNICODE_STRING64(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                ("_pad", ctypes.c_uint), ("Buffer", ctypes.c_ulonglong)]


class _PROCESS_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


# RTL_USER_PROCESS_PARAMETERS（x64）里的偏移
_PEB_PROCESS_PARAMETERS = 0x20
_PARAMS_CURRENT_DIRECTORY = 0x38
_PARAMS_COMMAND_LINE = 0x70


def probe_psutil(python_exe, timeout=20):
    """某个解释器是否装了 psutil。"""
    try:
        out = subprocess.run([python_exe, "-c", "import psutil;print(psutil.__version__)"],
                             capture_output=True, text=True, timeout=timeout,
                             creationflags=CREATE_NO_WINDOW)
        return out.returncode == 0 and bool((out.stdout or "").strip())
    except Exception:
        return False


def find_psutil_python():
    """在本机常见位置找一个装了 psutil 的 Python，返回路径（找不到返回空串）。"""
    import glob
    home = os.environ.get("USERPROFILE", "")
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [os.path.join(home, "miniconda3", "python.exe"),
             os.path.join(home, "anaconda3", "python.exe")]
    for ver in ("Python313", "Python312", "Python311", "Python310", "Python39"):
        cands.append(os.path.join(local, "Programs", "Python", ver, "python.exe"))
    cands += glob.glob(r"C:\Python3*\python.exe")
    for path in cands:
        if path and os.path.isfile(path) and probe_psutil(path):
            return path
    return ""


def try_enable_psutil(install=False):
    """让当前进程用上 psutil。

    先直接 import（用户手动装好、或换了带 psutil 的解释器会成功）；
    install=True 时先用 pip 装一次再 import。成功后就地替换模块级 psutil，
    采集立刻恢复全量模式，无需重启服务。
    """
    import importlib
    global psutil
    if psutil is not None:
        return {"ok": True, "already": True, "version": getattr(psutil, "__version__", "?")}
    output = ""
    if install:
        try:
            proc = subprocess.run([sys.executable, "-m", "pip", "install", "psutil"],
                                  capture_output=True, text=True, timeout=300,
                                  creationflags=CREATE_NO_WINDOW)
            output = ((proc.stdout or "") + (proc.stderr or ""))[-1500:]
        except Exception as e:
            return {"ok": False, "error": f"pip 安装失败：{e}", "output": output,
                    "suggest": find_psutil_python()}
    try:
        psutil = importlib.import_module("psutil")
    except Exception as e:
        return {"ok": False, "error": f"仍未安装 psutil（{e}）", "output": output,
                "suggest": find_psutil_python()}
    return {"ok": True, "version": getattr(psutil, "__version__", "?"), "output": output,
            "suggest": ""}


def read_peb_strings(pid):
    """读目标进程的 (工作目录, 命令行)。纯 ctypes，不依赖 psutil。

    读不到（权限不足 / 32 位目标 / 进程已退出）就返回 (None, None)。
    """
    if not IS_WINDOWS or not pid:
        return None, None
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    k32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    ntdll.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long

    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, int(pid))
    if not handle:
        return None, None
    try:
        pbi = _PROCESS_BASIC_INFORMATION64()
        ret = ctypes.c_ulong(0)
        if ntdll.NtQueryInformationProcess(ctypes.c_void_p(handle), 0, ctypes.byref(pbi),
                                           ctypes.sizeof(pbi), ctypes.byref(ret)) != 0:
            return None, None
        if not pbi.PebBaseAddress:
            return None, None
        read = ctypes.c_size_t(0)
        ptr = ctypes.c_ulonglong(0)
        if not k32.ReadProcessMemory(ctypes.c_void_p(handle),
                                     ctypes.c_void_p(pbi.PebBaseAddress + _PEB_PROCESS_PARAMETERS),
                                     ctypes.byref(ptr), ctypes.sizeof(ptr), ctypes.byref(read)):
            return None, None
        params = ptr.value
        if not params:
            return None, None

        def read_ustring(offset):
            us = _UNICODE_STRING64()
            if not k32.ReadProcessMemory(ctypes.c_void_p(handle),
                                         ctypes.c_void_p(params + offset),
                                         ctypes.byref(us), ctypes.sizeof(us),
                                         ctypes.byref(read)):
                return None
            if not us.Buffer or not us.Length:
                return None
            buf = ctypes.create_unicode_buffer(us.Length // 2 + 1)
            if not k32.ReadProcessMemory(ctypes.c_void_p(handle), ctypes.c_void_p(us.Buffer),
                                         ctypes.cast(buf, ctypes.c_void_p), us.Length,
                                         ctypes.byref(read)):
                return None
            return buf.value or None

        cwd = read_ustring(_PARAMS_CURRENT_DIRECTORY)
        if cwd and cwd.endswith("\\") and len(cwd) > 3:
            cwd = cwd[:-1]
        return cwd, read_ustring(_PARAMS_COMMAND_LINE)
    except Exception:
        return None, None
    finally:
        try:
            k32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass


def _safe_call(fn, default=""):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# 以“脱离当前进程树”的方式启动进程
# --------------------------------------------------------------------------- #

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _spawn_via_wmi(args, cwd=None):
    """用 WMI 的 Win32_Process.Create 创建进程。

    新进程的父进程是 WmiPrvSE.exe，既不在我们所在的 Job 对象里，也没有继承控制台，
    这是最接近“后端摆脱 Agent 独立运行”的方式（普通 CreateProcess 做不到：
    子进程会继承父进程的 Job，被上层连坐回收）。
    """
    if not IS_WINDOWS or not args:
        return 0
    cmdline = subprocess.list2cmdline([str(a) for a in args])
    script = (
        "$ErrorActionPreference='Stop';"
        "$a = @{ CommandLine = " + quote_ps(cmdline) + ";"
        + ((" CurrentDirectory = " + quote_ps(cwd) + ";") if cwd else "")
        + " };"
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments $a;"
        "Write-Output ($r.ProcessId)"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=40,
            creationflags=CREATE_NO_WINDOW)
    except Exception:
        return 0
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit() and int(line) > 0:
            return int(line)
    return 0


def _spawn_popen(args, cwd=None):
    """普通 CreateProcess 路径：DETACHED + 尽量 BREAKAWAY。"""
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | (DETACHED_PROCESS if IS_WINDOWS else 0)
    last_err = None
    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            p = subprocess.Popen(args, cwd=cwd or None, creationflags=flags | extra,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
            return p.pid
        except OSError as e:      # 含 PermissionError（所在 Job 不允许 breakaway）
            last_err = e
    raise last_err or RuntimeError("CreateProcess 启动失败")


def _find_pids_by_cmdline(argv, exclude=()):
    """按参数列表精确匹配进程（用于任务计划程序方式下找回新进程的 PID）。"""
    if psutil is None or not argv:
        return []
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = p.info["pid"]
            if pid in exclude:
                continue
            if list(p.info.get("cmdline") or []) == list(argv):
                out.append(pid)
        except Exception:
            continue
    return out


def _spawn_via_scheduler(args, cwd=None, exclude=()):
    """兜底路径：把命令写成临时 .cmd，交给任务计划程序执行。

    进程由计划程序服务（svchost）拉起，既不继承我们的 Job 也没有我们的控制台，
    因此在“监控器本身跑在某个 Agent 的 Job 里”这种恶劣情况下仍然能脱离。
    """
    if not IS_WINDOWS:
        return 0
    cmdline = subprocess.list2cmdline([str(a) for a in args])
    name = f"ABM_relaunch_{os.getpid()}_{int(time.time() * 1000) % 1000000}"
    wrapper = os.path.join(tempfile.gettempdir(), name + ".cmd")
    lines = ["@echo off", "chcp 65001 >nul"]
    if cwd:
        lines.append(f'cd /d "{cwd}"')
    lines.append(cmdline)
    try:
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write("\r\n".join(lines) + "\r\n")
    except Exception:
        return 0
    flag = CREATE_NO_WINDOW
    before = set(_find_pids_by_cmdline(args, exclude))    # 先记下已有的同命令进程
    try:
        subprocess.run(["schtasks", "/create", "/f", "/tn", name, "/tr", wrapper,
                        "/sc", "once", "/st", "00:00"],
                       capture_output=True, timeout=40, creationflags=flag)
        subprocess.run(["schtasks", "/run", "/tn", name],
                       capture_output=True, timeout=40, creationflags=flag)
        subprocess.run(["schtasks", "/delete", "/f", "/tn", name],
                       capture_output=True, timeout=40, creationflags=flag)
    except Exception:
        return 0
    # 计划程序不会告诉我们 PID，只能等一会儿再按参数列表找"新出现"的那个
    for _ in range(8):
        time.sleep(0.5)
        fresh = [pid for pid in _find_pids_by_cmdline(args, exclude) if pid not in before]
        if fresh:
            return fresh[0]
    return 0


def spawn_detached(args, cwd=None, verify_seconds=1.2, exclude=()):
    """尽力启动一个脱离当前进程树 / Job 的进程，并确认它真的活着。

    依次尝试 WMI → CreateProcess → 任务计划程序；返回
    ``{"pid":…, "how":…, "alive":bool, "attempts":[…] }``。
    “确认存活”这一步很关键：进程被上层 Job 回收时，仅凭 Popen 成功会误报成功。
    """
    if not args:
        raise ValueError("命令行为空")
    attempts = []
    for how, fn in (("WMI", lambda a, c: _spawn_via_wmi(a, c)),
                    ("CreateProcess", lambda a, c: _spawn_popen(a, c)),
                    ("任务计划程序", lambda a, c: _spawn_via_scheduler(a, c, exclude))):
        try:
            pid = fn(args, cwd)
        except Exception as e:
            attempts.append({"how": how, "error": f"{type(e).__name__}: {e}"})
            continue
        if not pid:
            attempts.append({"how": how, "error": "未能获得新进程号"})
            continue
        alive = process_alive(pid, verify_seconds)
        attempts.append({"how": how, "pid": pid, "alive": alive})
        if alive:
            return {"pid": pid, "how": how, "alive": True, "attempts": attempts}
    last = attempts[-1] if attempts else {"how": "-"}
    return {"pid": last.get("pid", 0), "how": last.get("how", "-"),
            "alive": False, "attempts": attempts}


def listen_map():
    """{(proto, port): pid}：当前所有监听端口及其占用进程；psutil 不可用时返回 None。"""
    if psutil is None:
        return None
    out = {}
    try:
        for c in psutil.net_connections(kind="inet"):
            if not c.pid or not c.laddr:
                continue
            try:
                proto = "TCP" if int(c.type) == 1 else "UDP"
            except Exception:
                proto = "TCP"
            if proto == "TCP" and c.status != psutil.CONN_LISTEN:
                continue
            out[(proto, c.laddr.port)] = c.pid
    except Exception:
        return None
    return out


def _port_is_listening(port, timeout=0.35):
    """不依赖 psutil 的监听判断：试着连一下（IPv4 / IPv6 环回都试）。

    注意有些服务只绑 ::1（比如 Vite），所以两个都要试。
    """
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                               socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex((host, int(port))) == 0:
                    return True
        except Exception:
            continue
    return False


def port_owners(ports):
    """给定 [(proto, port)]，返回 {port: pid}。

    有 psutil 时给出真实占用者；没有时退化为连通性探测（占用者记为 0 = 未知）。
    """
    m = listen_map()
    if m is not None:
        return {port: m[(proto, port)] for proto, port in ports if (proto, port) in m}
    return {port: 0 for _proto, port in ports if _port_is_listening(port)}


def wait_ports_free(ports, timeout=8.0, ignore_pids=()):
    """等这些端口不再被监听。返回仍被占用的 {port: pid}（空字典表示都释放了）。"""
    ports = list(ports)
    if not ports:
        return {}
    deadline = time.time() + max(0.0, timeout)
    while True:
        owners = {p: pid for p, pid in port_owners(ports).items() if pid not in ignore_pids}
        if not owners or time.time() >= deadline:
            return owners
        time.sleep(0.25)


def wait_ports_listening(ports, timeout=15.0):
    """等这些端口被重新监听。返回 {port: pid}（已开始监听的部分）。"""
    ports = list(ports)
    if not ports:
        return {}
    deadline = time.time() + max(0.0, timeout)
    while True:
        owners = port_owners(ports)
        if len(owners) >= len(ports) or time.time() >= deadline:
            return owners
        time.sleep(0.35)


def process_alive(pid, wait=1.0):
    """等一小会儿再确认新进程是否真的活着（很多失败是启动后立刻退出）。"""
    if not pid:
        return False
    time.sleep(max(0.0, wait))
    if psutil is not None:
        try:
            return psutil.Process(pid).is_running()
        except Exception:
            return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=CREATE_NO_WINDOW)
        return str(pid) in (out.stdout or "")
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #

class Rule:
    __slots__ = ("id", "label", "category", "color", "proc_re", "cmd_re")

    def __init__(self, d):
        self.id = d.get("id") or "unknown"
        self.label = d.get("label") or self.id
        self.category = d.get("category") or "agent"
        self.color = d.get("color") or C_ACCENT
        self.proc_re = compile_any(d.get("proc"))
        self.cmd_re = compile_any(d.get("cmd"))

    def match(self, name: str, cmdline: str, exe: str) -> bool:
        if self.proc_re and not any(r.search(name or "") for r in self.proc_re):
            return False
        if self.cmd_re:
            hay = f"{cmdline or ''} {exe or ''}"
            if not any(r.search(hay) for r in self.cmd_re):
                return False
        return bool(self.proc_re or self.cmd_re)


def load_rules():
    """从 config/agents.json 读取规则，不存在则写出一份默认规则。"""
    rules = None
    if os.path.isfile(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("agents") if isinstance(data, dict) else data
            if isinstance(items, list) and items:
                rules = [Rule(x) for x in items if isinstance(x, dict)]
        except Exception:
            rules = None
    if not rules:
        rules = [Rule(x) for x in DEFAULT_RULES]
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(RULES_FILE, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "_说明": "proc/cmd 为正则表达式（不区分大小写）。proc 匹配进程名，cmd 匹配命令行或可执行文件路径。category 可为 agent / ide / runtime。",
                           "agents": DEFAULT_RULES}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return rules


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

class Proc:
    """一个进程的快照信息。"""

    __slots__ = ("pid", "ppid", "name", "exe", "cmdline", "argv", "cwd", "create_time",
                 "username", "status", "rss", "threads", "cpu", "ports", "role",
                 "rule", "reason", "children", "depth", "access_denied", "conn_count")

    def __init__(self, pid, ppid, name, exe="", cmdline="", argv=None, cwd="", create_time=None,
                 username="", status="", rss=0, threads=0, cpu=0.0, access_denied=False):
        self.pid = pid
        self.ppid = ppid
        self.name = name or f"pid{pid}"
        self.exe = exe or ""
        self.cmdline = cmdline or ""
        # 原始参数列表（不含引号解析歧义）。重启后端时必须用它：
        # 把 cmdline 当成字符串重新分词会毁掉 “-c \"带空格的代码\"” 这类参数。
        self.argv = list(argv) if argv else []
        self.cwd = cwd or ""
        self.create_time = create_time
        self.username = username or ""
        self.status = status or ""
        self.rss = rss or 0
        self.threads = threads or 0
        self.cpu = cpu or 0.0
        self.ports = []            # [(proto, addr, port, state)]
        self.role = ROLE_CHILD
        self.rule = None           # 命中的 Agent 规则 id
        self.reason = ""           # 角色判定依据
        self.children = []         # [pid]
        self.depth = 0
        self.access_denied = access_denied
        self.conn_count = 0        # 已建立连接数

    @property
    def label(self):
        return self.name or f"pid{self.pid}"

    @property
    def listen_ports(self):
        return sorted({p for (_proto, _addr, p, st) in self.ports if st != "UDP"})

    @property
    def port_text(self):
        parts = []
        for proto, _addr, port, st in sorted(self.ports, key=lambda x: x[2]):
            parts.append(f"{port}/{proto.lower()}")
        return ", ".join(parts) if parts else ""

    def to_dict(self):
        return dict(pid=self.pid, ppid=self.ppid, name=self.name, exe=self.exe,
                    cmdline=self.cmdline, argv=self.argv, cwd=self.cwd,
                    create_time=fmt_time(self.create_time), username=self.username,
                    memory=human_size(self.rss), threads=self.threads,
                    ports=[f"{proto} {addr}:{port}" + ("" if st == "UDP" else f" ({st})")
                           for proto, addr, port, st in self.ports],
                    role=self.role, reason=self.reason, rule=self.rule)


class Snapshot:
    """一次采集的完整结果。"""

    def __init__(self):
        self.ts = time.time()
        self.procs = {}            # pid -> Proc
        self.roots = []            # agent 根进程 pid 列表
        self.agents = []           # AgentGroup 列表
        self.port_index = {}       # pid -> [ (proto, addr, port, state) ]
        self.listen_rows = []      # 端口总览行
        self.data_source = "psutil"
        self.collect_ms = 0
        self.error = ""

    @property
    def backend_count(self):
        return sum(1 for p in self.procs.values() if p.role == ROLE_BACKEND)

    @property
    def listen_count(self):
        return len({(r["proto"], r["port"]) for r in self.listen_rows})


class AgentGroup:
    """一个 Agent 主体及其下挂的后端。"""

    def __init__(self, proc: Proc, rule: Rule):
        self.proc = proc
        self.rule = rule
        self.backends = []         # [Proc]
        self.descendants = []      # [Proc] 全部后代

    @property
    def label(self):
        return self.rule.label

    @property
    def color(self):
        return self.rule.color

    @property
    def ports(self):
        out = []
        for b in self.backends:
            out.extend(b.ports)
        return out


# --------------------------------------------------------------------------- #
# Windows 原生进程枚举（一次性快照，避免逐进程开句柄）
# --------------------------------------------------------------------------- #

if IS_WINDOWS:
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong),
                    ("cntUsage", ctypes.c_ulong),
                    ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", ctypes.c_ulong),
                    ("cntThreads", ctypes.c_ulong),
                    ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_ulong),
                    ("szExeFile", ctypes.c_wchar * 260)]


def toolhelp_snapshot():
    """返回 {pid: (ppid, 进程名, 线程数)}。一次系统调用拿到全部进程，约 10ms。"""
    if not IS_WINDOWS:
        return {}
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    handle = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not handle or handle == INVALID:
        return {}
    out = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID),
                                             entry.szExeFile or "",
                                             int(entry.cntThreads))
            ok = k32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))
    return out


# --------------------------------------------------------------------------- #
# 采集器
# --------------------------------------------------------------------------- #

class Collector:
    # 这些属性便宜（几百个进程 <150ms），每轮全量采集
    CHEAP_ATTRS = ["pid", "ppid", "name", "cmdline", "create_time"]

    def __init__(self, rules):
        self.rules = rules
        self._prev_cpu = {}      # pid -> (ts, cpu_time)
        self._static_cache = {}  # pid -> (create_time, exe, cwd, username)
        self.last_source = "psutil"

    # ---------------- 对外入口 ----------------
    def collect(self) -> Snapshot:
        t0 = time.time()
        snap = Snapshot()
        if psutil is not None:
            try:
                self._collect_psutil(snap)
                snap.data_source = "psutil"
            except Exception:
                snap.error = traceback.format_exc()
                self._collect_cim(snap)
                snap.data_source = "PowerShell CIM（降级）"
        else:
            self._collect_cim(snap)
            snap.data_source = "PowerShell CIM（降级）"
        self._analyze(snap)
        try:
            self._enrich(snap)
        except Exception:
            pass
        snap.collect_ms = int((time.time() - t0) * 1000)
        return snap

    # ---------------- psutil 采集 ----------------
    def _collect_psutil(self, snap: Snapshot):
        """快速全量采集。

        在 Windows 上 memory_info / num_threads / status / cpu_times / create_time 这类
        需要逐进程开句柄的调用，首次访问要 5~15ms，全量取会拖到十几秒；
        因此这里只用一次 CreateToolhelp32Snapshot 拿 PID/父PID/名称/线程数，
        再用 psutil 批量取命令行，最后只为 Agent 和后端补齐详情（见 _enrich）。
        """
        if IS_WINDOWS:
            base = toolhelp_snapshot()
            if base:
                cmdlines = {}
                try:
                    for p in psutil.process_iter(["pid", "cmdline"]):
                        i = p.info
                        cmd = i.get("cmdline") or []
                        cmdlines[i["pid"]] = (" ".join(cmd), list(cmd))
                except Exception:
                    pass
                for pid, (ppid, name, threads) in base.items():
                    cmdline, argv = cmdlines.get(pid, ("", []))
                    snap.procs[pid] = Proc(pid=pid, ppid=ppid, name=name, cmdline=cmdline,
                                           argv=argv, threads=threads,
                                           access_denied=not cmdline)
            else:
                self._collect_psutil_iter(snap)
        else:
            self._collect_psutil_iter(snap)

        # 端口 / 连接
        try:
            conns = psutil.net_connections(kind="inet")
        except Exception:
            conns = []
        for c in conns:
            pid = c.pid
            if not pid:
                continue
            try:
                proto = "TCP" if int(c.type) == 1 else "UDP"
            except Exception:
                proto = "TCP"
            laddr = c.laddr
            addr = getattr(laddr, "ip", "") if laddr else ""
            port = getattr(laddr, "port", 0) if laddr else 0
            status = c.status or ""
            if not port:
                continue
            if proto == "TCP" and status != psutil.CONN_LISTEN:
                p = snap.procs.get(pid)
                if p:
                    p.conn_count += 1
                continue
            state = "UDP" if proto == "UDP" else "LISTEN"
            snap.port_index.setdefault(pid, []).append((proto, addr, port, state))

    # ---------------- 按需补齐昂贵信息 ----------------
    def _collect_psutil_iter(self, snap: Snapshot):
        """非 Windows / Toolhelp 不可用时的通用采集路径。"""
        for p in psutil.process_iter(self.CHEAP_ATTRS):
            try:
                i = p.info
            except Exception:
                continue
            pid = i.get("pid")
            if not pid:
                continue
            cmd = i.get("cmdline") or []
            cmdline = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd or "")
            snap.procs[pid] = Proc(pid=pid, ppid=i.get("ppid") or 0, name=i.get("name") or "",
                                   cmdline=cmdline,
                                   argv=list(cmd) if isinstance(cmd, (list, tuple)) else [],
                                   create_time=i.get("create_time"),
                                   access_denied=not cmdline)

    # ---------------- 按需补齐昂贵信息 ----------------
    def _enrich(self, snap: Snapshot):
        """只为 Agent 主体与后端进程补齐内存/线程/CPU/状态，以及静态的启动详情。"""
        now = time.time()
        cur_cpu = {}
        for p in snap.procs.values():
            if p.role in (ROLE_AGENT, ROLE_BACKEND):
                self.enrich_one(p, now, cur_cpu)
        self._prev_cpu = cur_cpu

    def enrich_one(self, proc: Proc, now=None, cur_cpu=None) -> Proc:
        """补齐单个进程的详情（界面里点选某个进程时按需调用）。

        没有 psutil 时也会退化到直接读 PEB，保证"工作目录 / 原始参数"这两项拿得到——
        重启后端完全依赖它们，缺了会用错误的目录启动。
        """
        if not proc.pid:
            return proc
        now = now or time.time()
        if psutil is None:
            cache = self._static_cache.get(proc.pid)
            if cache is None or cache[0] != proc.name:
                cwd, cmdline = read_peb_strings(proc.pid)
                cache = (proc.name, proc.exe, cwd or "", proc.username, proc.create_time)
                if cwd:                       # 只在读成功时写缓存，失败下轮再试
                    self._static_cache[proc.pid] = cache
            _name, _exe, cwd, _user, _ct = cache
            if cwd:
                proc.cwd = cwd
            if not proc.argv and cmdline:
                proc.argv = split_cmdline(cmdline)
            return proc
        try:
            ps = psutil.Process(proc.pid)
        except Exception:
            return proc

        cache = self._static_cache.get(proc.pid)
        if cache is None or cache[0] != proc.name:
            exe = _safe_call(ps.exe)
            cwd = _safe_call(ps.cwd)
            username = _safe_call(ps.username)
            try:
                create_time = ps.create_time()
            except Exception:
                create_time = None
            if cwd or exe:                    # 只在成功时缓存；瞬时失败下轮会重试
                self._static_cache[proc.pid] = (proc.name, exe, cwd, username, create_time)
            else:
                cache = None
        else:
            _name, exe, cwd, username, create_time = cache
        if cache is not None:
            _name, exe, cwd, username, create_time = cache
        else:
            exe, cwd, username, create_time = "", "", "", None
        if exe or proc.exe:
            proc.exe = exe or proc.exe
        if cwd or proc.cwd:
            proc.cwd = cwd or proc.cwd
        if username or proc.username:
            proc.username = username or proc.username
        if create_time and not proc.create_time:
            proc.create_time = create_time

        try:
            proc.rss = ps.memory_info().rss
        except Exception:
            pass
        try:
            proc.threads = ps.num_threads()
        except Exception:
            pass
        try:
            proc.status = ps.status()
        except Exception:
            pass
        try:
            ct = ps.cpu_times()
            total = ct.user + ct.system
            if cur_cpu is not None:
                cur_cpu[proc.pid] = (now, total)
            prev = self._prev_cpu.get(proc.pid)
            if prev and (now - prev[0]) > 0.05:
                proc.cpu = max(0.0, (total - prev[1]) / (now - prev[0]) * 100.0)
        except Exception:
            pass
        return proc

    # ---------------- PowerShell CIM 降级采集 ----------------
    def _collect_cim(self, snap: Snapshot):
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$procs = Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,CreationDate,WorkingSetSize;"
            "$ports = Get-NetTCPConnection -State Listen | "
            "Select-Object LocalAddress,LocalPort,OwningProcess;"
            "ConvertTo-Json -Compress -Depth 3 @{procs=$procs;ports=$ports}"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=60,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(out.stdout or "{}")
        except Exception:
            snap.error = "PowerShell CIM 采集失败"
            return
        procs = data.get("procs") or []
        if isinstance(procs, dict):
            procs = [procs]
        for i in procs:
            pid = int(i.get("ProcessId") or 0)
            if not pid:
                continue
            ct = i.get("CreationDate")
            cts = None
            if ct:
                m = re.search(r"/Date\((\d+)", str(ct))
                if m:
                    cts = int(m.group(1)) / 1000.0
            snap.procs[pid] = Proc(pid=pid, ppid=int(i.get("ParentProcessId") or 0),
                                   name=i.get("Name") or "", exe=i.get("ExecutablePath") or "",
                                   cmdline=i.get("CommandLine") or "",
                                   argv=split_cmdline(i.get("CommandLine") or ""),
                                   create_time=cts,
                                   rss=int(i.get("WorkingSetSize") or 0))
        ports = data.get("ports") or []
        if isinstance(ports, dict):
            ports = [ports]
        for p in ports:
            pid = int(p.get("OwningProcess") or 0)
            if not pid:
                continue
            snap.port_index.setdefault(pid, []).append(
                ("TCP", p.get("LocalAddress") or "", int(p.get("LocalPort") or 0), "LISTEN"))

    # ---------------- 分析：角色、分组、端口归属 ----------------
    def _analyze(self, snap: Snapshot):
        # 1. 父子关系
        for pid, p in snap.procs.items():
            if p.ppid and p.ppid in snap.procs and p.ppid != pid:
                snap.procs[p.ppid].children.append(pid)
        for p in snap.procs.values():
            p.children.sort()

        # 2. 端口挂到进程
        for pid, plist in snap.port_index.items():
            p = snap.procs.get(pid)
            if p:
                p.ports = plist

        # 3. 识别 Agent 根进程
        #    两遍制：先找出所有命中规则的进程，再只把"祖先链上没有命中同一条规则"的那些当主体。
        #    - Electron 的渲染/GPU/崩溃进程与主进程命中同一条规则 → 不算独立 Agent
        #    - 在别的 Agent 里面跑的另一个 Agent（命中另一条规则）→ 仍然算独立 Agent
        candidates = {}
        for pid, p in snap.procs.items():
            if SYSTEM_PROC_RE.match(p.name):
                continue
            for rule in self.rules:
                if rule.match(p.name, p.cmdline, p.exe):
                    candidates[pid] = rule
                    break
        for pid, rule in candidates.items():
            if any(candidates.get(a) is not None and candidates[a].id == rule.id
                   for a in self._ancestor_pids(snap, pid)):
                continue
            p = snap.procs[pid]
            p.role = ROLE_AGENT
            p.rule = rule.id
            p.reason = f"命中识别规则：{rule.label}"

        # 4. Agent 后代：判定角色
        roots = [p for p in snap.procs.values() if p.role == ROLE_AGENT]
        roots.sort(key=lambda x: x.pid)
        snap.roots = [p.pid for p in roots]
        agent_by_pid = {p.pid: p for p in roots}

        for root in roots:
            rule = next((r for r in self.rules if r.id == root.rule), None)
            group = AgentGroup(root, rule)
            self._walk(snap, root, rule, group, depth=1)
            group.backends.sort(key=lambda x: (not x.listen_ports, x.pid))
            snap.agents.append(group)

        snap.agents.sort(key=lambda g: (g.rule.category != "agent", g.label, g.proc.pid))

        # 5. 端口总览（所有监听端口）
        for pid, plist in snap.port_index.items():
            p = snap.procs.get(pid)
            for proto, addr, port, state in plist:
                owner = self._owner_group(snap, p)
                snap.listen_rows.append(dict(
                    port=port, proto=proto, addr=addr or "*", pid=pid,
                    name=p.name if p else "?",
                    owner=owner.label if owner else ("系统/独立进程" if p else "已退出"),
                    owner_rule=owner.rule.id if owner else "",
                    role=p.role if p else ROLE_CHILD,
                    system=bool(p and SYSTEM_PROC_RE.match(p.name)),
                    cwd=p.cwd if p else "",
                    cmdline=p.cmdline if p else "",
                ))
        snap.listen_rows.sort(key=lambda r: (r["port"], r["proto"]))

    def _ancestor_pids(self, snap: Snapshot, pid: int):
        """返回某个进程的全部祖先 pid（带环路保护）。"""
        out = []
        seen = set()
        cur = snap.procs.get(pid)
        hops = 0
        while cur and hops < 32:
            cur = snap.procs.get(cur.ppid)
            hops += 1
            if not cur or cur.pid in seen:
                break
            seen.add(cur.pid)
            out.append(cur.pid)
        return out

    def _has_agent_ancestor(self, snap: Snapshot, pid: int, rule_id: str) -> bool:
        for apid in self._ancestor_pids(snap, pid):
            a = snap.procs.get(apid)
            if a and a.role == ROLE_AGENT and a.rule == rule_id:
                return True
        return False

    def _walk(self, snap: Snapshot, parent: Proc, rule: Rule, group: AgentGroup, depth: int):
        for cid in parent.children:
            child = snap.procs.get(cid)
            if child is None:
                continue
            # 另一个 Agent 主体：它有自己的分组，不并入当前 Agent，也不继续往下走
            if child.role == ROLE_AGENT:
                continue
            child.depth = depth
            role, reason = self._classify(child, rule)
            child.role = role
            child.reason = reason
            if role == ROLE_BACKEND:
                group.backends.append(child)
            group.descendants.append(child)
            child.rule = child.rule or (rule.id if role in (ROLE_BACKEND, ROLE_CORE) else None)
            self._walk(snap, child, rule, group, depth + 1)

    def _classify(self, p: Proc, rule: Rule):
        """判断一个 Agent 后代的角色，返回 (role, 依据)。"""
        if SYSTEM_PROC_RE.match(p.name):
            return ROLE_CORE, "系统组件"
        ports = p.listen_ports
        hay = f"{p.name} {p.cmdline}"

        if ports:
            kind = "MCP 服务" if MCP_HINT_RE.search(hay) else (
                "数据库/中间件" if DB_HINT_RE.search(hay) else "HTTP/网络服务")
            return ROLE_BACKEND, f"{kind}，监听端口 {', '.join(str(x) for x in ports)}"
        if MCP_HINT_RE.search(hay):
            return ROLE_BACKEND, "MCP 服务（通过 stdio 通信，无监听端口）"
        if HELPER_HINT_RE.search(p.cmdline):
            return ROLE_CORE, "Agent 自身辅助进程（Electron 渲染/GPU/崩溃处理）"
        if rule.match(p.name, p.cmdline, p.exe):
            return ROLE_CORE, "Agent 自身工作进程"
        if BACKEND_NAME_RE.match(p.name) and HTTP_HINT_RE.search(p.cmdline):
            return ROLE_BACKEND, "疑似服务进程（名称 + 启动参数特征）"
        if DB_HINT_RE.search(hay):
            return ROLE_BACKEND, "疑似数据库/中间件"
        if BACKEND_NAME_RE.match(p.name):
            return ROLE_CHILD, "运行时的普通子进程（当前未监听端口）"
        return ROLE_CHILD, "普通子进程"

    def _owner_group(self, snap: Snapshot, p: Proc):
        """向上追溯，找到拥有该进程的 Agent 分组。"""
        if p is None:
            return None
        seen = set()
        cur = p
        hops = 0
        while cur and hops < 32:
            if cur.role == ROLE_AGENT:
                for g in snap.agents:
                    if g.proc.pid == cur.pid:
                        return g
                return None
            if cur.pid in seen:
                return None
            seen.add(cur.pid)
            cur = snap.procs.get(cur.ppid)
            hops += 1
        return None


# --------------------------------------------------------------------------- #
# 事件跟踪（后端启动 / 消失 / 随 Agent 一起退出）
# --------------------------------------------------------------------------- #

class EventTracker:
    def __init__(self):
        self.prev = {}          # pid -> (name, role, rule, port_text, cmdline, cwd)
        self.prev_agent_children = {}   # agent pid -> set(descendant pids)
        self.prev_agent_labels = {}     # agent pid -> 规则显示名
        self.prev_ports = set()
        self.events = []
        self.baseline_done = False

    def _push(self, kind, level, text, pid=None, agent=""):
        self.events.append(dict(ts=time.time(), kind=kind, level=level, text=text,
                                pid=pid, agent=agent))
        if len(self.events) > 4000:
            del self.events[:1000]

    def update(self, snap: Snapshot):
        cur = {}
        for pid, p in snap.procs.items():
            cur[pid] = (p.name, p.role, p.rule or "", p.port_text, p.cmdline, p.cwd)
        cur_agent_children = {}
        cur_agent_labels = {}
        for g in snap.agents:
            cur_agent_children[g.proc.pid] = {d.pid for d in g.descendants}
            cur_agent_labels[g.proc.pid] = g.rule.label

        if not self.baseline_done:
            self.baseline_done = True
            self.prev = cur
            self.prev_agent_children = cur_agent_children
            self.prev_agent_labels = cur_agent_labels
            self._push("baseline", "info", f"开始监控：发现 {len(snap.agents)} 个 Agent 主体、"
                                           f"{snap.backend_count} 个后端、{snap.listen_count} 个监听端口")
            return

        started = [pid for pid in cur if pid not in self.prev]
        ended = [pid for pid in self.prev if pid not in cur]

        # Agent 退出 + 其子进程同时消失 => 连带终止
        gone_agents = [pid for pid in ended if self.prev[pid][1] == ROLE_AGENT]
        for apid in gone_agents:
            name, _role, rule, _pt, _cmd, _cwd = self.prev[apid]
            label = self.prev_agent_labels.get(apid) or rule or name
            kids = self.prev_agent_children.get(apid, set())
            gone_kids = [k for k in kids if k in ended]
            behind = [k for k in gone_kids
                      if self.prev.get(k) and self.prev[k][1] == ROLE_BACKEND]
            if behind:
                detail = "、".join(
                    f"{self.prev[k][0]}(PID {k}{'，端口 ' + self.prev[k][3] if self.prev[k][3] else ''})"
                    for k in behind[:8])
                self._push("agent-exit", "warn",
                           f"Agent「{label}」({name}, PID {apid}) 已退出 → 连带终止 {len(behind)} 个后端：{detail}",
                           pid=apid)
            else:
                self._push("agent-exit", "info", f"Agent「{label}」({name}, PID {apid}) 已退出", pid=apid)

        # 后端退出（且其所属 agent 仍存活）
        for pid in ended:
            if pid in gone_agents:
                continue
            name, role, rule, ptext, _cmd, _cwd = self.prev[pid]
            if role == ROLE_BACKEND:
                self._push("backend-exit", "warn",
                           f"后端退出：{name}(PID {pid})" + (f"，释放端口 {ptext}" if ptext else ""),
                           pid=pid, agent=rule)
            elif role == ROLE_AGENT:
                self._push("agent-exit", "info", f"Agent「{name}」(PID {pid}) 已退出", pid=pid)

        # 新增后端
        for pid in started:
            name, role, rule, ptext, cmd, cwd = cur[pid]
            if role == ROLE_BACKEND:
                extra = f"，监听端口 {ptext}" if ptext else ""
                extra += f"，工作目录 {cwd}" if cwd else ""
                self._push("backend-start", "ok",
                           f"后端启动：{name}(PID {pid}){extra}｜命令：{short(cmd, 160)}",
                           pid=pid, agent=rule)
            elif role == ROLE_AGENT:
                self._push("agent-start", "ok", f"发现 Agent「{name}」(PID {pid})", pid=pid)

        # 端口新开/释放（只关心有归属的）
        for row in snap.listen_rows:
            key = (row["proto"], row["port"])
            if key not in self.prev_ports:
                if row["role"] == ROLE_BACKEND or row["owner_rule"]:
                    self._push("port-open", "ok",
                               f"端口监听：{row['proto']} {row['addr']}:{row['port']} "
                               f"← {row['name']}(PID {row['pid']})｜归属 {row['owner']}",
                               pid=row["pid"])
        self.prev_ports = {(r["proto"], r["port"]) for r in snap.listen_rows}

        self.prev = cur
        self.prev_agent_children = cur_agent_children
        self.prev_agent_labels = cur_agent_labels


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def build_markdown(snap: Snapshot, events=None) -> str:
    lines = []
    lines.append(f"# {APP_NAME} 快照报告")
    lines.append("")
    lines.append(f"- 生成时间：{fmt_time(snap.ts)}")
    lines.append(f"- 数据来源：{snap.data_source}（采集耗时 {snap.collect_ms} ms）")
    lines.append(f"- 进程总数：{len(snap.procs)}")
    lines.append(f"- Agent 主体：{len(snap.agents)}　后端服务：{snap.backend_count}　监听端口：{snap.listen_count}")
    lines.append("")

    lines.append("## 一、Agent 与其后端")
    lines.append("")
    if not snap.agents:
        lines.append("_未发现已知 Agent 进程。可在 config/agents.json 中添加识别规则。_")
        lines.append("")
    for g in snap.agents:
        p = g.proc
        lines.append(f"### {g.rule.label} — PID {p.pid}（{p.name}）")
        lines.append("")
        lines.append(f"- 命令行：`{p.cmdline or '-'}`")
        lines.append(f"- 工作目录：`{p.cwd or '-'}`")
        lines.append(f"- 启动时间：{fmt_time(p.create_time)}（已运行 {human_duration(time.time() - p.create_time if p.create_time else None)}）")
        lines.append(f"- 内存：{human_size(p.rss)}　线程：{p.threads}　用户：{p.username or '-'}")
        if not g.backends:
            lines.append("- 后端：_无（该 Agent 当前没有在自身进程树下启动后端）_")
        else:
            lines.append(f"- 后端（{len(g.backends)}）：")
            for b in g.backends:
                port = f"　端口 `{b.port_text}`" if b.ports else "　无监听端口"
                lines.append(f"  - **{b.name}**(PID {b.pid})　{b.reason}{port}")
                lines.append(f"    - 命令：`{short(b.cmdline, 300) or '-'}`")
                lines.append(f"    - 目录：`{b.cwd or '-'}`　启动于 {fmt_time(b.create_time)}")
        lines.append("")

    lines.append("## 二、端口总览")
    lines.append("")
    lines.append("| 端口 | 协议 | 监听地址 | PID | 进程 | 归属 | 角色 |")
    lines.append("|---:|---|---|---:|---|---|---|")
    for r in snap.listen_rows:
        lines.append(f"| {r['port']} | {r['proto']} | {r['addr']} | {r['pid']} | {r['name']} | "
                     f"{r['owner']} | {ROLE_LABEL.get(r['role'], r['role'])} |")
    lines.append("")

    if events:
        lines.append("## 三、事件时间线（本次运行期间）")
        lines.append("")
        for e in events[-200:]:
            lines.append(f"- `{fmt_clock(e['ts'])}` [{e['level']}] {e['text']}")
        lines.append("")
    return "\n".join(lines)


def build_json(snap: Snapshot, events=None):
    return json.dumps({
        "app": APP_NAME, "version": VERSION,
        "generated_at": fmt_time(snap.ts),
        "source": snap.data_source,
        "stats": dict(processes=len(snap.procs), agents=len(snap.agents),
                      backends=snap.backend_count, listening_ports=snap.listen_count),
        "agents": [dict(label=g.rule.label, id=g.rule.id, pid=g.proc.pid,
                        process=g.proc.to_dict(),
                        backends=[b.to_dict() for b in g.backends],
                        descendants=[d.to_dict() for d in g.descendants]) for g in snap.agents],
        "listening_ports": snap.listen_rows,
        "events": events or [],
    }, ensure_ascii=False, indent=2)


def proc_to_node(snap: Snapshot, p: Proc, depth: int = 0, include_children: bool = True) -> dict:
    """把一个进程（含其子树）转成给网页前端用的嵌套字典。"""
    d = dict(
        pid=p.pid, ppid=p.ppid, name=p.name, role=p.role, reason=p.reason,
        cmdline=p.cmdline, argv=p.argv, cwd=p.cwd, exe=p.exe, username=p.username, status=p.status,
        rss=p.rss, rss_text=human_size(p.rss), threads=p.threads, cpu=round(p.cpu, 1),
        create_time=p.create_time, created=fmt_time(p.create_time),
        uptime=human_duration(time.time() - p.create_time) if p.create_time else "-",
        port_text=p.port_text, conn_count=p.conn_count, rule=p.rule,
        ports=[dict(proto=pr, addr=a, port=pt, state=st)
               for pr, a, pt, st in sorted(p.ports, key=lambda x: x[2])],
        depth=depth, child_count=len(p.children),
    )
    if include_children:
        kids = []
        for c in p.children:
            child = snap.procs.get(c)
            if child is None:
                continue
            if child.role == ROLE_AGENT:
                # 嵌套在里面的另一个 Agent：只显示它本身，它的后端属于它自己的分组
                kids.append(proc_to_node(snap, child, depth + 1, include_children=False))
            else:
                kids.append(proc_to_node(snap, child, depth + 1))
        d["children"] = kids
    return d


def build_ui_payload(snap: Snapshot, events=None, rules=None) -> dict:
    """网页版 /api/snapshot 返回的完整数据结构。"""
    agents = []
    for g in snap.agents:
        root = g.proc
        ports = sorted({(pr, a, pt) for pr, a, pt, _s in g.ports}
                       | {(pr, a, pt) for pr, a, pt, _s in root.ports})
        agents.append(dict(
            id=g.rule.id, label=g.rule.label, category=g.rule.category, color=g.rule.color,
            pid=root.pid, name=root.name, cmdline=root.cmdline, cwd=root.cwd, exe=root.exe,
            username=root.username, created=fmt_time(root.create_time),
            uptime=human_duration(time.time() - root.create_time) if root.create_time else "-",
            rss_text=human_size(root.rss), threads=root.threads, cpu=round(root.cpu, 1),
            backend_count=len(g.backends),
            ports=[dict(proto=pr, addr=a, port=pt) for pr, a, pt in ports],
            port_text=", ".join(f"{pt}/{pr.lower()}" for pr, _a, pt in ports),
            backends=[dict(pid=b.pid, name=b.name, reason=b.reason, cmdline=b.cmdline,
                           cwd=b.cwd, port_text=b.port_text, created=fmt_time(b.create_time),
                           rss_text=human_size(b.rss),
                           ports=[dict(proto=pr, addr=a, port=pt, state=st)
                                  for pr, a, pt, st in sorted(b.ports, key=lambda x: x[2])])
                      for b in g.backends],
            tree=proc_to_node(snap, root, 0),
        ))
    return dict(
        ts=snap.ts, generated_at=fmt_time(snap.ts), source=snap.data_source,
        collect_ms=snap.collect_ms, error=snap.error,
        stats=dict(processes=len(snap.procs), agents=len(agents),
                   backends=snap.backend_count, listen=snap.listen_count,
                   events=len(events or [])),
        agents=agents,
        ports=snap.listen_rows,
        events=events or [],
        rules=[dict(id=r.id, label=r.label, category=r.category, color=r.color)
               for r in (rules if rules is not None else [])],
    )


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #

