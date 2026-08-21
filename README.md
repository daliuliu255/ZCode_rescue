# ZCode_rescue
This is a Zcode client tool for restoring context, project task data, and custom model entries


# 创建时间: 2026-08-06 03:41

# ZCode 修复工具 使用说明

## 不欢迎任何Linux.do论坛的用户使用，论坛用户的存在就是助长垃圾管理和垃圾举报人员的气焰！
## Linux.do的喜欢封号和禁言的傻逼，给老子去死！


## 功能概述

ZCode 修复工具用于解决 ZCode 因 `setting.json` 被全零字节(0x00)填充损坏，导致左侧项目/任务列表丢失的问题。工具会从 `tasks-index.sqlite` 提取全部项目和任务，自动生成修复版 `setting.json`，恢复项目列表显示。

工具基于 2026-08-05 实际发生的 ZCode 配置丢失事故和成功恢复过程开发。

## 运行方式

**推荐: 双击 `start_tool.bat` 启动**（自动选择 Python，无控制台窗口）

```bash
# 方式1: 双击 start_tool.bat (推荐)
# 自动使用 pythonw.exe 静默启动, 无黑框

# 方式2: 双击 start_tool_debug.bat (排错用)
# 带控制台窗口, 报错信息可见, 退出前 pause 停留

# 方式3: 命令行运行
python zcode_repair_tool.py
```

推荐 Python 3.10 (系统) 或 3.13 (如同时使用WorkBuddy，会存在这个版本的托管)：
```
C:\Users\DX\.workbuddy\binaries\python\versions\3.13.12\python.exe zcode_repair_tool.py
```

⚠️ 若界面打不开：查看本目录下的 `error.log` 文件（工具会自动写入启动错误详情）。

## 使用流程

### 第一步：路径配置（标签页1）

启动后自动扫描默认路径。也可手动：
- **自动扫描默认路径**：自动探测 `~/.zcode`、从配置读取 dataBaseDir、探测程序目录
- **手动选择**：点"选择..."按钮分别指定三个目录

| 目录 | 说明 |
|---|---|
| 程序安装目录(可选) | ZCode.exe 所在目录，仅用于参考 |
| 用户配置目录 | `C:\Users\DX\.zcode`，存放 setting.json |
| 数据目录 | setting.json 中 dataBaseDir 指向的目录（如 `K:\编程区\@@moxing_Data`） |

### 第二步：诊断分析（标签页2）

点"开始诊断"，工具自动：
1. 检查 setting.json 状态（正常 / 全零损坏 / 解析失败）
2. 查找 `.corrupt` 损坏备份并分析字节（全零判定）
3. 发现所有 tasks-index.sqlite（自动选任务数最多的活跃库）
4. 提取全部项目和任务

### 第三步：项目任务列表（标签页3）

- 4种排序：最近活跃 / 任务数 / 项目名称 / 磁盘分组
- 点项目可查看该项目下的任务（最多200条）
- 支持排序切换实时刷新

### 第四步：修复部署（标签页4）

1. 选择排序方式，点"生成修复内容"，预览修复 JSON
2. **导出修复文件**：保存副本到任意位置（不覆盖原文件），适合先备份
3. **备份并部署**：自动备份原 setting.json 为 `setting.json.backup-时间戳`，然后写入修复版

⚠️ 部署后必须：完全退出 ZCode（含托盘）→ 重新打开 → 查看项目列表是否恢复。

## 修复原理

ZCode 左侧项目列表 = `lastWorkspaceSession` 中 `workspacePurpose == "project"` 的条目。
setting.json 损坏重建后该字段被重置，导致项目列表丢失。

工具生成修复版时：
- 从 tasks-index.sqlite 提取全部 `workspace_path`（去重）
- 以 `{"kind":"local","workspacePath":"...","workspacePurpose":"project"}` 重建 lastWorkspaceSession
- 保留原有 conversation 类型的会话条目
- 同步更新 recentProjects 数组

## 安全说明

- 数据库读取全部使用 SQLite 只读模式（`?mode=ro`），不写入数据目录
- "导出修复文件" 不触碰原配置
- "备份并部署" 会先备份原文件再写入
- 工具本身不删除任何文件

## 故障排查

| 问题 | 处理 |
|---|---|
| 提示"未找到 setting.json" | 检查 用户配置目录 是否指向 ~/.zcode，数据目录是否正确 |
| 诊断显示全零损坏 | 属预期场景，直接生成修复 |
| 未找到数据库 | 检查 数据目录 是否含 `.zcode\v2\tasks-index.sqlite` |
| 部署后项目仍未恢复 | 确认完全退出 ZCode 含托盘进程，重启后再看 |
| GUI 无法启动 | 确认 Python 带 tkinter（`python -c "import tkinter"`） |

## 版本

- 当前版本: 1.0.0
- 文件: `zcode_repair_tool.py`（单文件，仅标准库依赖）

---

# V2 版本说明（zcode_repair_tool_V2.py）

## 与 V1 的关系

V2 继承 V1 全部功能（路径配置/诊断分析/项目任务/修复部署），新增第 5 个标签页 **"5. 备份数据库和模型列表"**。

## 新增功能：备份数据库和模型列表

### 备份内容（默认，均可自由修改/选择）

| 文件 | 默认来源路径 | 内容 |
|---|---|---|
| 会话上下文库 | `C:\Users\DX\.zcode\cli\db\db.sqlite` | 全部对话上下文（session/message/part） |
| 任务索引库 | `{ZCode数据存储路径}\.zcode\v2\tasks-index.sqlite` | 全部任务与项目分组 |
| 模型配置 | `{ZCode数据存储路径}.zcode\v2\config.json` | 模型 provider 列表/自定义 apiKey/模型清单 |

默认连带备份 tasks-index.sqlite 的 `-wal/-shm`（WAL 可能含未合并数据，仅备份主文件会丢数据，推荐勾选）。

### 备份目标

- 默认根目录：`K:\编程区\@@大模型备份`（可编辑/浏览选择）
- 自动创建子文件夹：`ZCode+年月日时`，如 `ZCode2026081716`
- 备份完成后自动写入 `备份说明与恢复指南.md`（含备份来源、文件用途、恢复步骤）

### 启动方式

```bash
# 方式1: 双击 start_tool_v2.bat (推荐, 优先系统Python310)
# 方式2: 命令行
D:\Soft\Python310\python.exe zcode_repair_tool_V2.py
```

⚠️ 注意：V2 需要带 tkinter 的 Python。系统 Python 3.10（`D:\Soft\Python310`）已确认可用；WorkBuddy 托管 3.13 无 tkinter，双击 `start_tool_v2.bat` 会优先使用系统 3.10。

### 备份操作步骤

1. 打开"5. 备份数据库和模型列表"页
2. 确认 3 个备份源路径（默认已填，可改）
3. 确认目标根目录（默认 `K:\编程区\@@大模型备份`），预览自动文件夹名
4. 点"开始备份"
5. 备份完成后查看日志 + 自动生成的 `备份说明与恢复指南.md`

### 恢复方法（简述）

1. 完全退出 ZCode（含托盘进程）
2. 用备份目录中文件覆盖对应原始路径（见说明文档中的来源对照表）
3. 重启 ZCode 验证

### 版本

- V2 版本号: 2.0.0
- 文件: `zcode_repair_tool_V2.py`（单文件，仅标准库依赖）
