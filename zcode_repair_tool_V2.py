#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCode 修复工具 V2 (ZCode Repair Tool V2)

基于 V1 扩展, 新增 "5. 备份数据库和模型列表" 页面:
  1. 备份 会话上下文库 db.sqlite + 任务索引库 tasks-index.sqlite + 模型配置 config.json
  2. 源路径与目标路径均可自由选择(默认值见 BACKUP_DEFAULTS)
  3. 自动创建 "ZCode+年月日时" 子目录(如 ZCode2026081716)
  4. 可选连带备份 SQLite 的 -wal/-shm(推荐, 防未合并数据丢失)
  5. 备份完成后自动写入 备份说明与恢复指南.md

适用场景: ZCode 的 setting.json 被全零字节(0x00)填充损坏, 导致左侧项目/任务列表丢失;
以及需要定期备份 会话库/任务库/模型配置 的日常数据保全。

功能(继承 V1):
  1. 扫描/手动选择 程序安装目录、用户配置目录(~/.zcode)、数据目录(dataBaseDir指向)
  2. 诊断 setting.json 状态(正常/全零损坏/解析失败), 检测 .corrupt 损坏备份
  3. 定位 tasks-index.sqlite(自动挑任务数最多的活跃库), 提取全部项目和任务
  4. 生成修复版 setting.json(lastWorkspaceSession 填充 workspacePurpose=project)
  5. 项目排序分布: 最近活跃 / 任务数 / 名称 / 磁盘分组
  6. 导出修复文件(不覆盖原文件) 或 备份后直接部署
  7. [V2新增] 备份 数据库与模型列表

运行: python zcode_repair_tool_V2.py
依赖: 仅 Python 标准库(tkinter/sqlite3/json/shutil/time)
"""

import datetime
import json
import os
import shutil
import sqlite3
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "ZCode 修复工具 V2"
APP_VERSION = "2.0.0"

# V2 新增: 备份功能默认路径(均可自由修改)
BACKUP_DEFAULTS = {
    "db": r"C:\Users\DX\.zcode\cli\db\db.sqlite",                        # 会话上下文库
    "tasks": r"K:\编程区\@@moxing_Data\.zcode\v2\tasks-index.sqlite",    # 任务索引活跃库
    "config": r"K:\编程区\@@moxing_Data\.zcode\v2\config.json",          # 模型provider配置
    "dest": r"K:\编程区\@@大模型备份",                                     # 备份目标根目录
}

# 本文件所在目录(用于定位错误日志)
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(TOOL_DIR, "error.log")


def _write_log(text):
    """写入错误日志文件(异常时可见)"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def _global_excepthook(exc_type, exc_value, exc_tb):
    """全局异常钩子: 写日志 + 弹窗, 避免双击启动时闪退看不到错误"""
    import traceback
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _write_log("未捕获异常:\n" + msg)
    try:
        root_tmp = tk.Tk()
        root_tmp.withdraw()
        tk.messagebox.showerror("ZCode修复工具错误", "程序发生错误:\n\n%s\n\n详细日志已写入:\n%s" % (exc_value, LOG_FILE))
        root_tmp.destroy()
    except Exception:
        pass

# 项目路径磁盘分组优先序
DISK_ORDER = ["K:\\编程区", "K:\\rom", "K:\\专用下载", "D:\\Down", "C:\\Users"]


def disk_group_key(path):
    """按磁盘/目录前缀分组排序键"""
    p = path.replace("/", "\\")
    for i, prefix in enumerate(DISK_ORDER):
        if p.startswith(prefix):
            return i
    if p.startswith("K:\\"):
        return len(DISK_ORDER)
    if p.startswith("D:\\"):
        return len(DISK_ORDER) + 1
    return len(DISK_ORDER) + 2


# ============================================================
# 核心逻辑
# ============================================================

