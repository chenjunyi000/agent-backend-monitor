#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 后端监控器 — 桌面版（tkinter 图形界面）
============================================

展示每个 AI Agent 在本机启动的后端进程：谁启动的、用什么命令行和工作目录、
监听了哪个端口、Agent 退出时会不会连带后端一起消失。

用法
----
    pythonw agent_backend_monitor.py              # 打开图形界面（或双击 启动监控.bat）
    python  agent_backend_monitor.py --report     # 命令行打印一份当前快照报告
    python  agent_backend_monitor.py --json out.json
    python  agent_backend_monitor.py --watch 3    # 每 3 秒刷新打印

采集内核在 abm_core.py，网页版见 agent_backend_web.py。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from abm_core import *  # noqa: F401,F403
from abm_core import (APP_NAME, VERSION, BASE_DIR, CONFIG_DIR, REPORT_DIR, RULES_FILE,
                      IS_WINDOWS, AgentGroup, Collector, EventTracker, Proc, Snapshot,
                      build_json, build_markdown, fmt_clock, fmt_time, human_duration,
                      human_size, load_rules, process_alive, quote_ps, read_peb_strings, short,
                      spawn_detached, wait_ports_free, wait_ports_listening)

# 界面字体
FONT = "Microsoft YaHei UI"
MONO = "Consolas"

# --------------------------------------------------------------------------- #
# 高 DPI 适配
# --------------------------------------------------------------------------- #

SCALE = 1.0


def px(n: float) -> int:
    """按屏幕缩放比例换算像素，避免在 125% / 200% 缩放下界面被位图拉伸而发虚。"""
    return max(1, int(round(n * SCALE)))


def enable_dpi_awareness() -> float:
    global SCALE
    if not IS_WINDOWS:
        return SCALE
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # per-monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        SCALE = ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        SCALE = 1.0
    SCALE = max(1.0, min(float(SCALE), 3.0))
    return SCALE


