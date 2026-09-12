# 工厂同步控制台

局域网工厂机台控制与同步工具：网页控制端下发任务，机台 Agent 拉取执行。支持 Win7 / Win10，可网页推送自升级，也可打 NSIS 手工安装包。

> **只适合可信内网。** 控制端没有登录鉴权，Agent 以管理员运行，可搜磁盘、拷文件、执行命令。不要暴露到公网。

当前 Agent 版本：**2.0.7**。控制端默认监听 `0.0.0.0:5000`。

## 下载成品

打包好的安装包和升级包在 [GitHub Releases](https://github.com/sojiagu/factory-sync-console/releases/latest)，不必自己从源码构建。

| 文件 | 用途 |
| --- | --- |
| `factory-sync-console.zip` | 控制端，解压后运行 `工厂同步控制台.exe` |
| `agent_2.0.7.zip` | 网页升级包，上传到控制台后推送 |
| `agent_Setup_*.exe` | 机台手工安装包 |

校验和见 Release 里的 `SHA256SUMS.txt`。

---

## 它做什么

| 能力 | 说明 |
| --- | --- |
| 机台在线 | Agent 定时心跳，网页显示名称、IP、版本、在线状态 |
| 公盘同步 | 从 UNC 共享拷到机台本地，覆盖或增量（robocopy） |
| 远程命令 | 下发 CMD，带常用模板 |
| 文件搜索 | 多盘、多关键字。Win10 用 `mftparser` 读 `$MFT`，Win7 用纯 Python `mftpy` |
| 文件回传 | 把机台本地文件传到控制端（有大小上限） |
| 屏幕预览 | 打开监视后 Agent 回传截图 |
| 日志 | Agent 日志上传；控制端有操作审计 |
| 自升级 | 网页上传 zip，按版本号推送；机台校验 sha256 后就地替换 |
| 手工安装 | 用户权限装到 `%LOCALAPPDATA%\agent`，运行时再提权 |

---

## 架构

```
浏览器  ──►  控制端 web.py :5000  ──►  任务队列 / 升级包 / 截图 / 回传文件
                 ▲
                 │  拉任务、报进度、传结果
                 │
          机台 agent.exe（C 启动器）
            ├─ Win10 → _internal\win10\agent.exe   Python 3.11 + mftparser
            └─ Win7  → _internal\win7\agent.exe    Python 3.8  + mftpy
```

- 控制端：Flask + Waitress，托盘常驻，可打包成 onedir，在 Win2008 / Win7 上跑。
- Agent：PyInstaller onedir。根目录 `agent.exe` 是很小的 C 启动器（`requireAdministrator`），按系统选 Win10 或 Win7 运行时。
- 网页升级 zip 和手工安装包用**同一份**目录树：`dist\agent_setup\`。

机台编码格式：`字母数字-字母数字-数字`，例如 `T1-DL-0`。

---

## 仓库结构

```
web.py                      控制端
agent.py                    机台 Agent
mftpy.py                    Win7 纯 Python 读 NTFS $MFT
launcher/                   C 启动器（gcc + 管理员清单）
agent.spec / agent_win7.spec
工厂同步控制台.spec
assemble_agent_setup.py     拼成统一安装目录
make_agent_package.py       打网页升级 zip
build.bat / build_agent.bat
rthooks/pyi_rth_nozstd.py   禁止打进残缺 zstandard
Release/SoftSetupCore/      NSIS 2.46 手工安装封装
```

不进仓库的产物：`dist/`、`build/`、`packages/`、`Release/Output/`、`Release/SoftSetupCore/FilesToInstall/`、`device.ini`、运行日志。

---

## 依赖

### 运行环境

| 角色 | Python | 系统 | 说明 |
| --- | --- | --- | --- |
| 控制端 | **3.8 x64** | Win7 / Server 2008 R2 / 更新 | `build.bat` 固定 `py -3.8-64` |
| Agent Win10 | **3.11 x64** | Win10 / Win11 | 需要 `mftparser`（3.9+） |
| Agent Win7 | **3.8 x64** | Win7 | 不用 `mftparser`，走 `mftpy` |
| 启动器 | llvm-mingw / MinGW-w64 `gcc` | — | `-nostdlib`，只链 kernel32 / user32，Win7 能跑 |

Windows 上 `py -3.8-64` 应对应 64 位 Python 3.8.10。

### Python 包

控制端见 `requirements-console.txt`：

```text
flask<3.1
waitress
pystray
pillow<11
pyinstaller>=5.13,<6.12
```

Win10 Agent 见 `requirements-agent-win10.txt`：`requests`、`urllib3`、`pystray`、`pillow`、`pyinstaller`、`mftparser`。

Win7 Agent 见 `requirements-agent-win7.txt`：同上，但 `pillow<11`、`pyinstaller>=5.13,<6.12`，**不要装 mftparser**。

标准库会用到：`tkinter`、`ctypes`、`configparser`。启动器不依赖 Go。

### 手工安装包

- NSIS 2.46 Unicode（仓库 `Release/SoftSetupCore/NSIS`）
- `force_user_level.ps1`：打安装包前把 `RequestExecutionLevel` 改回 `user`（不要只点 `SoftwarePack.exe`，它会改成 admin）
- `setup.nsi` 是 **UTF-16 LE + BOM**，不要当普通 UTF-8 文本直接改

---

## 源码运行

先改地址。控制端：`web.py` 里的 `CONTROL_PUBLIC_HOST`、`SHARE_ROOT`、`SHARE_USER`、`SHARE_PASS`。Agent：`agent.py` 里的 `DEFAULT_SERVER`，或安装后改 `device.ini`。

```bat
py -3.8-64 -m pip install -r requirements-console.txt
py -3.8-64 web.py
```

浏览器打开 `http://本机IP:5000`。

```bat
py -3.11 -m pip install -r requirements-agent-win10.txt
python agent.py
```

Win7 机台用 Python 3.8，并安装 `requirements-agent-win7.txt`。

`device.ini` 只在本机生成，不要提交。示例字段：设备编码、控制端 URL、公盘账号。

---

## 打包

### 控制端

```bat
build.bat
```

产物：`dist\工厂同步控制台\`（含 `python38.dll`）。把整个目录拷到服务器即可。

### Agent（网页升级 + 手工安装同一份）

```bat
build_agent.bat
```

顺序：Win10 PyInstaller → Win7 PyInstaller → 编译 C 启动器 → `assemble_agent_setup.py` → `packages\agent_x.y.z.zip`。

把 zip 上传到控制台「Agent 升级」，再勾选机台推送。版本相同会跳过。

### 手工安装 exe

1. 把 `dist\agent_setup\` **整棵**拷到 `Release\SoftSetupCore\FilesToInstall\`（不要带 `device.ini`）
2. 在 `Release\SoftSetupCore` 执行：

```bat
makeapp.bat
makeskinzip.bat runtime
powershell -NoProfile -ExecutionPolicy Bypass -File force_user_level.ps1
NSIS\makensis.exe SetupScripts\runtime\setup.nsi
```

3. 安装包在 `Release\Output\`

安装本身是用户权限，默认 `%LOCALAPPDATA%\agent`。第一次运行会弹 UAC；通过后注册计划任务 `FactorySyncAgent`（登录、最高权限），再删掉启动文件夹里的 `agent.lnk`。之后开机只靠任务，不再弹 UAC。

---

## 权限与自启

| 环节 | 权限 |
| --- | --- |
| 手工安装 | 当前用户，不需要管理员 |
| 运行 Agent / 搜 `$MFT` | 必须管理员（启动器清单 `requireAdministrator`） |
| 网页升级 | 就地覆盖；`apply.bat` 继承当时 Agent 的提权 |
| 开机 | 任务 `FactorySyncAgent`：`ONLOGON` + `/RL HIGHEST` + 当前用户 |

安装程序默认勾「开机自启」，先写启动文件夹快捷方式（安装时还建不了任务）。Agent 以管理员跑起来并建好任务后，会清掉这个快捷方式。任务没建成时快捷方式会留着，避免自启断掉。不要两套同时长期开着，否则登录时会各拉一份、互相 `taskkill`。

卸载会删除该计划任务。

---

## 网页升级注意

- zip 必须是统一包：`agent.exe` + `_internal\win10` + `_internal\win7`
- Win10 机台只解压本机运行时，不解压 Win7 目录
- 控制端拒收带 `mftscan.exe` 的包（已改为 `mftpy`，不再用 Go 扫描器）
- 自升级不用 PowerShell（避免 360 拦截），进度用 `curl.exe` 回传
- 升级时若旧 Agent 已是管理员，没有计划任务的机台会自动补建

---

## 配置项（改现场时）

| 位置 | 变量 | 含义 |
| --- | --- | --- |
| `web.py` | `SERVER_PORT` | 默认 5000 |
| `web.py` | `CONTROL_PUBLIC_HOST` | Agent 下载升级包、回连用的控制端 IP |
| `web.py` | `SHARE_ROOT` / `SHARE_USER` / `SHARE_PASS` | 公盘浏览与同步 |
| `agent.py` | `AGENT_VERSION` | 与升级包版本一致才推送 |
| `agent.py` | `DEFAULT_SERVER` | 未写 `device.ini` 时的控制端 |

仓库里的 IP、共享账号是原厂局域网默认值，换环境请改掉。

---

## 许可证

MIT。见 [LICENSE](LICENSE)。

`Release/SoftSetupCore` 里的 NSIS、皮肤插件、7-Zip 等是第三方工具，按各自原许可证使用。
