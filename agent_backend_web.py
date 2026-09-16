#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 后端监控器 — 网页版
=========================

在本机起一个只监听 127.0.0.1 的小服务，用浏览器看 Agent 与它启动的后端进程。

    python agent_backend_web.py                 # 默认 http://127.0.0.1:8737 并自动打开浏览器
    python agent_backend_web.py --port 9000 --no-browser
    python agent_backend_web.py --interval 1

采集内核与桌面版共用 abm_core.py，所以两边看到的数据完全一致。

接口
----
    GET  /                      页面
    GET  /api/snapshot          当前快照（含进程树、端口、事件、统计）
    GET  /api/report?format=md  下载 Markdown / JSON 报告
    POST /api/refresh           立即重新采集
    POST /api/kill              {"pid": 123, "tree": true}  结束进程（可选整棵树）
    POST /api/relaunch          {"pid": 123}   以独立方式重启该后端（脱离 Agent）

安全：只绑定回环地址；写操作需要页面内嵌的一次性 token（防其他本地网页乱调）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import abm_core as core
from abm_core import (APP_NAME, VERSION, Collector, EventTracker, REPORT_DIR,
                      build_markdown, build_json, build_ui_payload, fmt_time,
                      load_rules, short)

WEB_DIR = os.path.join(core.BASE_DIR, "web")
DEFAULT_PORT = 8737


# --------------------------------------------------------------------------- #
# 后台采样线程
# --------------------------------------------------------------------------- #

class Sampler(threading.Thread):
    """按固定间隔采集一次快照，所有浏览器共用同一份数据。"""

    def __init__(self, interval: float):
        super().__init__(daemon=True)
        self.interval = max(0.5, float(interval))
        self.rules = load_rules()
        self.collector = Collector(self.rules)
        self.tracker = EventTracker()
        self.lock = threading.Lock()
        self.payload = {"ts": time.time(), "stats": {}, "agents": [], "ports": [],
                        "events": [], "generated_at": "-", "source": "-", "collect_ms": 0}
        self._wake = threading.Event()
        self.error = ""
        self.suggest_python = None      # 懒探测：本机哪个解释器装了 psutil

    def collect_now(self):
        self._wake.set()

    def run(self):
        while True:
            t0 = time.time()
            try:
                if core.psutil is None:      # 用户装好 psutil 后自动恢复全量模式
                    if self.suggest_python is None:
                        self.suggest_python = core.find_psutil_python()
                    core.try_enable_psutil()
                snap = self.collector.collect()
                self.tracker.update(snap)
                payload = build_ui_payload(snap, self.tracker.events, self.rules)
                payload["interval"] = self.interval
                payload["app"] = APP_NAME
                payload["version"] = VERSION
                payload["python"] = sys.executable
                payload["suggest_python"] = self.suggest_python or ""
                with self.lock:
                    self.payload = payload
                self.error = ""
            except Exception:
                self.error = traceback.format_exc()
                with self.lock:
                    self.payload = dict(self.payload, error=self.error,
                                        generated_at=fmt_time(time.time()))
            spent = time.time() - t0
            self._wake.wait(max(0.2, self.interval - spent))
            self._wake.clear()

    def snapshot(self):
        with self.lock:
            return self.payload