class MonitorApp(tk.Tk):
    def __init__(self, interval=2.0):
        enable_dpi_awareness()
        super().__init__()
        try:
            self.tk.call("tk", "scaling", 1.3333 * SCALE)
        except Exception:
            pass
        self.title(f"{APP_NAME}  v{VERSION}")
        self.geometry(f"{px(1360)}x{px(820)}")
        self.minsize(px(1080), px(640))
        self.configure(bg=C_BG)

        self.rules = load_rules()
        self.collector = Collector(self.rules)
        self.tracker = EventTracker()
        self.snap = Snapshot()
        self.q = queue.Queue()
        self.busy = False
        self.interval = interval
        self.auto = tk.BooleanVar(value=True)
        self.compact = tk.BooleanVar(value=True)       # 隐藏辅助进程
        self.only_agent = tk.BooleanVar(value=False)   # 只看 AI Agent（隐藏 IDE）
        self.hide_sys = tk.BooleanVar(value=True)      # 隐藏系统端口
        self.only_listen = tk.BooleanVar(value=False)  # 只看有监听端口的后端
        self.filter_text = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="正在初始化…")
        self.sel_pid = None
        self.tree_items = {}
        self.port_items = {}
        self._sash_inited = False

        self._build_style()
        self._build_ui()
        self.after(80, self._poll_queue)
        self.refresh(manual=True)
        self.after(int(self.interval * 1000), self._tick)

    # ------------------------- 样式 -------------------------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.option_add("*Font", "{Microsoft YaHei UI} 9")
        style.configure(".", background=C_BG, foreground=C_FG, fieldbackground=C_PANEL,
                        bordercolor=C_PANEL2, lightcolor=C_PANEL2, darkcolor=C_PANEL2)
        style.configure("TFrame", background=C_BG)
        style.configure("Panel.TFrame", background=C_PANEL)
        style.configure("TLabel", background=C_BG, foreground=C_FG)
        style.configure("Dim.TLabel", background=C_BG, foreground=C_DIM)
        style.configure("Head.TLabel", background=C_BG, foreground=C_FG,
                        font=("{Microsoft YaHei UI}", 11, "bold"))
        style.configure("TButton", background=C_PANEL2, foreground=C_FG, borderwidth=0,
                        focusthickness=0, padding=(10, 5))
        style.map("TButton", background=[("active", "#38404e"), ("pressed", "#465066")])
        style.configure("Accent.TButton", background="#2f6fb0", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#3a82cc")])
        style.map("Mini.TButton", background=[("active", "#38404e"), ("pressed", "#465066")])
        style.configure("Mini.TButton", background=C_PANEL2, foreground=C_FG, padding=(6, 3))
        style.configure("Danger.TButton", background="#4a2229", foreground="#ffb3bb", padding=(6, 3))
        style.map("Danger.TButton", background=[("active", "#63303a"), ("pressed", "#7a3a45")])
        style.configure("TCheckbutton", background=C_BG, foreground=C_DIM)
        style.map("TCheckbutton", background=[("active", C_BG)], foreground=[("active", C_FG)])
        style.configure("TEntry", fieldbackground=C_PANEL2, foreground=C_FG,
                        insertcolor=C_FG, bordercolor=C_PANEL2)
        style.configure("TNotebook", background=C_BG, borderwidth=0, tabmargins=(6, 4, 0, 0))
        style.configure("TNotebook.Tab", background=C_PANEL, foreground=C_DIM,
                        padding=(16, 7), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", C_PANEL2)],
                  foreground=[("selected", C_ACCENT)])
        style.configure("Treeview", background=C_PANEL, fieldbackground=C_PANEL,
                        foreground=C_FG, rowheight=px(26), borderwidth=0)
        style.map("Treeview", background=[("selected", "#2f6fb0")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=C_PANEL2, foreground=C_DIM,
                        relief="flat", padding=(6, 6))
        style.map("Treeview.Heading", background=[("active", "#39404d")])
        style.configure("TPanedwindow", background=C_BG)
        style.configure("Sash", sashthickness=6, gripcount=0)
        style.configure("TProgressbar", background=C_ACCENT, troughcolor=C_PANEL2,
                        bordercolor=C_PANEL2, lightcolor=C_ACCENT, darkcolor=C_ACCENT)
        style.configure("Vertical.TScrollbar", background=C_PANEL2, troughcolor=C_BG,
                        bordercolor=C_BG, arrowcolor=C_DIM)

    # ------------------------- 界面 -------------------------
    def _build_ui(self):
        bar = ttk.Frame(self, padding=(12, 10, 12, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="🛰", font=(FONT, 15)).pack(side="left", padx=(0, 8))
        ttk.Label(bar, text=APP_NAME, style="Head.TLabel").pack(side="left")
        ttk.Label(bar, text=f"v{VERSION}", style="Dim.TLabel").pack(side="left", padx=(6, 16))

        ttk.Button(bar, text="刷新 (F5)", style="Accent.TButton",
                   command=lambda: self.refresh(manual=True)).pack(side="left", padx=(0, 4))
        ttk.Checkbutton(bar, text="自动刷新", variable=self.auto).pack(side="left", padx=(10, 2))
        self.interval_var = tk.StringVar(value=str(self.interval))
        cb = ttk.Combobox(bar, width=4, state="readonly", textvariable=self.interval_var,
                          values=["1", "2", "3", "5", "10"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._set_interval())
        ttk.Label(bar, text="秒", style="Dim.TLabel").pack(side="left", padx=(3, 14))

        ttk.Label(bar, text="过滤", style="Dim.TLabel").pack(side="left", padx=(0, 5))
        ent = ttk.Entry(bar, textvariable=self.filter_text, width=26)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda _e: self.render())

        ttk.Button(bar, text="导出报告", style="Mini.TButton",
                   command=self.export_report).pack(side="right", padx=2)
        ttk.Button(bar, text="打开规则文件", style="Mini.TButton",
                   command=self.open_rules).pack(side="right", padx=2)
        ttk.Button(bar, text="重新加载规则", style="Mini.TButton",
                   command=self.reload_rules).pack(side="right", padx=2)

        # 概览卡片
        strip = ttk.Frame(self, padding=(12, 0, 12, 2))
        strip.pack(fill="x")
        self.stat_values = {}
        for key, title, color in (("processes", "进程总数", C_FG),
                                  ("agents", "Agent 主体", C_PURPLE),
                                  ("backends", "后端服务", C_GREEN),
                                  ("listen", "监听端口", C_CYAN),
                                  ("agent_ports", "归属 Agent 的端口", C_GREEN),
                                  ("events", "事件", C_ORANGE),
                                  ("collect", "采集耗时", C_DIM),
                                  ("updated", "上次刷新", C_DIM)):
            card = tk.Frame(strip, bg=C_PANEL, highlightthickness=1,
                            highlightbackground=C_LINE, highlightcolor=C_LINE)
            card.pack(side="left", padx=(0, 8), pady=4)
            tk.Label(card, text=title, bg=C_PANEL, fg=C_DIM2, font=(FONT, 8),
                     anchor="w").pack(fill="x", padx=11, pady=(5, 0))
            value = tk.Label(card, text="-", bg=C_PANEL, fg=color, font=(FONT, 12, "bold"),
                             anchor="w")
            value.pack(fill="x", padx=11, pady=(0, 5))
            self.stat_values[key] = value

        opts = ttk.Frame(self, padding=(12, 4, 12, 6))
        opts.pack(fill="x")
        ttk.Checkbutton(opts, text="精简模式（隐藏 Agent 辅助进程）", variable=self.compact,
                        command=self.render).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opts, text="只看 AI Agent（隐藏 IDE）", variable=self.only_agent,
                        command=self.render).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opts, text="只看有端口/服务的后端", variable=self.only_listen,
                        command=self.render).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opts, text="端口页隐藏系统端口", variable=self.hide_sys,
                        command=self.render).pack(side="left", padx=(0, 14))
        ttk.Label(opts, text="◆ Agent 主体　▲ 后端服务　· 辅助进程　○ 子进程",
                  style="Dim.TLabel").pack(side="right")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        self._build_tree_tab()
        self._build_port_tab()
        self._build_event_tab()
        self._build_help_tab()

        bottom = ttk.Frame(self, padding=(12, 4, 12, 9))
        bottom.pack(fill="x")
        tk.Label(bottom, text="●", bg=C_BG, fg=C_GREEN, font=(FONT, 8)).pack(side="left", padx=(0, 6))
        ttk.Label(bottom, textvariable=self.status_text, style="Dim.TLabel").pack(side="left")
        self.prog = ttk.Progressbar(bottom, mode="indeterminate", length=px(110))

    def _build_tree_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  Agent 与后端  ")
        pane = ttk.Panedwindow(tab, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=2, pady=6)
        self.pane = pane

        left = ttk.Frame(pane)
        cols = ("role", "pid", "ports", "mem", "started", "note")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="进程 / Agent")
        self.tree.column("#0", width=px(300), minwidth=px(180), stretch=True)
        heads = [("role", "角色", 92, "w"), ("pid", "PID", 64, "e"),
                 ("ports", "监听端口", 130, "w"), ("mem", "内存", 72, "e"),
                 ("started", "启动时间", 140, "w"), ("note", "判定依据", 320, "w")]
        for key, text, width, anchor in heads:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=px(width), anchor=anchor,
                             stretch=(key in ("note", "ports")))
        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.tree.tag_configure("backend", foreground=C_GREEN)
        self.tree.tag_configure("backend_port", foreground=C_GREEN, font=(FONT, 9, "bold"))
        self.tree.tag_configure("core", foreground=C_DIM2)
        self.tree.tag_configure("child", foreground="#b9c0cc")
        self.tree.tag_configure("child_opus", foreground="#8b93a3")
        self.tree.tag_configure("stripe", background="#151922")
        self.tree.tag_configure("agent", foreground=C_ACCENT, font=(FONT, 9, "bold"))
        self.tree.tag_configure("agent_ide", foreground=C_PURPLE, font=(FONT, 9, "bold"))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._build_tree_menu()

        right = ttk.Frame(pane)
        head = ttk.Frame(right)
        head.pack(fill="x", pady=(2, 6))
        ttk.Label(head, text="进程详情", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="右键进程树可用完整操作", style="Dim.TLabel").pack(side="left", padx=8)

        hero = tk.Frame(right, bg=C_BG)
        hero.pack(fill="x", pady=(0, 6))
        self.hero_icon = tk.Label(hero, text="○", bg=C_BG, fg=C_ACCENT, font=(FONT, 13))
        self.hero_icon.pack(side="left", padx=(0, 7))
        self.hero_name = tk.Label(hero, text="（未选择进程）", bg=C_BG, fg=C_FG,
                                  font=(FONT, 12, "bold"), anchor="w")
        self.hero_name.pack(side="left")
        self.hero_chip = tk.Label(hero, text="", bg=C_PANEL2, fg=C_DIM, font=(FONT, 8),
                                  padx=9, pady=2)
        self.hero_chip.pack(side="left", padx=9)

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(0, 6))
        for i, (text, cmd, st) in enumerate((("复制详情", self.copy_details, "Mini.TButton"),
                                             ("复制命令", self.copy_launch_cmd, "Mini.TButton"),
                                             ("打开目录", self.open_exe_dir, "Mini.TButton"),
                                             ("重启并占回端口", self.relaunch_detached, "Mini.TButton"),
                                             ("结束进程树", self.kill_tree, "Danger.TButton"))):
            b = ttk.Button(btns, text=text, style=st, command=cmd)
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)

        self.detail = tk.Text(right, wrap="word", bg=C_PANEL, fg=C_FG, relief="flat",
                              padx=12, pady=10, insertbackground=C_FG,
                              font=("{Microsoft YaHei UI}", 9), height=10)
        dsb = ttk.Scrollbar(right, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set, state="disabled")
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")
        self.detail.tag_configure("h", foreground=C_ACCENT, font=("{Microsoft YaHei UI}", 10, "bold"))
        self.detail.tag_configure("k", foreground=C_DIM)
        self.detail.tag_configure("v", foreground=C_FG)
        self.detail.tag_configure("mono", foreground=C_CYAN, font=("{Consolas}", 9))
        self.detail.tag_configure("warn", foreground=C_ORANGE)
        self.detail.tag_configure("good", foreground=C_GREEN)
        self.detail.tag_configure("bad", foreground=C_RED)

        pane.add(left, weight=3)
        pane.add(right, weight=4)

    def _build_tree_menu(self):
        """进程树右键菜单：完整操作入口。"""
        m = tk.Menu(self, tearoff=0, bg=C_PANEL2, fg=C_FG, activebackground="#2f6fb0",
                    activeforeground="#ffffff", bd=0)
        m.add_command(label="复制进程详情", command=self.copy_details)
        m.add_command(label="复制启动命令（PowerShell）", command=self.copy_launch_cmd)
        m.add_separator()
        m.add_command(label="打开可执行文件所在目录", command=self.open_exe_dir)
        m.add_command(label="重启并占回原端口（先释放端口）", command=self.relaunch_detached)
        m.add_separator()
        m.add_command(label="结束进程树", command=self.kill_tree)
        self.tree_menu = m
        self.tree.bind("<Button-3>", self._on_tree_right_click)

    def _on_tree_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self.sel_pid = self.tree_items.get(row, self.sel_pid)
            self._render_detail()
        try:
            self.tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.tree_menu.grab_release()

    def _build_port_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  端口总览  ")
        cols = ("port", "proto", "addr", "pid", "name", "owner", "role", "cwd", "cmd")
        self.port_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        heads = [("port", "端口", 70, "e"), ("proto", "协议", 56, "center"),
                 ("addr", "监听地址", 120, "w"), ("pid", "PID", 64, "e"),
                 ("name", "进程", 130, "w"), ("owner", "归属 Agent", 180, "w"),
                 ("role", "角色", 92, "w"), ("cwd", "工作目录", 260, "w"),
                 ("cmd", "命令行", 420, "w")]
        for key, text, width, anchor in heads:
            self.port_tree.heading(key, text=text)
            self.port_tree.column(key, width=px(width), anchor=anchor,
                                  stretch=(key in ("cmd", "cwd", "owner")))
        vs = ttk.Scrollbar(tab, orient="vertical", command=self.port_tree.yview)
        self.port_tree.configure(yscrollcommand=vs.set)
        self.port_tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=6)
        vs.pack(side="right", fill="y", pady=6)
        self.port_tree.tag_configure("agentport", foreground=C_GREEN)
        self.port_tree.tag_configure("backend", foreground=C_GREEN)
        self.port_tree.tag_configure("sys", foreground=C_DIM)
        self.port_tree.bind("<Double-1>", self._on_port_double)

    def _build_event_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  事件时间线  ")
        head = ttk.Frame(tab)
        head.pack(fill="x", pady=(6, 0))
        ttk.Label(head, text="后端何时启动、何时消失，以及「Agent 退出 → 连带后端终止」的关联事件",
                  style="Dim.TLabel").pack(side="left")
        ttk.Button(head, text="清空", command=self._clear_events).pack(side="right", padx=3)
        ttk.Button(head, text="导出事件", command=self.export_events).pack(side="right", padx=3)
        cols = ("time", "level", "text")
        self.ev_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        self.ev_tree.heading("time", text="时间")
        self.ev_tree.column("time", width=px(90), anchor="center", stretch=False)
        self.ev_tree.heading("level", text="级别")
        self.ev_tree.column("level", width=px(70), anchor="center", stretch=False)
        self.ev_tree.heading("text", text="事件")
        self.ev_tree.column("text", width=px(1000), anchor="w")
        vs = ttk.Scrollbar(tab, orient="vertical", command=self.ev_tree.yview)
        self.ev_tree.configure(yscrollcommand=vs.set)
        self.ev_tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=6)
        vs.pack(side="right", fill="y", pady=6)
        self.ev_tree.tag_configure("ok", foreground=C_GREEN)
        self.ev_tree.tag_configure("warn", foreground=C_ORANGE)
        self.ev_tree.tag_configure("info", foreground=C_DIM)
        self.ev_tree.tag_configure("err", foreground=C_RED)

    def _build_help_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  说明  ")
        txt = tk.Text(tab, wrap="word", bg=C_PANEL, fg=C_FG, relief="flat",
                      padx=16, pady=14, font=("{Microsoft YaHei UI}", 9))
        txt.pack(fill="both", expand=True, padx=2, pady=6)
        txt.tag_configure("h", foreground=C_ACCENT, font=("{Microsoft YaHei UI}", 11, "bold"))
        txt.tag_configure("b", foreground=C_FG, font=("{Microsoft YaHei UI}", 9, "bold"))
        txt.tag_configure("m", foreground=C_CYAN, font=("{Consolas}", 9))
        txt.tag_configure("d", foreground=C_DIM)

        def w(text, tag=None):
            txt.insert("end", text, tag or ())

        w("这个工具解决什么问题\n", "h")
        w("很多 Agent 工具会把后端（本地 HTTP 服务、MCP server、语言服务器等）作为自己的子进程启动。\n"
          "父进程一退出，这些后端往往被一并收走，端口随之失效——排查时很难说清「是谁启动的、跑在哪、为什么没了」。\n"
          "本工具把 Agent 的进程树、每个后端的启动命令/工作目录/监听端口完整摊开，并持续记录它们的生灭事件。\n\n")

        w("四个页签\n", "h")
        w("① Agent 与后端　", "b")
        w("按真实父子关系展开的进程树。◆ 是 Agent 主体，▲ 是后端服务，· 是 Agent 自身的辅助进程（如 Electron 渲染进程）。\n"
          "   点任意一行，右侧显示完整命令行、工作目录、启动时间、父进程链、监听端口、内存/线程数。\n")
        w("② 端口总览　", "b")
        w("机器上所有监听端口及其占用进程，并标注它归属于哪个 Agent。双击一行可跳回进程树。\n")
        w("③ 事件时间线　", "b")
        w("持续记录后端启动/退出、端口监听/释放。当一个 Agent 退出并连带其子进程同时消失时，会生成一条\n"
          "   「Agent 退出 → 连带终止 N 个后端」事件，这类记录就是「关闭 Agent 后端也关掉」的直接证据。\n")
        w("④ 说明\n\n")

        w("识别规则可以自己改\n", "h")
        w("config/agents.json 里定义已知 Agent：", "d")
        w("proc", "m")
        w(" 是进程名正则，", "d")
        w("cmd", "m")
        w(" 是命令行/可执行文件路径正则，两者都匹配才算命中。\n"
          "点工具栏「打开规则文件」编辑后，点「重新加载规则」即可生效，无需重启。\n"
          "如果某个新工具没被识别出来，把它的命令行特征加进去就行。\n\n")

        w("常用操作\n", "h")
        w("· 结束进程树：", "b"); w("杀掉选中进程及其所有后代（谨慎使用）。\n")
        w("· 重启并占回端口：", "b")
        w("先结束旧进程、等它占用的端口真正释放，再用原命令行、原工作目录重新启动并占回同一个端口；\n"
          "   新进程由 WMI / CreateProcess / 任务计划程序三级方式脱离 Agent 的作业对象与控制台。\n")
        w("· 复制启动命令：", "b"); w("生成可直接粘贴到 PowerShell 的启动命令。\n")
        w("· 导出报告：", "b"); w("导出 Markdown / JSON 快照到 reports/ 目录，便于留证或发给别人。\n\n")

        w("权限说明\n", "h")
        w("普通权限下可以读取同用户进程；对以管理员身份运行的进程，命令行/工作目录可能显示为空。\n"
          "以管理员身份运行本工具可获得最完整的信息。\n\n")
        w("还有一个网页版\n", "h")
        w("同一个采集内核也提供了网页界面（只监听 127.0.0.1，不会对外暴露）：\n", "d")
        w("python agent_backend_web.py", "m")
        w("　或双击 ", "d")
        w("启动网页版.bat\n", "m")
        w("默认地址 http://127.0.0.1:8737 ，功能与桌面版一致，并支持一键下载报告。\n\n")

        w("命令行用法\n", "h")
        w("python agent_backend_monitor.py --report      打印 Markdown 报告\n"
          "python agent_backend_monitor.py --json out.json\n"
          "python agent_backend_monitor.py --watch 3     每 3 秒刷新打印\n", "m")
        txt.configure(state="disabled")

    # ------------------------- 刷新流程 -------------------------
    def _set_interval(self):
        try:
            self.interval = float(self.interval_var.get())
        except Exception:
            self.interval = 2.0

    def _tick(self):
        if self.auto.get():
            self.refresh()
        self.after(max(500, int(self.interval * 1000)), self._tick)

    def refresh(self, manual=False):
        if self.busy:
            return
        self.busy = True
        if manual:
            self.prog.pack(side="right")
            self.prog.start(12)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            snap = self.collector.collect()
            self.q.put(("snap", snap))
        except Exception:
            self.q.put(("err", traceback.format_exc()))

    def _poll_queue(self):
        try:
            while True:
                try:
                    kind, payload = self.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "snap":
                    self.snap = payload
                    self.tracker.update(payload)
                    try:
                        self.render()
                        self._render_events()
                    except Exception:
                        # 界面渲染出问题也不能打断刷新循环
                        self.status_text.set("渲染出错：" + short(traceback.format_exc(), 200))
                    self._update_status()
                else:
                    self.status_text.set("采集出错：" + short(payload, 160))
        finally:
            self.busy = False
            self.prog.stop()
            self.prog.pack_forget()
            self.after(120, self._poll_queue)

    def _update_status(self):
        s = self.snap
        events = len(self.tracker.events)
        agent_ports = sum(1 for r in s.listen_rows if r.get("owner_rule"))
        values = {
            "processes": str(len(s.procs)),
            "agents": str(len(s.agents)),
            "backends": str(s.backend_count),
            "listen": str(s.listen_count),
            "agent_ports": str(agent_ports),
            "events": str(events),
            "collect": f"{s.collect_ms} ms",
            "updated": fmt_clock(s.ts),
        }
        for key, text in values.items():
            label = self.stat_values.get(key)
            if label is not None:
                label.configure(text=text)
        try:
            self.nb.tab(0, text=f"  Agent 与后端  {len(s.agents)}  ")
            self.nb.tab(1, text=f"  端口总览  {s.listen_count}  ")
            self.nb.tab(2, text=f"  事件时间线  {events}  ")
        except Exception:
            pass
        self.status_text.set(
            f"{s.data_source}　·　{len(s.procs)} 个进程　·　{len(s.agents)} 个 Agent　·　"
            f"{s.backend_count} 个后端　·　{s.listen_count} 个监听端口　·　采集 {s.collect_ms} ms"
            + ("　·　部分进程需要管理员权限才能读到详情" if s.error else ""))
        if "CIM" in s.data_source and not getattr(self, "_degraded_warned", False):
            self._degraded_warned = True
            self.after(400, lambda: messagebox.showwarning(
                APP_NAME,
                "当前是【降级采集模式】：这个 Python 没有安装 psutil。\n\n"
                "端口和命令行仍然可读，但内存 / 启动时间等信息会缺失，"
                "重启后端时还可能落到错误的目录（例如 system32）。\n\n"
                "建议执行：pip install psutil\n"
                "或改用装了 psutil 的解释器启动（启动监控.bat 会自动挑选）。"))

    # ------------------------- 渲染 -------------------------
    def render(self):
        self._render_tree()
        self._render_ports()
        self._render_detail()

    def _match_filter(self, *texts):
        f = (self.filter_text.get() or "").strip().lower()
        if not f:
            return True
        return any(f in (t or "").lower() for t in texts)

    def _render_tree(self):
        tree = self.tree
        sel = self.sel_pid
        tree.delete(*tree.get_children())
        self.tree_items = {}
        self._row_no = 0

        agents = self.snap.agents
        if self.only_agent.get():
            agents = [g for g in agents if g.rule.category == "agent"]

        for g in agents:
            root = g.proc
            if not self._match_filter(g.label, root.name, root.cmdline, root.cwd):
                if not any(self._match_filter(b.name, b.cmdline) for b in g.backends):
                    continue
            port_txt = ", ".join(f"{p}/{pr.lower()}" for pr, _a, p, _s in sorted(set(g.ports)))
            note = f"{len(g.backends)} 个后端" + (f"，端口 {port_txt}" if port_txt else "")
            tag = f"rule_{g.rule.id}"
            tree.tag_configure(tag, foreground=g.rule.color, font=(FONT, 9, "bold"))
            iid = f"pid{root.pid}"
            tree.insert("", "end", iid=iid, text=f"◆ {g.label}　{root.name}",
                        values=(ROLE_LABEL[ROLE_AGENT], root.pid, port_txt or "-",
                                human_size(root.rss), fmt_time(root.create_time), note),
                        tags=(tag,), open=True)
            self.tree_items[iid] = root.pid
            self._render_children(root, iid, g)

        if sel is not None and f"pid{sel}" in self.tree_items:
            try:
                tree.selection_set(f"pid{sel}")
                tree.see(f"pid{sel}")
            except Exception:
                pass
        elif sel is None and self.tree_items:
            # 首次加载：自动选中第一个 Agent，右侧直接有内容可看
            first = next(iter(self.tree_items))
            try:
                tree.selection_set(first)
                self.sel_pid = self.tree_items[first]
            except Exception:
                pass

    def _render_children(self, parent: Proc, parent_iid: str, group: AgentGroup):
        for cid in parent.children:
            child = self.snap.procs.get(cid)
            if child is None:
                continue
            role = child.role
            if self.compact.get() and role == ROLE_CORE:
                continue
            if (self.only_listen.get() and role == ROLE_BACKEND
                    and not child.ports and "MCP" not in child.reason):
                continue
            if not self._match_filter(child.name, child.cmdline, child.cwd, child.reason):
                # 自身不匹配时，如果后代有匹配的仍需展开
                if not self._subtree_matches(child):
                    continue
            icon = ROLE_ICON.get(role, "○")
            tag = {"agent": "agent", ROLE_BACKEND: "backend_port" if child.ports else "backend",
                   ROLE_CORE: "core", ROLE_CHILD: "child"}.get(role, "child")
            self._row_no = getattr(self, "_row_no", 0) + 1
            tags = (tag, "stripe") if self._row_no % 2 == 0 else (tag,)
            iid = f"pid{child.pid}"
            self.tree.insert(parent_iid, "end", iid=iid,
                             text=f"    {icon} {child.name}",
                             values=(ROLE_LABEL.get(role, role), child.pid,
                                     child.port_text or "-", human_size(child.rss),
                                     fmt_time(child.create_time), child.reason),
                             tags=tags, open=(role == ROLE_BACKEND))
            self.tree_items[iid] = child.pid
            if role != ROLE_AGENT:      # 嵌套的另一个 Agent 有自己的分组，不再重复展开
                self._render_children(child, iid, group)

    def _subtree_matches(self, proc: Proc) -> bool:
        for cid in proc.children:
            c = self.snap.procs.get(cid)
            if c is None:
                continue
            if self._match_filter(c.name, c.cmdline, c.cwd) or self._subtree_matches(c):
                return True
        return False

    def _render_ports(self):
        tree = self.port_tree
        tree.delete(*tree.get_children())
        self.port_items = {}
        for i, r in enumerate(self.snap.listen_rows):
            if self.hide_sys.get() and (r["port"] in SYSTEM_PORTS or r["port"] < 1024):
                if not r["owner_rule"] and r["role"] != ROLE_BACKEND:
                    continue
            if not self._match_filter(str(r["port"]), r["name"], r["owner"], r["cwd"], r["cmdline"]):
                continue
            if self.only_agent.get() and not r["owner_rule"]:
                continue
            if r["role"] == ROLE_BACKEND or r["owner_rule"]:
                tag = "backend"
            else:
                tag = "sys"
            iid = f"port{i}"
            tree.insert("", "end", iid=iid,
                        values=(r["port"], r["proto"], r["addr"], r["pid"], r["name"],
                                r["owner"], ROLE_LABEL.get(r["role"], r["role"]),
                                r["cwd"] or "-", short(r["cmdline"], 300)),
                        tags=(tag,))
            self.port_items[iid] = r["pid"]

    def _render_events(self):
        tree = self.ev_tree
        tree.delete(*tree.get_children())
        for i, e in enumerate(self.tracker.events[-800:]):
            tree.insert("", "end", iid=f"ev{i}",
                        values=(fmt_clock(e["ts"]), e["level"].upper(), e["text"]),
                        tags=(e["level"],))
        kids = tree.get_children()
        if kids:
            tree.see(kids[-1])

    def _render_detail(self):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if self.sel_pid is None or self.sel_pid not in self.snap.procs:
            self.hero_icon.configure(text="○", fg=C_ACCENT)
            self.hero_name.configure(text="（未选择进程）")
            self.hero_chip.configure(text="", bg=C_BG, fg=C_DIM)
            self.detail.insert("end", "在上方进程树里选择一个进程，这里会显示它的完整启动详情。\n", "k")
            self.detail.insert("end", "\n提示：Agent 主体（◆）和它下面的后端（▲）都可以点；"
                                      "右键可以执行完整操作。", "k")
            self.detail.configure(state="disabled")
            return
        p = self.snap.procs[self.sel_pid]
        if not p.create_time:
            # Agent / 后端之外的过程没有在采集时补齐信息，点选时按需补一次
            try:
                self.collector.enrich_one(p)
            except Exception:
                pass
        group = next((g for g in self.snap.agents if g.proc.pid == p.pid), None)
        if group is None:
            group = next((g for g in self.snap.agents
                          if any(d.pid == p.pid for d in g.descendants)), None)

        def head(t):
            self.detail.insert("end", f"\n{t}\n", "h")

        def kv(k, v, tag="v"):
            self.detail.insert("end", f"  {k:<10}", "k")
            self.detail.insert("end", f"{v}\n", tag)

        # 顶部 Hero：进程名 + 角色徽标
        role_color = {ROLE_AGENT: C_ACCENT, ROLE_BACKEND: C_GREEN,
                      ROLE_CORE: C_DIM2}.get(p.role, C_PURPLE)
        self.hero_icon.configure(text=ROLE_ICON.get(p.role, "○"), fg=role_color)
        self.hero_name.configure(text=f"{p.name}　PID {p.pid}")
        self.hero_chip.configure(text=ROLE_LABEL.get(p.role, p.role),
                                 bg=C_PANEL2, fg=role_color)

        if p.reason:
            self.detail.insert("end", f"{p.reason}\n", "k")
        if p.rule:
            rule = next((r for r in self.rules if r.id == p.rule), None)
            self.detail.insert("end", f"所属工具：", "k")
            self.detail.insert("end", f"{rule.label if rule else p.rule}\n", "good")

        head("启动详情")
        kv("命令行", p.cmdline or "（读取被拒绝，试试以管理员身份运行）", "mono")
        kv("工作目录", p.cwd or "（读取被拒绝）", "mono")
        kv("可执行文件", p.exe or "-", "mono")
        kv("启动时间", f"{fmt_time(p.create_time)}　（已运行 "
                       f"{human_duration(time.time() - p.create_time) if p.create_time else '-'}）")
        kv("用户", p.username or "-")
        kv("状态", p.status or "-")
        kv("内存/线程", f"{human_size(p.rss)}　/　{p.threads} 线程　CPU {p.cpu:.1f}%")
        kv("父进程", self._parent_chain_text(p))

        if p.ports:
            head("监听端口")
            for proto, addr, port, state in sorted(p.ports, key=lambda x: x[2]):
                self.detail.insert("end", f"  {proto} {addr}:{port}", "good")
                self.detail.insert("end", f"　{state}\n", "k")
            if p.conn_count:
                kv("活动连接", f"{p.conn_count} 条（ESTABLISHED 等）")
        else:
            head("监听端口")
            self.detail.insert("end", "  无（该进程没有监听任何 TCP/UDP 端口）\n", "k")

        if p.children:
            head(f"直接子进程（{len(p.children)}）")
            for cid in p.children[:40]:
                c = self.snap.procs.get(cid)
                if c is None:
                    continue
                mark = "▲" if c.role == ROLE_BACKEND else ("◆" if c.role == ROLE_AGENT else "·")
                self.detail.insert("end", f"  {mark} {c.name}", "v")
                self.detail.insert("end", f"  (PID {c.pid})  ", "k")
                self.detail.insert("end", (c.port_text or "-") + "\n", "good")

        if group is not None:
            head(f"所属 Agent：{group.label}　后端共 {len(group.backends)} 个")
            for b in group.backends:
                self.detail.insert("end", f"  ▲ {b.name} (PID {b.pid})　", "v")
                self.detail.insert("end", f"{b.port_text or '无端口'}　", "good")
                self.detail.insert("end", f"{b.reason}\n", "k")
            if group.backends:
                ports = sorted({pr for _p, _a, pr, _s in group.ports})
                if ports:
                    self.detail.insert("end", "\n  提示：", "k")
                    self.detail.insert(
                        "end",
                        f"该 Agent 一旦退出，上面这些后端（端口 {', '.join(map(str, ports))}）"
                        f"通常会随它一起被终止。需要长期可用请用「重启并占回端口」。\n", "warn")

        self.detail.configure(state="disabled")

    def _parent_chain_text(self, p: Proc) -> str:
        chain = []
        cur, hops = p, 0
        while cur and hops < 12:
            parent = self.snap.procs.get(cur.ppid)
            if not parent:
                break
            chain.append(f"{parent.name}(PID {parent.pid})")
            cur = parent
            hops += 1
        return " ← ".join(chain) if chain else "（父进程已退出 / 未知）"

    # ------------------------- 交互动作 -------------------------
    def _on_tree_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        pid = self.tree_items.get(sel[0])
        if pid is None:
            return
        self.sel_pid = pid
        self._render_detail()

    def _on_port_double(self, _e=None):
        sel = self.port_tree.selection()
        if not sel:
            return
        pid = self.port_items.get(sel[0])
        if pid is None:
            return
        self.nb.select(0)
        self.sel_pid = pid
        iid = f"pid{pid}"
        if iid in self.tree_items:
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.tree.focus(iid)
        else:
            self.filter_text.set("")
            self.compact.set(False)
            self.render()
            if iid in self.tree_items:
                self.tree.selection_set(iid)
                self.tree.see(iid)
        self._render_detail()

    def _selected_proc(self):
        if self.sel_pid is None:
            messagebox.showinfo(APP_NAME, "请先在进程树里选择一个进程。")
            return None
        p = self.snap.procs.get(self.sel_pid)
        if p is None:
            messagebox.showinfo(APP_NAME, "该进程已经退出了。")
            return None
        return p

    def copy_details(self):
        p = self._selected_proc()
        if not p:
            return
        text = self.detail.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_text.set(f"已复制 PID {p.pid} 的详情到剪贴板")

    def _launch_cmd_text(self, p: Proc) -> str:
        parts = [f"& {quote_ps(p.exe or p.cmdline.split(' ')[0])}"]
        # 从原始命令行里剥掉可执行文件部分，保留参数
        cmd = p.cmdline.strip()
        args = ""
        if p.exe and cmd.lower().startswith(p.exe.lower()):
            args = cmd[len(p.exe):].strip()
        elif cmd:
            m = re.match(r'^("[^"]+"|\S+)\s*(.*)$', cmd)
            if m:
                args = m.group(2)
        if args:
            parts.append(args)
        prefix = f"cd {quote_ps(p.cwd)}; " if p.cwd else ""
        return prefix + " ".join(parts)

    def copy_launch_cmd(self):
        p = self._selected_proc()
        if not p:
            return
        text = self._launch_cmd_text(p)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_text.set("已复制 PowerShell 启动命令到剪贴板")

    def open_exe_dir(self):
        p = self._selected_proc()
        if not p:
            return
        target = p.exe if p.exe and os.path.exists(p.exe) else (p.cwd if p.cwd and os.path.isdir(p.cwd) else "")
        if not target:
            messagebox.showinfo(APP_NAME, "拿不到该进程的可执行文件路径或工作目录。")
            return
        try:
            if os.path.isfile(target):
                subprocess.Popen(["explorer", "/select,", target])
            else:
                os.startfile(target)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"打开失败：{e}")

    def kill_tree(self):
        p = self._selected_proc()
        if not p:
            return
        if p.role == ROLE_AGENT:
            messagebox.showwarning(APP_NAME, "这是 Agent 主体进程，请谨慎操作。")
        if not messagebox.askyesno(APP_NAME, f"确定要结束 {p.name} (PID {p.pid}) 及其全部子进程吗？\n此操作不可撤销。"):
            return
        victim = self._collect_tree(p)
        if psutil is None:
            for pid in victim:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            for pid in reversed(victim):
                try:
                    psutil.Process(pid).kill()
                except Exception:
                    pass
        self.status_text.set(f"已请求结束 {len(victim)} 个进程")
        self.after(400, lambda: self.refresh(manual=True))

    def _collect_tree(self, p: Proc):
        out, stack = [], [p]
        while stack:
            cur = stack.pop()
            out.append(cur.pid)
            for cid in cur.children:
                c = self.snap.procs.get(cid)
                if c:
                    stack.append(c)
        return out

    def relaunch_detached(self):
        p = self._selected_proc()
        if not p:
            return
        cmdline = p.cmdline.strip()
        if not cmdline:
            messagebox.showinfo(APP_NAME, "拿不到该进程的命令行，无法重启（可能需要管理员权限）。")
            return
        ports = sorted({(pr, pt) for pr, _a, pt, _s in p.ports})
        port_txt = "、".join(str(pt) for _pr, pt in ports) if ports else "（不占端口）"
        if not messagebox.askyesno(
                APP_NAME,
                "重启方式：\n"
                f"① 结束当前进程 {p.name} (PID {p.pid}) 及其子进程，释放端口 {port_txt}\n"
                "② 等端口真正释放\n"
                "③ 用原命令行、原工作目录，以「脱离 Agent」的方式重新启动并占回同一个端口\n\n"
                "会先中断当前正在跑的服务（约 1~3 秒）。\n\n"
                f"命令：\n{short(cmdline, 300)}\n工作目录：{p.cwd or '(未获取到)'}\n\n继续？"):
            return
        args = list(p.argv) if p.argv else self._split_cmdline(cmdline)
        if not p.cwd:
            try:
                self.collector.enrich_one(p)      # 补一次工作目录，免得重启到别处
            except Exception:
                pass

        # ① 结束旧进程树，释放端口
        victim = self._collect_tree(p)
        for pid in reversed(victim):
            try:
                if psutil is not None:
                    psutil.Process(pid).kill()
                else:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        still_busy = {}
        if ports:
            self.status_text.set(f"已结束旧进程，等待端口 {port_txt} 释放…")
            self.update_idletasks()
            still_busy = wait_ports_free(ports, timeout=8.0, ignore_pids=[p.pid])
            if still_busy:
                messagebox.showwarning(
                    APP_NAME,
                    "端口仍被占用，未继续启动：\n"
                    + "\n".join(f"端口 {pt} 还被 PID {owner} 占用" for pt, owner in still_busy.items())
                    + "\n\n（占用者不属于这个后端，需要你手动处理）")

        # ② 启动
        try:
            launched = spawn_detached(args, p.cwd or None, exclude=(p.pid,))
        except Exception as e:
            messagebox.showerror(APP_NAME, f"启动失败：{e}")
            self.after(600, lambda: self.refresh(manual=True))
            return

        # ③ 确认端口重新监听
        listening = wait_ports_listening(ports, timeout=15.0) if ports else {}
        self.after(600, lambda: self.refresh(manual=True))
        if not launched.get("alive"):
            tried = "、".join(a.get("how", "?") for a in launched.get("attempts", []))
            messagebox.showwarning(
                APP_NAME,
                f"已依次尝试 {tried}，但新进程没能活下来。\n\n常见原因：\n"
                "· 该命令依赖 Agent 注入的环境变量或 stdin\n"
                "· 当前环境（例如 Agent 的作业对象）不允许新进程存活")
            return
        new_pid = launched["pid"]
        new_cwd = None
        try:
            new_cwd, _cl = read_peb_strings(new_pid)
        except Exception:
            pass
        wrong_dir = bool(new_cwd and p.cwd
                         and os.path.normcase(os.path.normpath(new_cwd))
                         != os.path.normcase(os.path.normpath(p.cwd)))
        if ports and len(listening) == len(ports):
            self.status_text.set(
                f"已重启：旧进程 {len(victim)} 个已结束，端口 {port_txt} 由新进程 PID {new_pid} 重新监听")
        elif ports:
            missing = [pt for _pr, pt in ports if pt not in listening]
            self.status_text.set(
                f"新进程 PID {new_pid} 已启动（{launched['how']}），但端口 "
                f"{'、'.join(map(str, missing))} 还没开始监听")
        else:
            self.status_text.set(f"已用原命令独立启动 PID {new_pid}（{launched['how']}）")
        if wrong_dir:
            messagebox.showwarning(
                APP_NAME,
                f"新进程的工作目录是：\n  {new_cwd}\n与原进程的：\n  {p.cwd}\n不一致，"
                "服务可能没有指向原来的项目目录，请检查。")
        elif not new_cwd:
            self.status_text.set(self.status_text.get() + "　（未能确认新进程的工作目录）")

    @staticmethod
    def _split_cmdline(cmdline: str):
        """把 Windows 命令行拆成参数列表（用系统自身的解析器，保证路径带空格也不出错）。"""
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

    def _clear_events(self):
        self.tracker.events.clear()
        self._render_events()

    def export_events(self):
        if not self.tracker.events:
            messagebox.showinfo(APP_NAME, "还没有事件可导出。")
            return
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="导出事件", initialdir=REPORT_DIR, defaultextension=".md",
            initialfile=f"events_{datetime.now():%Y%m%d_%H%M%S}.md",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {APP_NAME} 事件时间线\n\n导出时间：{fmt_time(time.time())}\n\n")
            for e in self.tracker.events:
                f.write(f"- `{fmt_time(e['ts'])}` [{e['level']}] {e['text']}\n")
        self.status_text.set(f"已导出事件到 {path}")

    def export_report(self):
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="导出报告", initialdir=REPORT_DIR, defaultextension=".md",
            initialfile=f"agent_backends_{datetime.now():%Y%m%d_%H%M%S}.md",
            filetypes=[("Markdown", "*.md"), ("JSON", "*.json")])
        if not path:
            return
        try:
            if path.lower().endswith(".json"):
                content = build_json(self.snap, self.tracker.events)
            else:
                content = build_markdown(self.snap, self.tracker.events)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.status_text.set(f"报告已保存：{path}")
            if messagebox.askyesno(APP_NAME, f"已保存到：\n{path}\n\n现在打开吗？"):
                os.startfile(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存失败：{e}")

    def open_rules(self):
        if not os.path.isfile(RULES_FILE):
            load_rules()
        try:
            os.startfile(RULES_FILE)
        except Exception:
            subprocess.Popen(["notepad.exe", RULES_FILE])

    def reload_rules(self):
        self.rules = load_rules()
        self.collector.rules = self.rules
        self.refresh(manual=True)
        self.status_text.set(f"已重新加载 {len(self.rules)} 条识别规则")


# --------------------------------------------------------------------------- #
# 命令行模式
# --------------------------------------------------------------------------- #

def cli(args) -> int:
    rules = load_rules()
    collector = Collector(rules)

    def one(out_path=None, as_json=False):
        snap = collector.collect()
        if as_json or (out_path and out_path.lower().endswith(".json")):
            text = build_json(snap)
        else:
            text = build_markdown(snap)
        if out_path:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(text)
            safe_print(f"已写入 {out_path}")
        else:
            safe_print(text)
        return 0

    if args.report or args.json:
        return one(args.json, bool(args.json))
    if args.watch:
        try:
            while True:
                snap = collector.collect()
                os.system("cls" if IS_WINDOWS else "clear")
                safe_print(build_markdown(snap))
                time.sleep(max(1.0, args.watch))
        except KeyboardInterrupt:
            return 0
    return -1


def safe_print(text):
    try:
        sys.stdout.write(str(text) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{VERSION}")
    parser.add_argument("--report", action="store_true", help="打印 Markdown 快照报告后退出")
    parser.add_argument("--json", metavar="FILE", help="把 JSON 快照写入文件后退出")
    parser.add_argument("--watch", type=float, metavar="SEC", help="按间隔刷新并在控制台打印")
    parser.add_argument("--interval", type=float, default=2.0, help="图形界面自动刷新间隔（秒）")
    args = parser.parse_args()

    if args.report or args.json or args.watch:
        code = cli(args)
        if code >= 0:
            return code

    if psutil is None:
        safe_print("提示：未安装 psutil，将降级使用 PowerShell 采集（信息较少）。建议：pip install psutil")

    app = MonitorApp(interval=args.interval)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