class ZCodeScanner:
    """扫描、诊断、修复 ZCode 配置"""

    def __init__(self):
        self.program_dir = ""
        self.config_dir = ""
        self.data_dir = ""
        self.settings_path = ""
        self.settings = None
        self.settings_ok = False
        self.settings_error = ""
        self.corrupt_files = []
        self.databases = []
        self.db_path = ""
        self.db_tables = {}
        self.tasks_total = 0
        self.projects = []
        self.tasks = []
        self.messages = []          # 诊断日志

    def log(self, msg):
        self.messages.append(msg)

    # ---------- 路径发现 ----------

    def scan_default_paths(self):
        """扫描默认路径: 用户配置目录 + 从配置推断数据目录"""
        self.log("开始默认路径扫描...")
        home = os.path.expanduser("~")
        self.config_dir = os.path.join(home, ".zcode")
        self.log("用户配置目录(默认): %s" % self.config_dir)
        sp = os.path.join(self.config_dir, "v2", "setting.json")
        if os.path.isfile(sp):
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if cfg.get("dataBaseDir"):
                    self.data_dir = cfg["dataBaseDir"]
                    self.log("从配置读取 dataBaseDir: %s" % self.data_dir)
            except Exception:
                self.log("配置读取失败(可能损坏)")
        # 常见数据目录探测
        if not self.data_dir:
            for cand in ["K:\\编程区\\@@moxing_Data", "K:\\编程区"]:
                if os.path.isdir(cand):
                    self.data_dir = cand
                    self.log("探测到数据目录: %s" % cand)
                    break
        # 程序目录探测
        self.program_dir = self._detect_program_dir()
        if self.program_dir:
            self.log("探测到程序目录: %s" % self.program_dir)
        return True

    @staticmethod
    def _detect_program_dir():
        """探测 ZCode 程序安装目录"""
        for cand in ["D:\\Soft\\ZCode", os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "ZCode")]:
            if os.path.isfile(os.path.join(cand, "ZCode.exe")):
                return cand
        return ""

    # ---------- 诊断 ----------

    def find_settings(self):
        """定位 setting.json: 配置目录优先, 数据目录兜底"""
        self.settings_path = ""
        self.settings = None
        self.settings_ok = False
        self.settings_error = ""
        candidates = []
        if self.config_dir:
            candidates.append(os.path.join(self.config_dir, "v2", "setting.json"))
        if self.data_dir:
            candidates.append(os.path.join(self.data_dir, ".zcode", "v2", "setting.json"))
        for c in candidates:
            c = os.path.normpath(c)
            if os.path.isfile(c):
                self.settings_path = c
                break
        if not self.settings_path:
            self.settings_error = "未找到 setting.json"
            self.log("ERROR: %s" % self.settings_error)
            return False
        self.log("定位 setting.json: %s" % self.settings_path)
        try:
            with open(self.settings_path, "rb") as f:
                raw = f.read()
            if len(raw) > 0 and sum(1 for b in raw if b != 0) == 0:
                self.settings_error = "setting.json 全零损坏 (%d 字节全部为 0x00)" % len(raw)
                self.log("ERROR: %s" % self.settings_error)
                self.settings_ok = False
                return False
            self.settings = json.loads(raw.decode("utf-8"))
            self.settings_ok = True
            self.log("setting.json 正常 (%d 字节)" % len(raw))
            if self.settings.get("dataBaseDir") and not self.data_dir:
                self.data_dir = self.settings["dataBaseDir"]
                self.log("补充 dataBaseDir: %s" % self.data_dir)
            return True
        except Exception as e:
            self.settings_error = "setting.json 解析失败: %s" % e
            self.log("ERROR: %s" % self.settings_error)
            self.settings_ok = False
            return False

    def find_corrupt_files(self):
        """查找 setting.json.corrupt-* 损坏备份"""
        self.corrupt_files = []
        bases = [self.config_dir, self.data_dir]
        if self.data_dir:
            bases.append(os.path.join(self.data_dir, ".zcode"))
        for base in bases:
            if not base:
                continue
            v2 = os.path.join(base, "v2")
            if not os.path.isdir(v2):
                continue
            try:
                names = os.listdir(v2)
            except Exception:
                continue
            for fn in names:
                if fn.startswith("setting.json.corrupt"):
                    fp = os.path.join(v2, fn)
                    try:
                        with open(fp, "rb") as f:
                            data = f.read()
                        nonzero = sum(1 for b in data if b != 0)
                    except Exception:
                        nonzero = -1
                    self.corrupt_files.append((fp, len(data), nonzero))
                    if nonzero == 0:
                        self.log("损坏备份(全零): %s (%d 字节)" % (fp, len(data)))
                    elif nonzero > 0:
                        self.log("损坏备份(部分有效): %s (%d 字节, %d 非零)" % (fp, len(data), nonzero))
        return self.corrupt_files

    def find_databases(self):
        """在所有可能位置找 tasks-index.sqlite, 返回 [(path, size, tasks, mtime)]"""
        self.databases = []
        bases = [self.config_dir, self.data_dir]
        if self.data_dir:
            bases.append(os.path.join(self.data_dir, ".zcode"))
        seen = set()
        for base in bases:
            if not base:
                continue
            for rel in ["v2/tasks-index.sqlite", ".zcode/v2/tasks-index.sqlite"]:
                p = os.path.normpath(os.path.join(base, rel))
                if p in seen or not os.path.isfile(p):
                    continue
                seen.add(p)
                try:
                    size = os.path.getsize(p)
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p))
                    tasks = self._count_tasks(p)
                    self.databases.append((p, size, tasks, mtime))
                except Exception:
                    pass
        # 任务数降序, 活跃库排最前
        self.databases.sort(key=lambda x: -x[2])
        for p, size, tasks, mtime in self.databases:
            self.log("数据库: %s | %d 条任务 | %.1f KB | %s" % (
                p, tasks, size / 1024.0, mtime.strftime("%m-%d %H:%M")))
        return self.databases

    @staticmethod
    def _count_tasks(db_path):
        """只读统计任务数, 失败返回 -1"""
        try:
            uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True)
            n = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            con.close()
            return n
        except Exception:
            return -1

    @staticmethod
    def open_db(db_path):
        """只读打开数据库连接"""
        try:
            uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
            return sqlite3.connect(uri, uri=True)
        except Exception:
            return None

    def analyze_db(self, db_path=None):
        """分析数据库: 表统计 + 项目提取 + 任务提取"""
        if db_path:
            self.db_path = db_path
        if not self.db_path or not os.path.isfile(self.db_path):
            return False
        con = self.open_db(self.db_path)
        if con is None:
            self.log("ERROR: 无法只读打开数据库")
            return False
        cur = con.cursor()
        self.db_tables = {}
        for (t,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            try:
                self.db_tables[t] = cur.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
            except Exception:
                self.db_tables[t] = -1
        self.tasks_total = self.db_tables.get("tasks", 0)
        self.log("分析数据库: %s (%d 条任务)" % (self.db_path, self.tasks_total))
        # 项目提取
        self.projects = []
        rows = cur.execute(
            "SELECT workspace_path, COUNT(*) c, MAX(updated_at) mx FROM tasks "
            "GROUP BY workspace_path ORDER BY mx DESC"
        ).fetchall()
        for wp, c, mx in rows:
            if not wp:
                continue
            last_str = datetime.datetime.fromtimestamp(mx / 1000).strftime("%m-%d %H:%M") if mx else "?"
            self.projects.append({
                "path": wp, "count": c, "last": mx, "last_str": last_str
            })
        # 任务提取(限制数量避免卡顿)
        self.tasks = []
        for r in cur.execute(
            "SELECT workspace_path, task_id, title, task_status FROM tasks ORDER BY created_at"
        ).fetchall():
            self.tasks.append({
                "workspace": r[0], "task_id": r[1],
                "title": (r[2] or ""), "status": r[3] or ""
            })
        con.close()
        self.log("提取项目 %d 个, 任务 %d 条" % (len(self.projects), len(self.tasks)))
        return True

    # ---------- 修复生成 ----------

    def sort_projects(self, mode="recent"):
        """项目排序: recent 最近活跃 / tasks 任务数 / name 名称 / disk 磁盘分组"""
        ps = list(self.projects)
        if mode == "recent":
            ps.sort(key=lambda p: (p["last"] or 0), reverse=True)
        elif mode == "tasks":
            ps.sort(key=lambda p: (p["count"], p["last"] or 0), reverse=True)
        elif mode == "name":
            ps.sort(key=lambda p: p["path"].lower())
        elif mode == "disk":
            ps.sort(key=lambda p: (disk_group_key(p["path"]), p["path"].lower()))
        return ps

    def generate_fix(self, sort_mode="recent"):
        """生成修复版 setting.json 内容(JSON字符串)"""
        base = dict(self.settings) if isinstance(self.settings, dict) else {}
        base.setdefault("locale", "zh-CN")
        base.setdefault("recentProjects", [])
        # lastWorkspaceSession: project 在前, conversation 保留在后
        old_lws = base.get("lastWorkspaceSession", [])
        conv = []
        if isinstance(old_lws, list):
            conv = [s for s in old_lws
                    if isinstance(s, dict) and s.get("workspacePurpose") == "conversation"]
        projects = self.sort_projects(sort_mode)
        new_lws = [{"kind": "local", "workspacePath": p["path"],
                    "workspacePurpose": "project"} for p in projects]
        new_lws.extend(conv)
        base["lastWorkspaceSession"] = new_lws
        # recentProjects 同步为项目路径
        rp = [p["path"] for p in projects]
        cur_rp = base.get("recentProjects") or []
        base["recentProjects"] = rp + [x for x in cur_rp if x not in rp]
        # dataBaseDir 兜底
        if self.data_dir and not base.get("dataBaseDir"):
            base["dataBaseDir"] = self.data_dir
        return json.dumps(base, ensure_ascii=False, indent=2)

    def deploy(self, content, backup=True):
        """部署修复版到 setting.json 路径(先备份), 返回 (成功, 消息)"""
        if not self.settings_path:
            return False, "未定位到 setting.json 路径"
        target = self.settings_path
        try:
            if backup and os.path.isfile(target):
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                bk = target + ".backup-" + stamp
                with open(target, "rb") as f:
                    orig = f.read()
                with open(bk, "wb") as f:
                    f.write(orig)
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            return True, "已部署到 %s" % target
        except Exception as e:
            return False, "部署失败: %s" % e


# ============================================================
# 界面
# ============================================================

class ZCodeRepairApp:
    """ZCode 修复工具主窗口"""

    def __init__(self, root):
        self.root = root
        self.scanner = ZCodeScanner()
        self.sort_mode = tk.StringVar(value="recent")
        self.fix_content = ""
        root.title("%s v%s" % (APP_TITLE, APP_VERSION))
        root.geometry("1080x720")
        root.minsize(900, 600)
        self._build_ui()
        # 启动时自动扫描默认路径
        self.root.after(200, self.on_auto_scan)

    # ---------- UI 构建 ----------

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        self.nb = ttk.Notebook(main)
        self.nb.pack(fill="both", expand=True)
        self.tab_paths = ttk.Frame(self.nb)
        self.tab_diag = ttk.Frame(self.nb)
        self.tab_proj = ttk.Frame(self.nb)
        self.tab_fix = ttk.Frame(self.nb)
        self.tab_backup = ttk.Frame(self.nb)
        self.nb.add(self.tab_paths, text=" 1. 路径配置 ")
        self.nb.add(self.tab_diag, text=" 2. 诊断分析 ")
        self.nb.add(self.tab_proj, text=" 3. 项目任务 ")
        self.nb.add(self.tab_fix, text=" 4. 修复部署 ")
        self.nb.add(self.tab_backup, text=" 5. 备份数据库和模型 ")

        self._build_tab_paths()
        self._build_tab_diag()
        self._build_tab_proj()
        self._build_tab_fix()
        self._build_tab_backup()

        # 状态栏
        self.status = ttk.Label(main, text="就绪", relief="sunken", anchor="w", padding=(4, 2))
        self.status.pack(fill="x", pady=(6, 0))

    def _build_tab_paths(self):
        f = self.tab_paths
        lf = ttk.LabelFrame(f, text="目录配置", padding=10)
        lf.pack(fill="x", pady=6)

        rows = [
            ("程序安装目录(可选):", "program"),
            ("用户配置目录(~/.zcode):", "config"),
            ("数据目录(dataBaseDir指向):", "data"),
        ]
        self.path_vars = {}
        for i, (label, key) in enumerate(rows):
            ttk.Label(lf, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            ent = ttk.Entry(lf, textvariable=var, width=70)
            ent.grid(row=i, column=1, sticky="we", pady=3, padx=6)
            self.path_vars[key] = var
            ttk.Button(lf, text="选择...", width=8,
                       command=lambda k=key: self._pick_dir(k)).grid(row=i, column=2, padx=2)
        lf.columnconfigure(1, weight=1)

        btnf = ttk.Frame(f)
        btnf.pack(fill="x", pady=6)
        ttk.Button(btnf, text="自动扫描默认路径", command=self.on_auto_scan).pack(side="left", padx=4)
        ttk.Button(btnf, text="重新加载路径到界面", command=self.on_sync_paths).pack(side="left", padx=4)

        self.path_log = tk.Text(f, height=12, state="disabled", font=("Consolas", 9))
        self.path_log.pack(fill="both", expand=True, pady=6)
        ttk.Label(f, text="提示: 数据目录即 setting.json 中 dataBaseDir 指向的目录, 内含 .zcode\\v2\\tasks-index.sqlite",
                  foreground="#888").pack(anchor="w")

    def _build_tab_diag(self):
        f = self.tab_diag
        # setting.json 状态
        lf1 = ttk.LabelFrame(f, text="setting.json 状态", padding=8)
        lf1.pack(fill="x", pady=4)
        self.lbl_setting = ttk.Label(lf1, text="尚未诊断", wraplength=900)
        self.lbl_setting.pack(anchor="w")

        lf2 = ttk.LabelFrame(f, text="损坏备份 (.corrupt)", padding=8)
        lf2.pack(fill="x", pady=4)
        self.lbl_corrupt = ttk.Label(lf2, text="尚未诊断", wraplength=900)
        self.lbl_corrupt.pack(anchor="w")

        lf3 = ttk.LabelFrame(f, text="任务数据库 (tasks-index.sqlite)", padding=8)
        lf3.pack(fill="x", pady=4)
        self.lbl_db = ttk.Label(lf3, text="尚未诊断", wraplength=900, justify="left")
        self.lbl_db.pack(anchor="w")

        lf4 = ttk.LabelFrame(f, text="诊断日志", padding=8)
        lf4.pack(fill="both", expand=True, pady=4)
        self.diag_log = tk.Text(lf4, height=14, state="disabled", font=("Consolas", 9))
        self.diag_log.pack(fill="both", expand=True)

        btnf = ttk.Frame(f)
        btnf.pack(fill="x", pady=6)
        ttk.Button(btnf, text="开始诊断", command=self.on_diagnose).pack(side="left", padx=4)
        ttk.Button(btnf, text="清空日志", command=lambda: self._clear_text(self.diag_log)).pack(side="left", padx=4)

    def _build_tab_proj(self):
        f = self.tab_proj
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="排序方式:").pack(side="left", padx=2)
        for val, txt in [("recent", "最近活跃"), ("tasks", "任务数"), ("name", "项目名称"), ("disk", "磁盘分组")]:
            ttk.Radiobutton(top, text=txt, value=val, variable=self.sort_mode).pack(side="left", padx=4)
        ttk.Button(top, text="刷新列表", command=self.on_refresh_projects).pack(side="left", padx=6)

        # 项目列表
        pf = ttk.LabelFrame(f, text="项目列表", padding=4)
        pf.pack(fill="both", expand=True, pady=4)
        cols = ("idx", "path", "count", "last")
        self.tree_proj = ttk.Treeview(pf, columns=cols, show="headings", height=14)
        self.tree_proj.heading("idx", text="#")
        self.tree_proj.heading("path", text="项目路径")
        self.tree_proj.heading("count", text="任务数")
        self.tree_proj.heading("last", text="最近活跃")
        self.tree_proj.column("idx", width=40, anchor="center")
        self.tree_proj.column("path", width=520, anchor="w")
        self.tree_proj.column("count", width=70, anchor="center")
        self.tree_proj.column("last", width=110, anchor="center")
        sb = ttk.Scrollbar(pf, orient="vertical", command=self.tree_proj.yview)
        self.tree_proj.configure(yscrollcommand=sb.set)
        self.tree_proj.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree_proj.bind("<<TreeviewSelect>>", self.on_project_select)

        # 任务列表(选中项目后显示)
        tf = ttk.LabelFrame(f, text="选中项目的任务(最多200条)", padding=4)
        tf.pack(fill="both", expand=True, pady=4)
        cols2 = ("ws", "title", "status")
        self.tree_task = ttk.Treeview(tf, columns=cols2, show="headings", height=8)
        self.tree_task.heading("ws", text="工作区")
        self.tree_task.heading("title", text="任务标题")
        self.tree_task.heading("status", text="状态")
        self.tree_task.column("ws", width=260, anchor="w")
        self.tree_task.column("title", width=460, anchor="w")
        self.tree_task.column("status", width=90, anchor="center")
        sb2 = ttk.Scrollbar(tf, orient="vertical", command=self.tree_task.yview)
        self.tree_task.configure(yscrollcommand=sb2.set)
        self.tree_task.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")

        self.lbl_proj_summary = ttk.Label(f, text="")
        self.lbl_proj_summary.pack(anchor="w", pady=2)

    def _build_tab_fix(self):
        f = self.tab_fix
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="生成排序:").pack(side="left", padx=2)
        for val, txt in [("recent", "最近活跃"), ("tasks", "任务数"), ("name", "项目名称"), ("disk", "磁盘分组")]:
            ttk.Radiobutton(top, text=txt, value=val, variable=self.sort_mode).pack(side="left", padx=4)
        ttk.Button(top, text="生成修复内容", command=self.on_generate).pack(side="left", padx=6)

        lf = ttk.LabelFrame(f, text="修复内容预览", padding=4)
        lf.pack(fill="both", expand=True, pady=4)
        self.txt_preview = tk.Text(lf, font=("Consolas", 9), state="disabled", wrap="none")
        sby = ttk.Scrollbar(lf, orient="vertical", command=self.txt_preview.yview)
        sbx = ttk.Scrollbar(lf, orient="horizontal", command=self.txt_preview.xview)
        self.txt_preview.configure(yscrollcommand=sby.set, xscrollcommand=sbx.set)
        self.txt_preview.grid(row=0, column=0, sticky="nsew")
        sby.grid(row=0, column=1, sticky="ns")
        sbx.grid(row=1, column=0, sticky="ew")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        btnf = ttk.Frame(f)
        btnf.pack(fill="x", pady=6)
        ttk.Button(btnf, text="导出修复文件(不覆盖原文件)", command=self.on_export).pack(side="left", padx=4)
        ttk.Button(btnf, text="备份并部署到配置目录", command=self.on_deploy).pack(side="left", padx=4)
        self.lbl_fix_status = ttk.Label(f, text="", foreground="#0a7")
        self.lbl_fix_status.pack(anchor="w", pady=2)

    # ---------- V2: 备份数据库和模型列表 ----------

    def _build_tab_backup(self):
        """标签页5: 备份 会话库/任务库/模型配置 到 目标目录"""
        f = self.tab_backup

        # 备份源
        lf1 = ttk.LabelFrame(f, text="备份源(默认路径, 可编辑 / 可浏览选择)", padding=10)
        lf1.pack(fill="x", pady=6)
        self.bk_src_vars = {
            "db": tk.StringVar(value=BACKUP_DEFAULTS["db"]),
            "tasks": tk.StringVar(value=BACKUP_DEFAULTS["tasks"]),
            "config": tk.StringVar(value=BACKUP_DEFAULTS["config"]),
        }
        rows = [
            ("会话上下文库 db.sqlite:", "db"),
            ("任务索引库 tasks-index.sqlite:", "tasks"),
            ("模型配置 config.json:", "config"),
        ]
        for i, (label, key) in enumerate(rows):
            ttk.Label(lf1, text=label).grid(row=i, column=0, sticky="w", pady=3)
            ent = ttk.Entry(lf1, textvariable=self.bk_src_vars[key], width=68)
            ent.grid(row=i, column=1, sticky="we", pady=3, padx=6)
            ttk.Button(lf1, text="选择...", width=8,
                       command=lambda k=key: self._bk_pick_file(k)).grid(row=i, column=2, padx=2)
        lf1.columnconfigure(1, weight=1)
        self.bk_include_wal = tk.BooleanVar(value=True)
        ttk.Checkbutton(lf1, text="连带备份 SQLite 的 -wal/-shm 文件(推荐: 防未合并数据丢失)",
                        variable=self.bk_include_wal).grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(lf1, text="提示: tasks-index.sqlite 的 WAL 可能含未合并数据(如 4MB), 仅备份主文件会丢这部分",
                  foreground="#888").grid(row=len(rows) + 1, column=0, columnspan=3, sticky="w")

        # 备份目标
        lf2 = ttk.LabelFrame(f, text="备份目标(可编辑 / 可浏览选择)", padding=10)
        lf2.pack(fill="x", pady=6)
        self.bk_dest_var = tk.StringVar(value=BACKUP_DEFAULTS["dest"])
        ttk.Label(lf2, text="目标根目录:").grid(row=0, column=0, sticky="w", pady=3)
        ent = ttk.Entry(lf2, textvariable=self.bk_dest_var, width=68)
        ent.grid(row=0, column=1, sticky="we", pady=3, padx=6)
        ttk.Button(lf2, text="选择...", width=8,
                   command=lambda: self._bk_pick_dir()).grid(row=0, column=2, padx=2)
        self.lbl_bk_folder = ttk.Label(lf2, text="", foreground="#06c")
        self.lbl_bk_folder.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        lf2.columnconfigure(1, weight=1)
        self._refresh_bk_folder_preview()

        # 操作按钮
        btnf = ttk.Frame(f)
        btnf.pack(fill="x", pady=6)
        ttk.Button(btnf, text="开始备份", command=self.on_backup).pack(side="left", padx=4)
        ttk.Button(btnf, text="刷新文件夹名预览", command=self._refresh_bk_folder_preview).pack(side="left", padx=4)
        self.lbl_bk_status = ttk.Label(f, text="", foreground="#0a7")
        self.lbl_bk_status.pack(anchor="w", pady=2)

        # 日志
        lf3 = ttk.LabelFrame(f, text="备份日志", padding=4)
        lf3.pack(fill="both", expand=True, pady=6)
        self.txt_bk_log = tk.Text(lf3, height=12, state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(lf3, orient="vertical", command=self.txt_bk_log.yview)
        self.txt_bk_log.configure(yscrollcommand=sb.set)
        self.txt_bk_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _refresh_bk_folder_preview(self):
        """刷新自动生成的备份子文件夹名预览: ZCode+年月日时"""
        sub = "ZCode" + time.strftime("%Y%m%d%H")
        root = self.bk_dest_var.get().strip() or "(未设置)"
        full = os.path.join(root, sub) if root != "(未设置)" else "(未设置目标)"
        self.lbl_bk_folder.config(text="本次备份将自动创建文件夹: %s" % full)

    def _bk_pick_file(self, key):
        p = filedialog.askopenfilename(title="选择备份源文件")
        if p:
            self.bk_src_vars[key].set(p)

    def _bk_pick_dir(self):
        d = filedialog.askdirectory(title="选择备份目标根目录")
        if d:
            self.bk_dest_var.set(d)
            self._refresh_bk_folder_preview()

    def _bk_log(self, text):
        self.txt_bk_log.config(state="normal")
        self.txt_bk_log.insert("end", text + "\n")
        self.txt_bk_log.see("end")
        self.txt_bk_log.config(state="disabled")

    def _bk_build_readme(self, srcs, copied, target):
        """生成 备份说明与恢复指南.md 内容"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        lines.append("# ZCode 数据备份说明与恢复指南")
        lines.append("")
        lines.append("- 备份时间: %s" % now)
        lines.append("- 备份工具: %s v%s" % (APP_TITLE, APP_VERSION))
        lines.append("- 备份目录: %s" % target)
        lines.append("")
        lines.append("## 一、备份来源(本目录文件与原始位置的对应)")
        lines.append("")
        lines.append("| 本目录文件 | 备份时来源路径 | 用途 |")
        lines.append("|---|---|---|")
        for _base, src, _label in srcs:
            lines.append("| %s | %s | 见下表 |" % (os.path.basename(src), src))
        lines.append("")
        lines.append("## 二、各文件用途")
        lines.append("")
        lines.append("| 文件 | 内容 |")
        lines.append("|---|---|")
        lines.append("| db.sqlite | ZCode 会话上下文库(cli\\db), 含 session/message/part, 即全部对话上下文 |")
        lines.append("| tasks-index.sqlite | 任务索引库(active tasks), 含全部任务与项目分组 |")
        lines.append("| config.json | 模型 provider 配置(provider 列表/自定义 apiKey/模型清单) |")
        lines.append("| tasks-index.sqlite-wal/-shm | SQLite WAL 日志与共享内存(若同目录存在), 必须与主库一起还原 |")
        lines.append("")
        lines.append("## 三、恢复方法(将本目录文件还原到原始位置)")
        lines.append("")
        lines.append("1. 完全退出 ZCode(任务管理器结束所有 ZCode 进程, 含托盘)")
        lines.append("2. 备份目标位置的现有文件(避免误覆盖)")
        lines.append("3. 用本目录的文件逐个覆盖对应原始路径:")
        for _base, src, _label in srcs:
            lines.append("   - %s -> %s" % (os.path.basename(src), src))
        lines.append("   - 若存在 -wal/-shm 一并覆盖")
        lines.append("4. 重新打开 ZCode, 验证项目/任务/模型列表恢复")
        lines.append("")
        lines.append("## 四、本次实际复制文件清单")
        lines.append("")
        for c in copied:
            lines.append("- %s" % c)
        lines.append("")
        lines.append("## 五、注意事项")
        lines.append("")
        lines.append("- tasks-index.sqlite 若带 -wal/-shm 一并备份/还原, 否则可能丢未合并数据")
        lines.append("- 备份建议在 ZCode 退出状态下进行, 数据一致性最佳")
        lines.append("- 恢复 config.json 前先备份现有 config.json(被 ZCode 重置后无自动备份)")
        lines.append("- 恢复后若模型列表异常, 重启 ZCode 联网用 OAuth 凭证重新拉取")
        return "\n".join(lines)

    def on_backup(self):
        """执行备份: 复制3个源文件(+wal/shm)到 ZCode+年月日时 子目录, 并写说明文档"""
        src_keys = [("db", "会话上下文库"), ("tasks", "任务索引库"), ("config", "模型配置")]
        srcs = []
        missing = []
        for key, label in src_keys:
            p = self.bk_src_vars[key].get().strip()
            if not p:
                missing.append("%s(路径为空)" % label)
            elif not os.path.isfile(p):
                missing.append("%s: %s" % (label, p))
            else:
                srcs.append((os.path.basename(p), p, label))
        if missing:
            messagebox.showerror("备份失败", "以下备份源不存在或未填写:\n\n" + "\n".join(missing))
            return
        dest = self.bk_dest_var.get().strip()
        if not dest:
            messagebox.showerror("备份失败", "未设置备份目标根目录")
            return
        if not os.path.isdir(dest):
            try:
                os.makedirs(dest, exist_ok=True)
                self._bk_log("已创建目标根目录: %s" % dest)
            except Exception as e:
                messagebox.showerror("备份失败", "无法创建目标根目录: %s" % e)
                return
        sub = "ZCode" + time.strftime("%Y%m%d%H")
        target = os.path.join(dest, sub)
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as e:
            messagebox.showerror("备份失败", "无法创建备份子目录: %s" % e)
            return
        self._bk_log("=" * 60)
        self._bk_log("备份时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        self._bk_log("备份目录: %s" % target)
        copied = []
        ok = True
        for base, src, label in srcs:
            try:
                shutil.copy2(src, os.path.join(target, base))
                copied.append("主文件: %s -> %s" % (src, os.path.join(target, base)))
                self._bk_log("[OK] %s: %s" % (label, src))
            except Exception as e:
                self._bk_log("[失败] %s: %s" % (label, e))
                ok = False
            if self.bk_include_wal.get():
                for ext in ("-wal", "-shm"):
                    wp = src + ext
                    if os.path.isfile(wp):
                        try:
                            shutil.copy2(wp, os.path.join(target, base + ext))
                            copied.append("WAL附属: %s -> %s" % (wp, os.path.join(target, base + ext)))
                            self._bk_log("[OK] 附属 %s: %s" % (ext, wp))
                        except Exception as e:
                            self._bk_log("[失败] 附属 %s: %s" % (ext, e))
                            ok = False
        # 写说明文档
        try:
            readme = self._bk_build_readme(srcs, copied, target)
            rp = os.path.join(target, "备份说明与恢复指南.md")
            with open(rp, "w", encoding="utf-8", newline="\n") as f:
                f.write(readme)
            self._bk_log("[OK] 已写入恢复说明: %s" % rp)
        except Exception as e:
            self._bk_log("[失败] 写入恢复说明: %s" % e)
            ok = False
        self._bk_log("-" * 60)
        if ok:
            msg = "备份完成! 目录: %s\n\n共复制 %d 个文件, 已写入 备份说明与恢复指南.md" % (target, len(copied))
            self.lbl_bk_status.config(text=msg, foreground="#0a7")
            self.set_status("备份完成: %s" % target)
            messagebox.showinfo("备份完成", msg)
        else:
            self.lbl_bk_status.config(text="备份完成但有失败项, 详见日志", foreground="#c33")
            self.set_status("备份部分失败")

    # ---------- 事件处理 ----------

    def _pick_dir(self, key):
        d = filedialog.askdirectory(title="选择目录")
        if d:
            self.path_vars[key].set(d)
            self._apply_paths()

    def _apply_paths(self):
        self.scanner.program_dir = self.path_vars["program"].get().strip()
        self.scanner.config_dir = self.path_vars["config"].get().strip()
        self.scanner.data_dir = self.path_vars["data"].get().strip()

    def on_auto_scan(self):
        self.scanner = ZCodeScanner()
        self.scanner.scan_default_paths()
        self.on_sync_paths()
        self._append_log(self.path_log, self.scanner.messages)
        self.set_status("默认路径扫描完成")

    def on_sync_paths(self):
        self.path_vars["program"].set(self.scanner.program_dir)
        self.path_vars["config"].set(self.scanner.config_dir)
        self.path_vars["data"].set(self.scanner.data_dir)
        self._apply_paths()

    def on_diagnose(self):
        self._apply_paths()
        self.scanner.messages = []
        if not self.scanner.config_dir and not self.scanner.data_dir:
            messagebox.showwarning("提示", "请先配置 用户配置目录 或 数据目录")
            return
        # 1. setting.json
        self.scanner.find_settings()
        if self.scanner.settings_ok and self.scanner.settings:
            self.lbl_setting.config(text="[正常] %s\n  dataBaseDir: %s\n  lastWorkspaceSession: %d 项, recentProjects: %d 项" % (
                self.scanner.settings_path,
                self.scanner.settings.get("dataBaseDir", "?"),
                len(self.scanner.settings.get("lastWorkspaceSession", [])),
                len(self.scanner.settings.get("recentProjects", []))))
        else:
            self.lbl_setting.config(text="[异常] %s" % self.scanner.settings_error, foreground="#c33")
        # 2. corrupt 文件
        self.scanner.find_corrupt_files()
        if self.scanner.corrupt_files:
            lines = []
            for fp, size, nz in self.scanner.corrupt_files:
                desc = "全零损坏(不可修复)" if nz == 0 else ("部分损坏(%d非零)" % nz if nz > 0 else "读取失败")
                lines.append("%s | %d 字节 | %s" % (fp, size, desc))
            self.lbl_corrupt.config(text="发现 %d 个损坏备份:\n%s" % (len(lines), "\n".join(lines)), foreground="#c33")
        else:
            self.lbl_corrupt.config(text="未发现 .corrupt 损坏备份")
        # 3. 数据库
        self.scanner.find_databases()
        if self.scanner.databases:
            lines = []
            for p, size, tasks, mtime in self.scanner.databases:
                tag = "  <- 活跃库" if (p == self.scanner.db_path or lines == []) else ""
                lines.append("%s | %d 条任务 | %.1f KB | %s%s" % (
                    p, tasks, size / 1024.0, mtime.strftime("%m-%d %H:%M"), tag))
            self.lbl_db.config(text="发现 %d 个数据库(按任务数排序):\n%s" % (len(lines), "\n".join(lines)))
            # 自动选择任务数最多的活跃库
            best = self.scanner.databases[0]
            self.scanner.analyze_db(best[0])
            self.lbl_db.config(text="选择活跃库: %s | %d 条任务\n\n发现 %d 个数据库:\n%s" % (
                best[0], best[2], len(lines), "\n".join(lines)))
        else:
            self.lbl_db.config(text="未找到 tasks-index.sqlite", foreground="#c33")
        # 日志
        self._append_log(self.diag_log, self.scanner.messages)
        self.set_status("诊断完成: %d 项目, %d 任务" % (len(self.scanner.projects), self.scanner.tasks_total))

    def on_refresh_projects(self):
        if not self.scanner.projects:
            if not self.scanner.db_path:
                messagebox.showinfo("提示", "请先执行 诊断分析")
                return
            self.scanner.analyze_db(self.scanner.db_path)
        self.tree_proj.delete(*self.tree_proj.get_children())
        ps = self.scanner.sort_projects(self.sort_mode.get())
        for i, p in enumerate(ps, 1):
            self.tree_proj.insert("", "end", iid=str(i - 1), values=(i, p["path"], p["count"], p["last_str"]))
        self.lbl_proj_summary.config(text="共 %d 个项目, %d 条任务" % (len(ps), self.scanner.tasks_total))
        self.set_status("项目列表已刷新")

    def on_project_select(self, _event):
        sel = self.tree_proj.selection()
        if not sel:
            return
        iid = int(sel[0])
        ps = self.scanner.sort_projects(self.sort_mode.get())
        if iid >= len(ps):
            return
        wp = ps[iid]["path"]
        self.tree_task.delete(*self.tree_task.get_children())
        cnt = 0
        for t in self.scanner.tasks:
            if t["workspace"] == wp:
                self.tree_task.insert("", "end",
                                      values=(t["workspace"], t["title"][:100], t["status"]))
                cnt += 1
                if cnt >= 200:
                    break

    def on_generate(self):
        if not self.scanner.projects:
            messagebox.showinfo("提示", "请先执行 诊断分析, 提取项目和任务")
            return
        self.fix_content = self.scanner.generate_fix(self.sort_mode.get())
        self._set_text(self.txt_preview, self.fix_content)
        self.lbl_fix_status.config(text="已生成, 共 %d 个项目加入 lastWorkspaceSession(workspacePurpose=project)" % len(self.scanner.projects))
        self.set_status("修复内容已生成")

    def on_export(self):
        if not self.fix_content:
            messagebox.showinfo("提示", "请先 生成修复内容")
            return
        default = os.path.join(os.path.expanduser("~"), "Desktop",
                               "setting.json.recovered-%s.json" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        path = filedialog.asksaveasfilename(
            title="导出修复文件", defaultextension=".json",
            initialfile=os.path.basename(default), initialdir=os.path.dirname(default))
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.fix_content)
            self.lbl_fix_status.config(text="已导出: %s" % path)
            messagebox.showinfo("完成", "修复文件已导出到:\n%s\n\n使用方法: 退出ZCode -> 备份原setting.json -> 用此文件替换" % path)
        except Exception as e:
            messagebox.showerror("错误", "导出失败: %s" % e)

    def on_deploy(self):
        if not self.fix_content:
            messagebox.showinfo("提示", "请先 生成修复内容")
            return
        target = self.scanner.settings_path
        if not target:
            messagebox.showerror("错误", "未定位到 setting.json, 无法部署")
            return
        if not messagebox.askyesno("确认部署",
                                   "将覆盖: %s\n\n部署前会自动备份原文件为 setting.json.backup-时间戳\n是否继续?" % target):
            return
        ok, msg = self.scanner.deploy(self.fix_content, backup=True)
        if ok:
            self.lbl_fix_status.config(text=msg, foreground="#0a7")
            messagebox.showinfo("完成", "%s\n\n请完全退出 ZCode(含托盘) 后重新打开, 查看项目列表是否恢复" % msg)
        else:
            self.lbl_fix_status.config(text=msg, foreground="#c33")
            messagebox.showerror("错误", msg)

    # ---------- 工具方法 ----------

    def set_status(self, text):
        self.status.config(text=text)

    @staticmethod
    def _append_log(text_widget, lines):
        text_widget.config(state="normal")
        for line in lines:
            text_widget.insert("end", line + "\n")
        text_widget.see("end")
        text_widget.config(state="disabled")

    @staticmethod
    def _set_text(text_widget, content):
        text_widget.config(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", content)
        text_widget.config(state="disabled")

    @staticmethod
    def _clear_text(text_widget):
        text_widget.config(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.config(state="disabled")


def main():
    _write_log("程序启动: %s v%s" % (APP_TITLE, APP_VERSION))
    try:
        root = tk.Tk()
        try:
            style = ttk.Style()
            try:
                style.theme_use("vista")
            except Exception:
                pass
        except Exception:
            pass
        app = ZCodeRepairApp(root)
        root.mainloop()
    except tk.TclError as e:
        _write_log("GUI启动失败(TclError): %s" % e)
        return 1
    except Exception as e:
        import traceback
        _write_log("main异常: %s" % traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.excepthook = _global_excepthook
    sys.exit(main())