SAMPLER: Sampler = None      # type: ignore
TOKEN = secrets.token_urlsafe(18)


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBackendMonitor/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------
    def log_message(self, fmt, *args):
        pass  # 静音访问日志

    def send_error(self, code, message=None, explain=None):
        """覆盖标准库实现：错误响应也必须是 JSON。

        标准库默认会返回一个 HTML 错误页（<!DOCTYPE HTML ... ），
        前端 fetch 拿到它只会报 “Unexpected token '<'”，非常难排查。
        """
        try:
            self._send(code, json.dumps(
                {"ok": False, "status": code,
                 "error": message or explain or f"HTTP {code}"},
                ensure_ascii=False).encode("utf-8"))
        except Exception:
            pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin") if self.headers else None
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        self._send(code, text.encode("utf-8"), ctype)

    def _host_ok(self) -> bool:
        """只接受来自本机地址的请求（防 DNS rebinding）。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _authorized(self) -> bool:
        return self.headers.get("X-ABM-Token") == TOKEN

    # ---------- GET ----------
    def do_GET(self):
        try:
            self._do_get()
        except Exception as e:
            self._fail(e)

    def _do_get(self):
        if not self._host_ok():
            return self._text("forbidden host", 403)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/snapshot":
            return self._json(SAMPLER.snapshot())
        if path == "/api/report":
            return self._report(parse_qs(urlparse(self.path).query))
        if path == "/api/health":
            return self._json({"ok": True, "app": APP_NAME, "version": VERSION,
                               "pid": os.getpid(), "error": SAMPLER.error})
        if path == "/api/reload-rules":
            SAMPLER.rules = load_rules()
            SAMPLER.collector.rules = SAMPLER.rules
            SAMPLER.collect_now()
            return self._json({"ok": True, "rules": len(SAMPLER.rules)})
        if path.startswith("/api/"):
            return self._json({"ok": False, "error": f"未知接口 {path}"}, 404)
        return self._text("not found", 404)

    def do_HEAD(self):
        """只回响应头，避免冒出 HTML 错误页。"""
        try:
            if not self._host_ok():
                return self._send(403, b"", "text/plain; charset=utf-8")
            self._send(200, b"", "application/json; charset=utf-8")
        except Exception:
            pass

    def do_OPTIONS(self):
        """允许跨来源调用（例如页面从 localhost 打开、接口在 127.0.0.1）。"""
        origin = self.headers.get("Origin") or "*"
        self._send(204, b"", "text/plain; charset=utf-8", {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-ABM-Token",
            "Access-Control-Max-Age": "600",
        })

    def _fail(self, exc):
        try:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc()[-1200:]}, 500)
        except Exception:
            pass

    def _serve_index(self):
        try:
            with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
                html = f.read()
        except OSError as e:
            return self._text(f"找不到页面文件 web/index.html：{e}", 500)
        # 占位符必须只出现在"值"的位置：早先直接替换 __ABM_TOKEN__ 会把 JS 变量名一起换掉，
        # 生成的脚本语法错误 → 前端拿不到 token → 所有写操作被 403 拒绝（表现为点了没反应）。
        html = html.replace("%%ABM_TOKEN%%", TOKEN).replace("%%ABM_VERSION%%", VERSION)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_static(self, name):
        name = os.path.basename(name)
        types = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
        full = os.path.join(WEB_DIR, name)
        if not os.path.isfile(full):
            return self._text("not found", 404)
        with open(full, "rb") as f:
            data = f.read()
        ctype = types.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        self._send(200, data, ctype)

    def _report(self, q):
        fmt = (q.get("format") or ["md"])[0].lower()
        snap = SAMPLER.snapshot()
        if fmt == "json":
            body = json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8")
            name = f"agent_backends_{datetime_stamp()}.json"
            ctype = "application/json; charset=utf-8"
        else:
            body = render_markdown(snap).encode("utf-8")
            name = f"agent_backends_{datetime_stamp()}.md"
            ctype = "text/markdown; charset=utf-8"
        self._send(200, body, ctype,
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    # ---------- POST ----------
    def do_POST(self):
        try:
            self._do_post()
        except Exception as e:
            self._fail(e)

    def _do_post(self):
        if not self._host_ok():
            return self._text("forbidden host", 403)
        if not self._authorized():
            return self._json({"ok": False, "error": "无效的访问令牌，请刷新页面后重试"}, 403)
        path = urlparse(self.path).path
        data = self._body_json()
        if path == "/api/refresh":
            SAMPLER.collect_now()
            return self._json({"ok": True})
        if path == "/api/kill":
            return self._json(kill_process(int(data.get("pid") or 0),
                                           bool(data.get("tree", True))))
        if path == "/api/relaunch":
            return self._json(relaunch_process(int(data.get("pid") or 0),
                                               bool(data.get("kill", True))))
        if path == "/api/reload-rules":
            SAMPLER.rules = load_rules()
            SAMPLER.collector.rules = SAMPLER.rules
            SAMPLER.collect_now()
            return self._json({"ok": True, "rules": len(SAMPLER.rules)})
        if path == "/api/enable-psutil":
            result = core.try_enable_psutil(install=bool(data.get("install", True)))
            if result.get("ok"):
                SAMPLER.collector = Collector(SAMPLER.rules)   # 重建采集器，丢掉降级状态
                SAMPLER.collect_now()
            return self._json(result)
        return self._json({"ok": False, "error": f"未知接口 {path}"}, 404)


def datetime_stamp():
    return time.strftime("%Y%m%d_%H%M%S")


def render_markdown(payload) -> str:
    """由 payload 还原出一份 Markdown 报告（不需要重新采集）。"""
    lines = [f"# {APP_NAME} 快照报告", "",
             f"- 生成时间：{payload.get('generated_at')}",
             f"- 数据来源：{payload.get('source')}（采集耗时 {payload.get('collect_ms')} ms）",
             f"- 进程总数：{payload.get('stats', {}).get('processes')}",
             f"- Agent 主体：{payload.get('stats', {}).get('agents')}　"
             f"后端服务：{payload.get('stats', {}).get('backends')}　"
             f"监听端口：{payload.get('stats', {}).get('listen')}", ""]
    lines += ["## 一、Agent 与其后端", ""]
    if not payload.get("agents"):
        lines += ["_未发现已知 Agent 进程。可在 config/agents.json 中添加识别规则。_", ""]
    for g in payload.get("agents", []):
        lines += [f"### {g['label']} — PID {g['pid']}（{g['name']}）", "",
                  f"- 命令行：`{g.get('cmdline') or '-'}`",
                  f"- 工作目录：`{g.get('cwd') or '-'}`",
                  f"- 启动时间：{g.get('created')}（已运行 {g.get('uptime')}）",
                  f"- 内存：{g.get('rss_text')}　线程：{g.get('threads')}",
                  f"- 端口：{g.get('port_text') or '无'}"]
        if not g.get("backends"):
            lines += ["- 后端：_无（该 Agent 没有在自身进程树下启动后端）_"]
        else:
            lines += [f"- 后端（{len(g['backends'])}）："]
            for b in g["backends"]:
                lines += [f"  - **{b['name']}**(PID {b['pid']})　{b['reason']}"
                          f"{'　端口 ' + b['port_text'] if b.get('port_text') else ''}",
                          f"    - 命令：`{short(b.get('cmdline'), 300)}`",
                          f"    - 目录：`{b.get('cwd') or '-'}`　启动于 {b.get('created')}"]
        lines += [""]
    lines += ["## 二、端口总览", "",
              "| 端口 | 协议 | 监听地址 | PID | 进程 | 归属 | 角色 |",
              "|---:|---|---|---:|---|---|---|"]
    for r in payload.get("ports", []):
        lines.append(f"| {r['port']} | {r['proto']} | {r['addr']} | {r['pid']} | {r['name']} | "
                     f"{r['owner']} | {core.ROLE_LABEL.get(r['role'], r['role'])} |")
    lines += ["", "## 三、事件时间线", ""]
    for e in payload.get("events", [])[-200:]:
        lines.append(f"- `{fmt_time(e['ts'])}` [{e['level']}] {e['text']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 进程操作
# --------------------------------------------------------------------------- #

def _find_proc(payload, pid):
    """在 Agent 进程树里按 pid 找节点。"""
    stack = [g["tree"] for g in payload.get("agents", []) if g.get("tree")]
    while stack:
        node = stack.pop()
        if node.get("pid") == pid:
            return node
        stack.extend(node.get("children") or [])
    return None


def _tree_pids(payload, pid):
    node = _find_proc(payload, pid)
    if not node:
        return [pid]
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n["pid"])
        stack.extend(n.get("children") or [])
    return out


def kill_process(pid, tree=True):
    if not pid:
        return {"ok": False, "error": "缺少 pid"}
    payload = SAMPLER.snapshot()
    pids = _tree_pids(payload, pid) if tree else [pid]
    killed = _kill_pids(pids)
    failed = [p for p in pids if p not in killed]
    SAMPLER.collect_now()
    return {"ok": True, "killed": killed, "failed": failed}


def relaunch_process(pid, kill_first=True):
    """重启一个后端：默认先结束旧进程、把端口让出来，再用原命令原端口拉起来。"""
    if not pid:
        return {"ok": False, "error": "缺少 pid"}
    payload = SAMPLER.snapshot()
    node = _find_proc(payload, pid)
    if not node:
        return {"ok": False, "error": "找不到该进程（可能已经退出）"}
    cmdline = (node.get("cmdline") or "").strip()
    if not cmdline:
        return {"ok": False, "error": "拿不到该进程的命令行，可能需要管理员权限"}
    # 优先用采集到的原始参数列表；退回到按命令行字符串分词
    args = node.get("argv") or split_cmdline(cmdline)
    cwd = node.get("cwd") or None
    if not cwd:
        # 非 Agent/后端进程默认不采集工作目录；这里现场补读（psutil 缺失时直接读 PEB）
        try:
            import psutil
            cwd = psutil.Process(pid).cwd()
        except Exception:
            cwd = None
    if not cwd:
        cwd = core.read_peb_strings(pid)[0]
    cwd_unknown = not cwd

    ports = [(p.get("proto") or "TCP", int(p.get("port") or 0))
             for p in (node.get("ports") or []) if p.get("port")]
    result = {"ok": True, "cmdline": cmdline, "cwd": cwd,
              "cwd_unknown": cwd_unknown,
              "ports": [p for _proto, p in ports], "killed": [], "still_busy": {}}

    # ① 先结束旧进程（含其子进程），把端口让出来
    if kill_first:
        victim = _tree_pids(payload, pid)
        result["killed"] = _kill_pids(victim)
        if ports:
            busy = core.wait_ports_free(ports, timeout=8.0, ignore_pids=[pid])
            # 端口还占着：如果是同一个 Agent 树里的其他进程，一并结束；否则如实报告
            for port, owner in list(busy.items()):
                other = _find_proc(payload, owner)
                if other and other.get("rule") == node.get("rule"):
                    result["killed"] += _kill_pids([owner])
                    busy.pop(port, None)
            if busy:
                result["still_busy"] = busy
                busy = core.wait_ports_free(ports, timeout=3.0, ignore_pids=[pid])
                result["still_busy"] = busy
            result["released"] = not busy
            if busy:
                result["warning"] = ("端口 " + "、".join(
                    f"{pt}（占用者 {('PID ' + str(owner)) if owner else '未知进程'}）"
                    for pt, owner in busy.items()) + " 仍被占用。")

    # ② 用原命令启动
    try:
        launched = core.spawn_detached(args, cwd, exclude=(pid,))
    except Exception as e:
        return {"ok": False, "error": f"启动失败：{e}", **{k: result[k] for k in ("killed",)}}
    result.update(launched)

    # ③ 确认端口真的重新被监听（这才是"用之前的端口起来了"的判据）
    if ports:
        listening = core.wait_ports_listening(ports, timeout=15.0)
        result["listening"] = listening
        result["bound_ports"] = sorted(listening)
        missing = [p for _proto, p in ports if p not in listening]
        result["missing_ports"] = missing
        if missing and launched.get("alive"):
            result["warning"] = (f"新进程 PID {launched['pid']} 已启动，但端口 "
                                 f"{'、'.join(map(str, missing))} 还没开始监听"
                                 f"（可能仍在启动中，或换到了别的端口）。")

    SAMPLER.collect_now()

    # ④ 复核新进程的工作目录：目录不对（比如落到 system32）是"起来了但没在服务"的常见原因
    try:
        new_cwd = core.read_peb_strings(launched["pid"])[0] if launched.get("pid") else None
    except Exception:
        new_cwd = None
    if new_cwd:
        result["new_cwd"] = new_cwd
        if cwd and os.path.normcase(os.path.normpath(new_cwd)) != os.path.normcase(os.path.normpath(cwd)):
            result["warning"] = (f"新进程的工作目录是 {new_cwd}，与原进程的 {cwd} 不一致，"
                                 f"服务可能没有指向原来的项目目录。")

    if not launched.get("alive"):
        tried = "、".join(a.get("how", "?") for a in launched.get("attempts", []))
        prev = result.get("warning")
        result["warning"] = ((prev + "　") if prev else "") + (
            f"已尝试 {tried}，但新进程都没能活下来。常见原因：该命令依赖 Agent "
            f"注入的环境变量，或当前环境（如 Agent 的作业对象）不允许新进程存活。")
    elif not result.get("warning") and cwd_unknown:
        result["warning"] = ("未能读到原进程的工作目录，新进程可能在默认目录（如 system32）下运行，"
                             "请注意服务是否指向了正确的项目目录。")
    return result


def _kill_pids(pids):
    """结束一批进程，返回真正结束掉的 pid 列表。"""
    done = []
    try:
        import psutil
    except Exception:
        psutil = None
    for target in reversed(list(pids)):
        try:
            if psutil is not None:
                psutil.Process(target).kill()
            else:
                subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"],
                               capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            done.append(target)
        except Exception:
            pass
    return done


def split_cmdline(cmdline):
    """命令行分词（与采集内核用同一套系统级解析）。"""
    return core.split_cmdline(cmdline)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

def pick_port(preferred: int) -> int:
    """找一个真正空闲的端口。

    注意：Windows 上给探测 socket 设 SO_REUSEADDR 会让 bind 在端口被占用时也“成功”，
    结果两个监控服务会绑到同一个端口互相抢答请求（页面时好时坏）。
    所以这里既不加 SO_REUSEADDR，绑上之后再真正连一下确认没人应答。
    """
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        if port_in_use(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        if port_in_use(port):
            continue
        return port
    raise SystemExit(f"从 {preferred} 起连续 20 个端口都被占用，请用 --port 指定其他端口")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex(("127.0.0.1", port)) == 0


class MonitorServer(ThreadingHTTPServer):
    """禁用地址复用：端口被占就直接报错，绝不和已有实例抢同一个端口。"""
    allow_reuse_address = False
    daemon_threads = True


def main() -> int:
    global SAMPLER
    ap = argparse.ArgumentParser(description=f"{APP_NAME} 网页版 v{VERSION}")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--interval", type=float, default=2.0, help="采样间隔秒数（默认 2）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = ap.parse_args()

    if not os.path.isdir(WEB_DIR):
        print(f"缺少前端文件目录：{WEB_DIR}", file=sys.stderr)
        return 2

    port = pick_port(args.port)
    SAMPLER = Sampler(args.interval)
    SAMPLER.start()

    try:
        httpd = MonitorServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"无法监听端口 {port}：{e}\n（端口可能刚被别的程序占用，换一个：--port {port + 1}）",
              file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} 网页版已启动：{url}")
    if port != args.port:
        print(f"  注意：{args.port} 端口已被占用，已自动改用 {port}")
    print(f"  采样间隔 {args.interval}s　规则文件 {core.RULES_FILE}　Ctrl+C 退出")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
