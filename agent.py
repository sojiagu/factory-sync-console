import tkinter as tk
from tkinter import messagebox
import os
import sys
import ctypes
from ctypes import wintypes
import time
import threading
import json
import uuid
import socket
import hashlib
import zipfile
import shutil
import configparser
import subprocess
import re
from urllib.parse import urlparse, quote

# urllib3 会探测 zstandard；PyInstaller 经常打进残缺模块，一读 __version__ 就崩。
# 局域网 HTTP 不需要 zstd，直接禁用。
import sys as _sys

class _BlockZstandard:
    def find_spec(self, fullname, path, target=None):
        if fullname == "zstandard" or fullname.startswith("zstandard."):
            raise ImportError("zstandard disabled")
        return None

_sys.meta_path.insert(0, _BlockZstandard())
_sys.modules.pop("zstandard", None)

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import pystray
from PIL import Image, ImageDraw

# ===============================
# 版本与默认配置
# ===============================
AGENT_VERSION = "2.0.7"
DEFAULT_SERVER = "http://192.168.36.248:5000"
DEFAULT_MACHINE_NAME = "T1-DL-0"
MACHINE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z][A-Za-z0-9]*-[0-9]+$")
REPORT_ENDPOINT = "/report"
POLL_INTERVAL = 5
DEFAULT_SHARE_USER = "1"
DEFAULT_SHARE_PASS = "1"
MUTEX_NAME = "MyAgentMutexName"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
os.makedirs(BASE_DIR, exist_ok=True)

DEVICE_INI = os.path.join(BASE_DIR, "device.ini")
LOG_DIR = os.path.join(BASE_DIR, "log")
LOG_KEEP_DAYS = 30
PENDING_FILE = os.path.join(BASE_DIR, "update_pending.json")
FAIL_FLAG = os.path.join(BASE_DIR, "update_fail.flag")
OK_FLAG = os.path.join(BASE_DIR, "update_ok.flag")


def resource_path(name):
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(sys.executable), name))
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            cands.append(os.path.join(meipass, name))
        cands.append(os.path.join(BASE_DIR, name))
        cands.append(os.path.join(BASE_DIR, "_internal", name))
    else:
        cands.append(os.path.join(BASE_DIR, name))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return cands[0] if cands else name


def icon_file_path():
    path = resource_path("icon.ico")
    return path if path and os.path.isfile(path) else ""


def icon_path_for_tk():
    path = icon_file_path()
    if not path:
        return ""
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    buf = ctypes.create_unicode_buffer(520)
    try:
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 520)
        if n and buf.value:
            try:
                buf.value.encode("ascii")
                return buf.value
            except UnicodeEncodeError:
                pass
    except Exception:
        pass
    dest = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".", "agent_icon.ico")
    try:
        shutil.copy2(path, dest)
        return dest
    except Exception:
        return path


def apply_window_icon(win):
    path = icon_path_for_tk()
    if not path or win is None:
        return
    try:
        win.iconbitmap(default=path)
    except Exception:
        pass
    try:
        win.iconbitmap(path)
    except Exception:
        pass


def show_popup(kind, title, text):
    try:
        root = tk.Tk()
        root.withdraw()
        apply_window_icon(root)
        root.title(title)
        if kind == "error":
            messagebox.showerror(title, text, parent=root)
        elif kind == "warning":
            messagebox.showwarning(title, text, parent=root)
        else:
            messagebox.showinfo(title, text, parent=root)
        try:
            root.destroy()
        except Exception:
            pass
    except Exception:
        try:
            flag = 0x10 if kind == "error" else 0x40
            ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flag)
        except Exception:
            pass

session = requests.Session()
retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[502, 503, 504])
session.mount("http://", HTTPAdapter(max_retries=retries))
session.mount("https://", HTTPAdapter(max_retries=retries))
# 进度/结果回传单独会话，避免和拉任务互相堵，也不做重试把几秒复制拖成几十秒
report_session = requests.Session()
_progress_post_lock = threading.Lock()
_progress_posting = False

_busy = threading.Event()
_cancel_event = threading.Event()
_current_task_id = ""
_pending_cancel_id = ""
_current_proc = None
_current_proc_lock = threading.Lock()
_log_lock = threading.Lock()
_last_log_cleanup = 0


def cleanup_old_logs(keep_days=LOG_KEEP_DAYS):
    global _last_log_cleanup
    now = time.time()
    if now - _last_log_cleanup < 3600 and _last_log_cleanup:
        return
    _last_log_cleanup = now
    try:
        if not os.path.isdir(LOG_DIR):
            return
        cutoff = now - max(1, int(keep_days)) * 86400
        for name in os.listdir(LOG_DIR):
            if not (name.startswith("agent_") and name.endswith(".txt")):
                continue
            path = os.path.join(LOG_DIR, name)
            try:
                day = name[6:14]
                ts = time.mktime(time.strptime(day, "%Y%m%d"))
            except Exception:
                try:
                    ts = os.path.getmtime(path)
                except Exception:
                    continue
            if ts < cutoff:
                try:
                    os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass


def list_log_dates():
    dates = []
    try:
        if not os.path.isdir(LOG_DIR):
            return dates
        for name in os.listdir(LOG_DIR):
            if name.startswith("agent_") and name.endswith(".txt") and len(name) >= 18:
                day = name[6:14]
                if day.isdigit():
                    dates.append(day)
    except Exception:
        pass
    dates.sort(reverse=True)
    return dates


def read_log_day(day, max_bytes=1200 * 1024):
    path = os.path.join(LOG_DIR, "agent_%s.txt" % day)
    if not os.path.isfile(path):
        return "", False
    try:
        size = os.path.getsize(path)
        with _log_lock:
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(-max_bytes, os.SEEK_END)
                    raw = f.read()
                    truncated = True
                else:
                    raw = f.read()
                    truncated = False
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1:]
            text = "…（仅显示末尾约 %s）\n" % format_size(max_bytes) + text
        return text, truncated
    except Exception as e:
        return "读取日志失败: %s\n" % e, False


def log_local(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        cleanup_old_logs()
        path = os.path.join(LOG_DIR, "agent_%s.txt" % time.strftime("%Y%m%d"))
        line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
        with _log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        print(line.strip())
    except Exception:
        pass


def task_cancelled(task_id=None):
    if task_id and _pending_cancel_id and str(task_id) == str(_pending_cancel_id):
        return True
    if not _cancel_event.is_set():
        return False
    if task_id and _current_task_id and str(task_id) != str(_current_task_id):
        return False
    return True


def request_task_cancel(task_id):
    global _current_proc, _pending_cancel_id
    tid = str(task_id or "").strip()
    if not tid:
        return
    _pending_cancel_id = tid
    if _current_task_id and str(_current_task_id) != tid:
        return
    if _current_task_id:
        _cancel_event.set()
        with _current_proc_lock:
            proc = _current_proc
        if proc is not None:
            kill_proc(proc)


def kill_proc(proc):
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass
    try:
        if proc.poll() is None:
            subprocess.run(
                "taskkill /F /T /PID %d" % proc.pid,
                shell=True,
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def report_cancelled(task_id, detail="任务已取消"):
    report_result(task_id, "cancelled", detail)


def format_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return "%dB" % int(n)
    return "%.1f%s" % (n, units[i])


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def install_dir_writable(path):
    path = os.path.abspath(path or "")
    if not path or not os.path.isdir(path):
        return False
    probe = os.path.join(path, ".agent_write_test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        try:
            if os.path.isfile(probe):
                os.remove(probe)
        except Exception:
            pass
        return False


# ===============================
# 读取/保存配置
# ===============================
def load_ini():
    cfg = configparser.ConfigParser()
    if os.path.exists(DEVICE_INI):
        try:
            cfg.read(DEVICE_INI, encoding="utf-8")
        except Exception:
            try:
                cfg.read(DEVICE_INI)
            except Exception:
                pass
    return cfg


def save_ini(cfg):
    with open(DEVICE_INI, "w", encoding="utf-8") as f:
        cfg.write(f)


def ini_get(cfg, section, key, default=""):
    try:
        if cfg.has_option(section, key):
            return cfg.get(section, key)
    except Exception:
        pass
    return default


def valid_machine_name(name):
    name = (name or "").strip()
    return bool(name) and name.upper() != "UNKNOWN" and bool(MACHINE_NAME_RE.match(name))


def ask_machine_name(prev="", err=""):
    root = tk.Tk()
    root.withdraw()
    apply_window_icon(root)
    result = {"val": None, "cancelled": True}

    def ask_name():
        d = tk.Toplevel(root)
        d.title("设备码定义")
        apply_window_icon(d)
        d.geometry("440x240")
        d.resizable(False, False)
        d.update_idletasks()
        x = (d.winfo_screenwidth() - 440) // 2
        y = (d.winfo_screenheight() - 240) // 2
        d.geometry("+%d+%d" % (x, y))
        d.grab_set()
        tk.Label(d, text="请填写本机标识（线体-站位-编号）").pack(pady=(12, 2))
        tk.Label(d, text="例如 T1-DL-01，不能与控制端已在线机台重复", fg="#64748b").pack()
        var = tk.StringVar(value=(prev or "").strip() or DEFAULT_MACHINE_NAME)
        entry = tk.Entry(d, textvariable=var, width=36)
        entry.pack(pady=6)
        entry.focus_set()
        try:
            entry.icursor(tk.END)
        except Exception:
            pass
        err_var = tk.StringVar(value=err or "")
        tk.Label(d, textvariable=err_var, fg="#b91c1c", wraplength=400).pack(pady=4)

        def on_ok():
            val = (var.get() or "").strip()
            if not val:
                err_var.set("必须填写机台名，格式：线体-站位-编号，例如 T1-DL-01")
                return
            if val.upper() == "UNKNOWN":
                err_var.set("不能使用 UNKNOWN，请按 线体-站位-编号 填写，例如 T1-DL-01")
                return
            if not valid_machine_name(val):
                err_var.set("格式必须是 线体-站位-编号，例如 T1-DL-01")
                return
            result["val"] = val
            result["cancelled"] = False
            d.destroy()

        def on_close():
            result["cancelled"] = True
            result["val"] = None
            d.destroy()

        tk.Button(d, text="确定", command=on_ok, width=10).pack(pady=8)
        d.protocol("WM_DELETE_WINDOW", on_close)
        entry.bind("<Return>", lambda e: on_ok())
        d.wait_window()

    ask_name()
    try:
        root.destroy()
    except Exception:
        pass
    return result


def claim_machine_name(server, name, agent_id, ip="", force=False):
    try:
        url = "%s/agent/claim?machine=%s&agent_id=%s&ip=%s" % (
            server.rstrip("/"),
            quote(name),
            quote(agent_id or ""),
            quote(ip or ""),
        )
        if force:
            url += "&force=1"
        resp = session.get(url, timeout=8)
        data = {}
        try:
            data = resp.json() or {}
        except Exception:
            data = {}
        if resp.status_code == 200 and data.get("ok"):
            return True, "", (data.get("machine") or name)
        err = data.get("error") or ("控制端拒绝登记 HTTP %s" % resp.status_code)
        return False, err, name
    except Exception as e:
        return False, "无法连接控制端 %s（%s）" % (server, e), name


def get_local_ip(server=None):
    raw = (server or "").strip()
    if not raw:
        try:
            raw = CONTROL_SERVER
        except NameError:
            raw = DEFAULT_SERVER
    host = urlparse(raw).hostname or "192.168.36.248"
    port = urlparse(raw).port or 80
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect((host, port))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass
    return "unknown"


def migrate_legacy_device_ini():
    if os.path.isfile(DEVICE_INI):
        return
    old = os.path.join(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)", "agent", "device.ini")
    if not os.path.isfile(old):
        return
    try:
        shutil.copy2(old, DEVICE_INI)
        log_local("已从旧 Program Files 安装复制 device.ini")
    except Exception as e:
        log_local("复制旧 device.ini 失败: %s" % e)


def load_settings():
    migrate_legacy_device_ini()
    cfg = load_ini()
    if "device" not in cfg:
        cfg["device"] = {}
    if "server" not in cfg:
        cfg["server"] = {}
    if "share" not in cfg:
        cfg["share"] = {}
    if "sync" not in cfg:
        cfg["sync"] = {}

    server = ini_get(cfg, "server", "url", "").strip() or DEFAULT_SERVER
    cfg["server"]["url"] = server
    user = ini_get(cfg, "share", "user", "").strip() or DEFAULT_SHARE_USER
    password = ini_get(cfg, "share", "pass", "").strip() or DEFAULT_SHARE_PASS
    cfg["share"]["user"] = user
    cfg["share"]["pass"] = password
    if cfg.has_section("sync"):
        cfg.set("sync", "engine", "native")
        if cfg.has_option("sync", "mt"):
            cfg.remove_option("sync", "mt")
        if cfg.has_option("sync", "auto"):
            cfg.remove_option("sync", "auto")

    agent_id = ini_get(cfg, "device", "id", "").strip()
    if not agent_id:
        agent_id = str(uuid.uuid4())
        cfg["device"]["id"] = agent_id

    name = ini_get(cfg, "device", "name", "").strip()
    local_ip = get_local_ip(server)
    err = ""
    adopted = False
    if agent_id and name:
        ok, err, claimed = claim_machine_name(server, name, agent_id, local_ip, force=False)
        if ok and valid_machine_name((claimed or "").strip()):
            name = claimed.strip()
            adopted = True
    while not adopted:
        if not valid_machine_name(name):
            asked = ask_machine_name(
                prev=name if name and name.upper() != "UNKNOWN" else DEFAULT_MACHINE_NAME,
                err=err or ("格式必须是 线体-站位-编号，例如 T1-DL-01" if name else ""),
            )
            if asked.get("cancelled") or not asked.get("val"):
                show_popup("error", "提示", "必须填写机台名才能启动，程序将退出。")
                sys.exit(1)
            name = asked["val"]
        ok = False
        claimed = name
        for attempt in range(4):
            ok, err, claimed = claim_machine_name(server, name, agent_id, local_ip, force=True)
            if ok:
                name = claimed or name
                break
            if "已在线" in (err or "") and attempt < 3:
                time.sleep(2)
                continue
            break
        if ok:
            break
        asked = ask_machine_name(prev=name, err=err)
        if asked.get("cancelled") or not asked.get("val"):
            show_popup("error", "提示", "机台名未确认，程序将退出。")
            sys.exit(1)
        name = asked["val"]

    cfg["device"]["name"] = name
    cfg["device"]["id"] = agent_id

    try:
        save_ini(cfg)
    except Exception as e:
        log_local("保存 device.ini 失败: %s" % e)
    return {
        "name": name,
        "agent_id": agent_id,
        "server": server.rstrip("/"),
        "share_user": user,
        "share_pass": password,
    }


def stop_other_agent_instances():
    """结束其它 agent.exe（例如仍在 Program Files 里的旧副本），再抢互斥锁。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        subprocess.run(
            'taskkill /F /FI "IMAGENAME eq agent.exe" /FI "PID ne %d"' % os.getpid(),
            shell=True,
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=8,
        )
    except Exception:
        pass
    time.sleep(0.6)
    lnk = os.path.join(
        os.environ.get("ProgramData") or r"C:\ProgramData",
        r"Microsoft\Windows\Start Menu\Programs\StartUp\agent.lnk",
    )
    try:
        if os.path.isfile(lnk):
            os.remove(lnk)
    except Exception:
        pass


def check_single_instance():
    stop_other_agent_instances()
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.GetLastError() == 183:
        show_popup("info", "Agent", "Agent 已在运行。")
        print("已有客户端在运行，退出。")
        sys.exit(0)


check_single_instance()
SETTINGS = load_settings()
MACHINE_NAME = SETTINGS["name"]
AGENT_ID = SETTINGS["agent_id"]
CONTROL_SERVER = SETTINGS["server"]
SHARE_USER = SETTINGS["share_user"]
SHARE_PASS = SETTINGS["share_pass"]


LOCAL_IP = get_local_ip()


# ===============================
# 回传结果
# ===============================
def report_result(task_id, status, message, progress=None, extra=None):
    global _progress_posting
    data = {
        "task_id": task_id,
        "machine": MACHINE_NAME,
        "status": status,
        "message": message,
        "ip": LOCAL_IP,
        "version": AGENT_VERSION,
    }
    if progress is not None:
        data["progress"] = progress
    if extra:
        data.update(extra)
    if status == "progress":
        with _progress_post_lock:
            if _progress_posting:
                return
            _progress_posting = True
        try:
            report_session.post(CONTROL_SERVER + REPORT_ENDPOINT, json=data, timeout=1.5)
        except Exception as e:
            log_local("回传失败: %s" % e)
        finally:
            with _progress_post_lock:
                _progress_posting = False
        return
    try:
        report_session.post(CONTROL_SERVER + REPORT_ENDPOINT, json=data, timeout=5)
    except Exception as e:
        log_local("回传失败: %s" % e)


# ===============================
# 网络共享
# ===============================
def unc_share_root(path):
    p = (path or "").replace("/", "\\")
    if p.startswith("\\\\"):
        parts = [x for x in p.split("\\") if x]
        if len(parts) >= 2:
            return "\\\\" + parts[0] + "\\" + parts[1]
    return path


def normalize_win_path(p):
    s = (p or "").strip().strip('"').strip("'")
    if not s:
        return ""
    s = s.replace("/", "\\")
    if s.startswith("\\\\"):
        rest = s[2:]
        while "\\\\" in rest:
            rest = rest.replace("\\\\", "\\")
        return "\\\\" + rest.rstrip("\\")
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    s = s.rstrip("\\")
    if len(s) == 2 and s[1] == ":":
        return s + "\\"
    try:
        s = os.path.normpath(s)
    except Exception:
        pass
    return s


def looks_like_filename(name):
    return bool(name and re.search(r"\.[A-Za-z0-9]{1,8}$", name))


def path_kind(path):
    try:
        if os.path.isfile(path):
            return "file"
        if os.path.isdir(path):
            return "dir"
    except Exception:
        pass
    return ""


def dest_marked_as_dir(raw):
    s = (raw or "").strip().strip('"').strip("'")
    return s.endswith("\\") or s.endswith("/")


def ensure_dir(path, task_id, label="目标"):
    if path_kind(path) == "file":
        report_result(task_id, "error", "%s路径已存在且是文件，无法作为文件夹: %s" % (label, path))
        return False
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception as e:
        report_result(task_id, "error", "无法创建%s目录 %s: %s" % (label, path, e))
        return False


def resolve_copy_target(src, dst_raw, task_id):
    """判定源是文件还是文件夹，并算出真实目标目录/文件名。失败时已回传 error。"""
    src = normalize_win_path(src)
    marked_dir = dest_marked_as_dir(dst_raw)
    dst = normalize_win_path(dst_raw)
    if not src:
        report_result(task_id, "error", "源路径为空")
        return None
    if not dst:
        report_result(task_id, "error", "目标路径为空")
        return None
    kind = path_kind(src)
    if kind == "file":
        src_name = os.path.basename(src)
        src_dir = os.path.dirname(src)
        if not src_name or not src_dir:
            report_result(task_id, "error", "源文件路径无效: %s" % src)
            return None
        dst_kind = path_kind(dst)
        if dst_kind == "dir" or marked_dir:
            dest_dir, dest_name = dst, src_name
        elif dst_kind == "file":
            dest_dir, dest_name = os.path.dirname(dst), os.path.basename(dst)
        else:
            leaf = os.path.basename(dst)
            parent = os.path.dirname(dst)
            if leaf and (leaf.lower() == src_name.lower() or looks_like_filename(leaf)):
                dest_dir, dest_name = parent, leaf
            else:
                dest_dir, dest_name = dst, src_name
        if not dest_dir or not dest_name:
            report_result(task_id, "error", "目标路径无效: %s" % dst)
            return None
        return {
            "kind": "file",
            "src": src,
            "src_dir": src_dir,
            "src_name": src_name,
            "dest_dir": dest_dir,
            "dest_name": dest_name,
            "dest_file": os.path.join(dest_dir, dest_name),
        }
    if kind == "dir":
        if path_kind(dst) == "file":
            report_result(task_id, "error", "目标路径是文件，不能复制文件夹: %s" % dst)
            return None
        return {
            "kind": "dir",
            "src": src,
            "dest_dir": dst,
        }
    report_result(task_id, "error", "源路径不存在（不是文件也不是文件夹）: %s" % src)
    return None


def connect_share(share_path, task_id):
    share_path = normalize_win_path(share_path)
    try:
        if path_kind(share_path):
            return True
        if not share_path.startswith("\\\\"):
            return True
        root = unc_share_root(share_path)
        cmd = 'net use "%s" /user:%s %s /persistent:no' % (root or share_path, SHARE_USER, SHARE_PASS)
        try:
            subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=25,
            )
        except subprocess.TimeoutExpired:
            report_result(task_id, "error", "连接共享超时(25秒): %s" % (root or share_path))
            return False
        if path_kind(share_path):
            return True
        if root and os.path.isdir(root):
            return True
        report_result(task_id, "error", "连接共享失败或路径不可访问: %s" % share_path)
        return False
    except Exception as e:
        report_result(task_id, "error", "连接共享失败: %s" % e)
        return False


# ===============================
# 复制：robocopy 优先，失败/卡住再分块读写兜底
# ===============================
def file_size(path):
    try:
        if path and os.path.isfile(path):
            return os.path.getsize(path)
    except Exception:
        pass
    return -1


def dest_file_complete(dst, src_sz):
    sz = file_size(dst)
    if sz < 0:
        return False
    if src_sz > 0:
        return sz >= src_sz
    return os.path.isfile(dst)


def dest_tree_bytes(path):
    total = 0
    nfiles = 0
    try:
        for root, dirs, files in os.walk(path):
            for name in files:
                p = os.path.join(root, name)
                try:
                    total += os.path.getsize(p)
                    nfiles += 1
                except Exception:
                    pass
    except Exception:
        pass
    return total, nfiles


class NativeCopyProgress:
    def __init__(self, task_id):
        self.task_id = task_id
        self.copied_files = 0
        self.file_done = 0
        self.file_total = 0
        self.current_name = ""
        self.confirmed = False
        self.stop = False
        self.t0 = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def elapsed(self):
        return max(0.0, time.time() - self.t0)

    def extra(self):
        data = {
            "copied_bytes": max(0, int(self.file_done or 0)),
            "total_bytes": max(0, int(self.file_total or 0)),
            "elapsed_sec": round(self.elapsed(), 1),
        }
        if self.copied_files:
            data["copied_files"] = self.copied_files
        return data

    def _loop(self):
        last = None
        last_force = 0.0
        while not self.stop:
            sig = (self.copied_files, self.file_done, self.file_total, self.current_name, self.confirmed)
            now = time.time()
            if sig != last or (now - last_force) >= 1.0:
                last = sig
                last_force = now
                self._report()
            time.sleep(0.3)

    def _report(self):
        name = self.current_name or "文件"
        done = max(0, int(self.file_done or 0))
        total = max(0, int(self.file_total or 0))
        if total > 0:
            frac = min(1.0, float(done) / float(total))
            msg = "复制中 %s / %s" % (format_size(done), format_size(total))
            if self.copied_files:
                msg = "已处理 %d 个，当前 %s %s" % (self.copied_files, name, msg)
            pct = 100 if self.confirmed else min(88, 8 + int(80.0 * frac))
        elif self.copied_files:
            msg = "复制中，已处理 %d 个文件" % self.copied_files
            pct = min(88, 8 + min(70, self.copied_files))
        else:
            msg = "正在复制 %s" % name
            pct = 8
        report_result(self.task_id, "progress", msg, pct, extra=self.extra())

    def close(self):
        self.stop = True


def robocopy_path(p):
    p = normalize_win_path(p)
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    return p


def robocopy_exe():
    exe = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "robocopy.exe")
    if os.path.isfile(exe):
        return exe
    return "robocopy"


_mt_cache = {}


def dest_drive_letter(path):
    p = normalize_win_path(path)
    if len(p) >= 2 and p[1] == ":":
        return p[0].upper()
    return ""


def ram_gb():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys / (1024.0 ** 3)
    except Exception:
        pass
    return 0.0


def drive_is_hdd(path):
    """True=机械盘，False=SSD，None=未知。"""
    letter = dest_drive_letter(path)
    if not letter:
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW("\\\\.\\%s:" % letter, 0, 3, None, 3, 0, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (-1, invalid, 0):
        return None

    class STORAGE_PROPERTY_QUERY(ctypes.Structure):
        _fields_ = [
            ("PropertyId", ctypes.c_int),
            ("QueryType", ctypes.c_int),
            ("AdditionalParameters", ctypes.c_byte * 1),
        ]

    class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("Version", ctypes.c_ulong),
            ("Size", ctypes.c_ulong),
            ("IncursSeekPenalty", ctypes.c_ubyte),
        ]

    query = STORAGE_PROPERTY_QUERY()
    query.PropertyId = 7
    query.QueryType = 0
    out = DEVICE_SEEK_PENALTY_DESCRIPTOR()
    returned = ctypes.c_ulong(0)
    ok = False
    try:
        ok = bool(kernel32.DeviceIoControl(
            handle, 0x002D1400,
            ctypes.byref(query), ctypes.sizeof(query),
            ctypes.byref(out), ctypes.sizeof(out),
            ctypes.byref(returned), None,
        ))
    except Exception:
        ok = False
    try:
        kernel32.CloseHandle(handle)
    except Exception:
        pass
    if ok:
        return bool(out.IncursSeekPenalty)
    return None


def choose_robocopy_mt(dst):
    """按本机 CPU/内存/磁盘自动选 /MT，范围 2～32。不与 /IPG 同时用。"""
    letter = dest_drive_letter(dst) or "*"
    cached = _mt_cache.get(letter)
    if cached:
        return cached
    cpus = os.cpu_count() or 2
    ram = ram_gb()
    hdd = drive_is_hdd(dst)
    unc = normalize_win_path(dst).startswith("\\\\")
    if hdd is True:
        mt = 2 if cpus <= 2 else 4
        disk = "hdd"
    elif hdd is False:
        if cpus >= 12:
            mt = 24
        elif cpus >= 8:
            mt = 16
        elif cpus >= 4:
            mt = 12
        else:
            mt = 8
        disk = "ssd"
    else:
        mt = 8 if cpus >= 4 else 4
        disk = "unknown"
    if ram and ram < 4:
        mt = min(mt, 4)
    elif ram and ram < 8:
        mt = min(mt, 8)
    if unc:
        mt = min(mt, 8)
    mt = max(2, min(32, int(mt)))
    _mt_cache[letter] = mt
    log_local("robocopy 调度 disk=%s cpu=%d ram=%.1fGB MT=%d" % (disk, cpus, ram, mt))
    return mt


def start_robocopy(cmd):
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        startupinfo=startupinfo,
        creationflags=CREATE_NO_WINDOW,
    )


def run_robocopy_file(task_id, src, dst_dir, file_name, progress=None):
    """robocopy 拷单文件。目标已满且稳定则成功；卡住且未满才切兜底。"""
    src_dir = os.path.dirname(src)
    dst = os.path.join(dst_dir, file_name)
    src_sz = max(0, file_size(src))
    if progress is not None:
        progress.file_total = src_sz
        progress.file_done = 0
        progress.confirmed = False
    cmd = [
        robocopy_exe(),
        robocopy_path(src_dir),
        robocopy_path(dst_dir),
        file_name,
        "/COPY:DAT",
        "/DCOPY:T",
        "/FFT",
        "/XJ",
        "/R:1",
        "/W:1",
        "/NDL",
        "/NJH",
        "/NFL",
        "/NP",
    ]
    log_local("robocopy: %s" % " ".join(cmd))
    if task_cancelled(task_id):
        return "cancelled"
    try:
        proc = start_robocopy(cmd)
    except Exception as e:
        log_local("robocopy 启动失败: %s" % e)
        return "fallback"
    global _current_proc
    with _current_proc_lock:
        _current_proc = proc
    last_size = -1
    last_change = time.time()
    stall_sec = 20
    try:
        while proc.poll() is None:
            if task_cancelled(task_id):
                kill_proc(proc)
                return "cancelled"
            sz = max(0, file_size(dst))
            if progress is not None:
                progress.file_done = sz
                progress.file_total = src_sz or sz
            if sz != last_size:
                last_size = sz
                last_change = time.time()
            elif dest_file_complete(dst, src_sz) and time.time() - last_change >= 1.5:
                log_local("目标已完整，结束等待 robocopy")
                kill_proc(proc)
                if progress is not None:
                    progress.file_done = src_sz or sz
                    progress.file_total = progress.file_done
                    progress.confirmed = True
                return "ok"
            elif time.time() - last_change > stall_sec:
                if dest_file_complete(dst, src_sz):
                    log_local("robocopy 无进展但目标已完整，按成功结束")
                    kill_proc(proc)
                    if progress is not None:
                        progress.file_done = src_sz or sz
                        progress.file_total = progress.file_done
                        progress.confirmed = True
                    return "ok"
                log_local("robocopy 无进展 %ds，切分块复制" % stall_sec)
                kill_proc(proc)
                return "fallback"
            time.sleep(0.3)
        rc = proc.returncode if proc.returncode is not None else 16
        if task_cancelled(task_id):
            return "cancelled"
        if rc < 8 and dest_file_complete(dst, src_sz):
            if progress is not None:
                progress.file_done = src_sz or max(0, file_size(dst))
                progress.file_total = progress.file_done or progress.file_total
                progress.confirmed = True
            return "ok"
        if dest_file_complete(dst, src_sz):
            if progress is not None:
                progress.confirmed = True
            return "ok"
        log_local("robocopy 退出码 %d 或目标不完整，切分块复制" % rc)
        return "fallback"
    finally:
        with _current_proc_lock:
            if _current_proc is proc:
                _current_proc = None


def run_robocopy_dir(task_id, src, dst, mode, progress=None):
    """一次 robocopy 拷文件夹。无进展则切兜底。"""
    mt = choose_robocopy_mt(dst)
    cmd = [
        robocopy_exe(),
        robocopy_path(src),
        robocopy_path(dst),
        "/MIR" if mode == "mirror" else "/E",
        "/COPY:DAT",
        "/DCOPY:T",
        "/FFT",
        "/XJ",
        "/R:1",
        "/W:1",
        "/MT:%d" % mt,
        "/NDL",
        "/NJH",
        "/NFL",
        "/NP",
    ]
    log_local("robocopy: %s" % " ".join(cmd))
    if task_cancelled(task_id):
        return "cancelled"
    try:
        proc = start_robocopy(cmd)
    except Exception as e:
        log_local("robocopy 启动失败: %s" % e)
        return "fallback"
    global _current_proc
    with _current_proc_lock:
        _current_proc = proc
    last_sig = None
    last_change = time.time()
    stall_sec = 45
    last_scan = 0.0
    try:
        while proc.poll() is None:
            if task_cancelled(task_id):
                kill_proc(proc)
                return "cancelled"
            now = time.time()
            if now - last_scan >= 1.5:
                last_scan = now
                bytes_n, files_n = dest_tree_bytes(dst)
                if progress is not None:
                    progress.file_done = bytes_n
                    progress.copied_files = files_n
                    progress.current_name = "文件夹"
                sig = (bytes_n, files_n)
                if sig != last_sig:
                    last_sig = sig
                    last_change = now
            if time.time() - last_change > stall_sec:
                log_local("robocopy 文件夹无进展 %ds，切逐文件复制" % stall_sec)
                kill_proc(proc)
                return "fallback"
            time.sleep(0.4)
        rc = proc.returncode if proc.returncode is not None else 16
        if task_cancelled(task_id):
            return "cancelled"
        if rc < 8:
            if progress is not None:
                progress.confirmed = True
            return "ok"
        log_local("robocopy 退出码 %d，切逐文件复制" % rc)
        return "fallback"
    finally:
        with _current_proc_lock:
            if _current_proc is proc:
                _current_proc = None


def copy_file_chunked(task_id, src, dst, progress=None):
    """兜底：分块读写，拷完立即返回。"""
    src_sz = max(0, file_size(src))
    if progress is not None:
        progress.file_total = src_sz
        progress.file_done = 0
        progress.confirmed = False
    tmp = dst + ".agent_tmp"
    copied = 0
    try:
        with open(src, "rb") as inf, open(tmp, "wb") as outf:
            while True:
                if task_cancelled(task_id):
                    try:
                        outf.close()
                    except Exception:
                        pass
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception:
                        pass
                    return "cancelled"
                buf = inf.read(4 * 1024 * 1024)
                if not buf:
                    break
                outf.write(buf)
                copied += len(buf)
                if progress is not None:
                    progress.file_done = copied
                    progress.file_total = src_sz or copied
        try:
            os.replace(tmp, dst)
        except OSError:
            try:
                if os.path.exists(dst):
                    os.remove(dst)
            except Exception:
                pass
            os.replace(tmp, dst)
        if progress is not None:
            progress.file_done = src_sz or copied
            progress.file_total = progress.file_done
            progress.confirmed = True
        return "ok"
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def copy_file_auto(task_id, src, dst, progress=None):
    """单文件：robocopy 优先，不行再分块。"""
    rc = run_robocopy_file(task_id, src, os.path.dirname(dst), os.path.basename(dst), progress)
    if rc == "ok" or rc == "cancelled":
        return rc
    return copy_file_chunked(task_id, src, dst, progress)


def cleanup_mirror_extras(src, dst):
    for root, dirs, files in os.walk(dst, topdown=False):
        rel_path = os.path.relpath(root, dst)
        src_dir = os.path.join(src, rel_path)
        for f in files:
            dst_file = os.path.join(root, f)
            if not os.path.exists(os.path.join(src_dir, f)):
                try:
                    os.remove(dst_file)
                except Exception:
                    pass
        for d in dirs:
            dst_subdir = os.path.join(root, d)
            if not os.path.exists(os.path.join(src_dir, d)):
                try:
                    shutil.rmtree(dst_subdir)
                except Exception:
                    pass


def copy_single_file(task_id, src, dst_dir, dest_name=None):
    src = normalize_win_path(src)
    dst_dir = normalize_win_path(dst_dir)
    name = dest_name or os.path.basename(src)
    if path_kind(src) != "file":
        report_result(task_id, "error", "源文件不存在: %s" % src)
        return
    if not name:
        report_result(task_id, "error", "目标文件名无效")
        return
    if not ensure_dir(dst_dir, task_id):
        return
    dest_file = os.path.join(dst_dir, name)
    if path_kind(dest_file) == "dir":
        report_result(task_id, "error", "目标已存在且是文件夹，无法写入文件: %s" % dest_file)
        return
    if task_cancelled(task_id):
        report_cancelled(task_id)
        return
    report_result(task_id, "progress", "正在复制 %s ..." % name, 8)
    log_local("复制 %s -> %s" % (src, dest_file))
    progress = NativeCopyProgress(task_id)
    progress.current_name = name
    start = time.time()
    try:
        rc = copy_file_auto(task_id, src, dest_file, progress)
    except Exception as e:
        progress.close()
        report_result(task_id, "error", "复制失败: %s" % e)
        return
    progress.close()
    if rc == "cancelled":
        report_cancelled(task_id)
        return
    size = max(0, progress.file_total or progress.file_done or file_size(src))
    elapsed = max(0.0, time.time() - start)
    report_result(
        task_id,
        "success",
        "文件已复制 %s -> %s (%s, 耗时%.1fs)" % (src, dest_file, format_size(size), elapsed),
        100,
        extra={
            "copied_files": 1,
            "copied_bytes": size,
            "total_bytes": size,
            "elapsed_sec": round(elapsed, 1),
        },
    )


def copy_dir_chunked(task_id, src, dst, mode, progress):
    for root, dirs, files in os.walk(src):
        if task_cancelled(task_id):
            report_cancelled(task_id)
            return "cancelled"
        rel_path = os.path.relpath(root, src)
        target_dir = os.path.join(dst, rel_path)
        os.makedirs(target_dir, exist_ok=True)
        for f in files:
            if task_cancelled(task_id):
                report_cancelled(task_id)
                return "cancelled"
            src_file = os.path.join(root, f)
            dst_file = os.path.join(target_dir, f)
            progress.current_name = f
            progress.file_done = 0
            progress.file_total = max(0, file_size(src_file))
            try:
                rc = copy_file_chunked(task_id, src_file, dst_file, progress)
            except Exception as e:
                report_result(task_id, "error", "复制失败: %s -> %s, %s" % (src_file, dst_file, e))
                return "error"
            if rc == "cancelled":
                report_cancelled(task_id)
                return "cancelled"
            progress.copied_files += 1
            progress.file_done = progress.file_total
    if mode == "mirror":
        if task_cancelled(task_id):
            report_cancelled(task_id)
            return "cancelled"
        report_result(task_id, "progress", "正在清理多余文件...", 95)
        cleanup_mirror_extras(src, dst)
    return "ok"


def run_native_copy(task_id, src, dst, mode):
    src = normalize_win_path(src)
    dst = normalize_win_path(dst)
    if path_kind(src) == "file":
        copy_single_file(task_id, src, dst)
        return
    if path_kind(src) != "dir":
        report_result(task_id, "error", "源路径不存在（不是文件也不是文件夹）: %s" % src)
        return
    if not ensure_dir(dst, task_id):
        return
    report_result(task_id, "progress", "正在用 robocopy 复制文件夹...", 5)
    log_local("复制文件夹 %s -> %s mode=%s" % (src, dst, mode))
    progress = NativeCopyProgress(task_id)
    start = time.time()
    engine = "robocopy"
    try:
        rc = run_robocopy_dir(task_id, src, dst, mode, progress)
        if rc == "cancelled":
            report_cancelled(task_id)
            return
        if rc != "ok":
            report_result(task_id, "progress", "robocopy 失败或卡住，切分块复制...", 15)
            engine = "chunked"
            rc = copy_dir_chunked(task_id, src, dst, mode, progress)
            if rc != "ok":
                return
        if progress is not None:
            progress.confirmed = True
    finally:
        progress.close()
    elapsed = max(0.0, time.time() - start)
    report_result(
        task_id,
        "success",
        "文件夹已同步（%s/%s） %s -> %s  耗时%.1fs" % (mode, engine, src, dst, elapsed),
        100,
        extra={
            "copied_files": progress.copied_files,
            "elapsed_sec": round(elapsed, 1),
        },
    )


def deploy_folder(task):
    task_id = task.get("task_id", str(time.time()))
    src = task.get("source") or ""
    dst = task.get("destination") or ""
    mode = task.get("mode", "overwrite")
    report_result(task_id, "progress", "正在连接共享...", 2)
    if task_cancelled(task_id):
        report_cancelled(task_id)
        return
    if not connect_share(src, task_id):
        return
    target = resolve_copy_target(src, dst, task_id)
    if not target:
        return
    log_local("复制%s %s -> %s" % (
        "文件" if target["kind"] == "file" else "文件夹",
        target["src"],
        target.get("dest_file") or target["dest_dir"],
    ))
    report_result(task_id, "progress", "robocopy 优先，失败再分块复制", 4)
    if task_cancelled(task_id):
        report_cancelled(task_id)
        return
    if target["kind"] == "file":
        try:
            copy_single_file(task_id, target["src"], target["dest_dir"], target["dest_name"])
        except Exception as e:
            report_result(task_id, "error", "复制文件失败: %s" % e)
        return
    try:
        run_native_copy(task_id, target["src"], target["dest_dir"], mode)
    except Exception as e:
        report_result(task_id, "error", "复制文件夹失败: %s" % e)


# ===============================
# 自升级（onedir: agent.exe + _internal）
# ===============================
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_payload(extract_dir):
    for root, dirs, files in os.walk(extract_dir):
        if "agent.exe" in files:
            internal = os.path.join(root, "_internal")
            return os.path.join(root, "agent.exe"), internal if os.path.isdir(internal) else None
        depth = root[len(extract_dir):].count(os.sep)
        if depth >= 2:
            dirs[:] = []
    return None, None


def write_apply_scripts(bat_path, payload_dir, install_dir, new_ver, task_id):
    # 保留 device.ini / log。不用 PowerShell，避免 360 主动防御拦截。
    work = os.path.dirname(bat_path)
    report_url = (CONTROL_SERVER or "").rstrip("/") + REPORT_ENDPOINT
    reports = {
        "88": (88, "正在备份并替换文件"),
        "92": (92, "正在启动新 Agent"),
        "93": (93, "等待新进程启动"),
    }
    for name, (pct, msg) in reports.items():
        with open(os.path.join(work, "report_%s.json" % name), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "task_id": str(task_id or ""),
                    "machine": str(MACHINE_NAME or ""),
                    "status": "progress",
                    "message": msg,
                    "progress": pct,
                    "version": str(new_ver or ""),
                },
                f,
                ensure_ascii=False,
            )

    content = r"""@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "INSTALL=__INSTALL__"
set "SRC=__SRC__"
set "VER=__VER__"
set "AGENT_REPORT=__REPORT__"
set "CURL=%SystemRoot%\System32\curl.exe"

taskkill /F /IM agent.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul

call :POST 88

if exist "%INSTALL%\agent.exe" copy /y "%INSTALL%\agent.exe" "%INSTALL%\agent.exe.bak" >nul
if exist "%INSTALL%\_internal.bak" rd /s /q "%INSTALL%\_internal.bak"

set /a _mv=0
:MV_INTERNAL
if not exist "%INSTALL%\_internal" goto MV_OK
move /y "%INSTALL%\_internal" "%INSTALL%\_internal.bak" >nul 2>&1
if not errorlevel 1 goto MV_OK
set /a _mv+=1
if !_mv! GEQ 8 goto FAIL
ping -n 2 127.0.0.1 >nul
goto MV_INTERNAL
:MV_OK

copy /y "%SRC%\agent.exe" "%INSTALL%\agent.exe" >nul
if errorlevel 1 goto FAIL
if exist "%SRC%\_internal" (
  robocopy "%SRC%\_internal" "%INSTALL%\_internal" /E /IS /IT /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP >nul
  if errorlevel 8 goto FAIL
)
if exist "%SRC%\icon.ico" copy /y "%SRC%\icon.ico" "%INSTALL%\icon.ico" >nul
if exist "%INSTALL%\_internal\icon.ico" copy /y "%INSTALL%\_internal\icon.ico" "%INSTALL%\icon.ico" >nul
del /f /q "%INSTALL%\update_fail.flag" >nul 2>&1
echo %VER%> "%INSTALL%\update_ok.flag"

call :POST 92
set /a _try=0
:START_TRY
start "" "%INSTALL%\agent.exe"
ping -n 2 127.0.0.1 >nul
tasklist /FI "IMAGENAME eq agent.exe" | find /I "agent.exe" >nul
if not errorlevel 1 goto DONE
set /a _try+=1
if !_try! GEQ 20 goto FAIL
call :POST 93
goto START_TRY

:DONE
exit /b 0

:FAIL
echo FAIL> "%INSTALL%\update_fail.flag"
if exist "%INSTALL%\update_ok.flag" del /f /q "%INSTALL%\update_ok.flag" >nul 2>&1
if exist "%INSTALL%\agent.exe.bak" copy /y "%INSTALL%\agent.exe.bak" "%INSTALL%\agent.exe" >nul
if exist "%INSTALL%\_internal.bak" (
  if exist "%INSTALL%\_internal" rd /s /q "%INSTALL%\_internal"
  move /y "%INSTALL%\_internal.bak" "%INSTALL%\_internal" >nul
)
start "" "%INSTALL%\agent.exe"
exit /b 1

:POST
if not exist "%CURL%" goto :eof
if "%AGENT_REPORT%"=="" goto :eof
if not exist "%~dp0report_%~1.json" goto :eof
"%CURL%" -s -m 3 -X POST "%AGENT_REPORT%" -H "Content-Type: application/json; charset=utf-8" --data-binary "@%~dp0report_%~1.json" >nul 2>&1
goto :eof
"""
    content = (
        content.replace("__INSTALL__", install_dir)
        .replace("__SRC__", payload_dir)
        .replace("__VER__", new_ver)
        .replace("__REPORT__", report_url)
    )
    with open(bat_path, "w", encoding="gbk", errors="replace") as f:
        f.write(content)



def allowed_update_url(url):
    try:
        u = urlparse(url)
        s = urlparse(CONTROL_SERVER)
        return u.scheme in ("http", "https") and u.hostname and u.hostname == s.hostname
    except Exception:
        return False


def self_update(task):
    task_id = task.get("task_id", str(time.time()))
    version = (task.get("version") or "").strip()
    url = (task.get("url") or "").strip()
    expect_sha = (task.get("sha256") or "").strip().lower()
    install_dir = BASE_DIR

    if not getattr(sys, "frozen", False):
        report_result(task_id, "error", "当前为脚本模式，不执行自升级")
        return
    if not is_admin() and not install_dir_writable(install_dir):
        report_result(task_id, "error", "升级需要管理员权限（无法写入安装目录）")
        return
    if not version or not url or not expect_sha:
        report_result(task_id, "error", "升级任务缺少 version/url/sha256")
        return
    if version == AGENT_VERSION:
        report_result(task_id, "success", "已是目标版本 %s，无需升级" % version, 100)
        return
    if not allowed_update_url(url):
        report_result(task_id, "error", "升级地址不在控制端白名单: %s" % url)
        return

    work = os.path.join(os.environ.get("TEMP", BASE_DIR), "agent_update_%s" % version)
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    zip_path = os.path.join(work, "pkg.zip")
    extract_dir = os.path.join(work, "extract")
    payload_dir = os.path.join(work, "payload")
    os.makedirs(extract_dir, exist_ok=True)
    os.makedirs(payload_dir, exist_ok=True)

    report_result(task_id, "progress", "开始下载 Agent %s ..." % version, 5)
    log_local("下载升级包 %s" % url)
    try:
        resp = session.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        got = 0
        last_t = 0
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(64 * 1024):
                if task_cancelled(task_id):
                    report_cancelled(task_id, "升级已取消")
                    return
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last_t >= 1:
                    last_t = now
                    pct = 5 + int(50 * (float(got) / total)) if total else 20
                    report_result(
                        task_id,
                        "progress",
                        "下载中 %s / %s" % (format_size(got), format_size(total) if total else "?"),
                        min(55, pct),
                    )
    except Exception as e:
        report_result(task_id, "error", "下载升级包失败: %s" % e)
        return

    report_result(task_id, "progress", "校验 SHA256...", 58)
    if task_cancelled(task_id):
        report_cancelled(task_id, "升级已取消")
        return
    digest = sha256_file(zip_path)
    if digest.lower() != expect_sha:
        report_result(task_id, "error", "校验失败 sha256=%s 期望=%s" % (digest, expect_sha))
        return

    report_result(task_id, "progress", "解压升级包...", 65)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        report_result(task_id, "error", "解压失败: %s" % e)
        return

    exe_path, internal_dir = find_payload(extract_dir)
    if not exe_path:
        report_result(task_id, "error", "升级包内未找到 agent.exe（需含 agent.exe + _internal）")
        return
    shutil.copy2(exe_path, os.path.join(payload_dir, "agent.exe"))
    if internal_dir:
        dest_internal = os.path.join(payload_dir, "_internal")
        if os.path.isdir(dest_internal):
            shutil.rmtree(dest_internal, ignore_errors=True)
        shutil.copytree(internal_dir, dest_internal)
    else:
        report_result(task_id, "error", "升级包缺少 _internal 目录（分离式安装需要）")
        return
    icon_src = os.path.join(os.path.dirname(exe_path), "icon.ico")
    if not os.path.isfile(icon_src) and internal_dir:
        icon_src = os.path.join(internal_dir, "icon.ico")
    if os.path.isfile(icon_src):
        try:
            shutil.copy2(icon_src, os.path.join(payload_dir, "icon.ico"))
        except Exception:
            pass

    bat_path = os.path.join(work, "apply.bat")
    write_apply_scripts(bat_path, payload_dir, install_dir, version, task_id)
    pending = {"task_id": task_id, "version": version, "from": AGENT_VERSION}
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False)
    except Exception as e:
        log_local("写 update_pending 失败: %s" % e)

    report_result(task_id, "progress", "即将重启替换 Agent %s ..." % version, 85)
    if task_cancelled(task_id):
        report_cancelled(task_id, "升级已取消")
        return
    log_local("启动 apply.bat 并退出")
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        subprocess.Popen(
            'cmd.exe /c "%s"' % bat_path,
            shell=True,
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=si,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=False,
        )
    except Exception as e:
        report_result(task_id, "error", "拉起升级脚本失败: %s" % e)
        return
    time.sleep(0.8)
    os._exit(0)


def finish_pending_update():
    pending = {}
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                pending = json.load(f) or {}
        except Exception:
            pending = {}
    task_id = pending.get("task_id") or str(time.time())
    try:
        if os.path.exists(FAIL_FLAG):
            report_result(task_id, "error", "升级失败，已尝试回滚（当前版本 %s）" % AGENT_VERSION)
            log_local("升级失败回滚，当前 %s" % AGENT_VERSION)
            try:
                os.remove(FAIL_FLAG)
            except Exception:
                pass
            try:
                os.remove(PENDING_FILE)
            except Exception:
                pass
            return True
        upgraded = os.path.exists(OK_FLAG) or (
            pending.get("version") == AGENT_VERSION
            and pending.get("from")
            and pending.get("from") != AGENT_VERSION
        )
        if upgraded:
            target = pending.get("version") or AGENT_VERSION
            report_result(
                task_id,
                "success",
                "已升级到 %s（当前运行 %s）" % (target, AGENT_VERSION),
                100,
                extra={"version": AGENT_VERSION},
            )
            log_local("升级完成 %s" % AGENT_VERSION)
            for p in (OK_FLAG, PENDING_FILE):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            return True
    except Exception as e:
        log_local("处理升级结果失败: %s" % e)
    return False


def retry_pending_update():
    for delay in (2, 5, 10):
        time.sleep(delay)
        if not os.path.exists(PENDING_FILE) and not os.path.exists(OK_FLAG) and not os.path.exists(FAIL_FLAG):
            return
        if finish_pending_update():
            return


def capture_screen_jpeg(out_path, live=False):
    from PIL import ImageGrab
    user32 = ctypes.windll.user32
    left = int(user32.GetSystemMetrics(76))
    top = int(user32.GetSystemMetrics(77))
    width = int(user32.GetSystemMetrics(78))
    height = int(user32.GetSystemMetrics(79))
    if width > 0 and height > 0:
        img = ImageGrab.grab(bbox=(left, top, left + width, top + height))
    else:
        img = ImageGrab.grab()
    if img.mode != "RGB":
        img = img.convert("RGB")
    max_w = 1280 if live else 1600
    if img.width > max_w:
        nh = max(1, int(img.height * (max_w / float(img.width))))
        resample = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS
        img = img.resize((max_w, nh), resample)
    folder = os.path.dirname(out_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    img.save(out_path, "JPEG", quality=45 if live else 55, optimize=not live)


def upload_screenshot_file(task_id, jpeg_path):
    with open(jpeg_path, "rb") as f:
        resp = session.post(
            CONTROL_SERVER + "/screenshot",
            data={"task_id": task_id, "machine": MACHINE_NAME},
            files={"file": ("screen.jpg", f, "image/jpeg")},
            timeout=30,
        )
    data = {}
    try:
        data = resp.json() or {}
    except Exception:
        data = {}
    return resp.status_code, data


def do_screenshot(task):
    task_id = task.get("task_id", str(time.time()))
    live = bool(task.get("live"))
    report_result(task_id, "progress", "正在查看屏幕...", 20)
    tmp = os.path.join(os.environ.get("TEMP", BASE_DIR), "agent_shot_%s.jpg" % str(task_id).replace("-", "")[:12])
    frames = 0
    started = time.time()
    try:
        while True:
            if task_cancelled(task_id):
                report_cancelled(task_id, "屏幕查看已取消")
                return
            capture_screen_jpeg(tmp, live=live)
            frames += 1
            code, data = upload_screenshot_file(task_id, tmp)
            if code != 200:
                report_result(task_id, "error", "截图上传失败 HTTP %s" % code)
                return
            if not data.get("ok"):
                report_result(task_id, "error", data.get("error") or "截图上传被拒绝")
                return
            if not live or not data.get("continue"):
                break
            if time.time() - started > 600:
                break
            time.sleep(0.25)
        report_result(task_id, "success", "屏幕查看结束，共 %d 帧" % frames, 100)
    except Exception as e:
        log_local("截图失败: %s" % e)
        report_result(task_id, "error", "截图失败: %s" % e)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


_log_fetch_lock = threading.Lock()


def do_fetch_logs(task):
    task_id = task.get("task_id", str(time.time()))
    day = (task.get("date") or "").strip()
    if len(day) != 8 or not day.isdigit():
        day = ""
    with _log_fetch_lock:
        try:
            cleanup_old_logs()
            dates = list_log_dates()
            if not day:
                day = dates[0] if dates else time.strftime("%Y%m%d")
            text, truncated = read_log_day(day)
            if not text and day not in dates:
                text = "当天没有日志文件（%s）\n" % day
            payload = {
                "task_id": task_id,
                "machine": MACHINE_NAME,
                "date": day,
                "dates": dates,
                "text": text,
                "truncated": truncated,
                "version": AGENT_VERSION,
                "ip": LOCAL_IP,
            }
            resp = session.post(CONTROL_SERVER + "/agent_log", json=payload, timeout=45)
            if resp.status_code != 200:
                report_result(task_id, "error", "日志上传失败 HTTP %s" % resp.status_code)
                return
            data = {}
            try:
                data = resp.json() or {}
            except Exception:
                data = {}
            if not data.get("ok"):
                report_result(task_id, "error", data.get("error") or "日志上传被拒绝")
                return
            report_result(task_id, "success", "已上传本地日志 %s" % day, 100)
        except Exception as e:
            log_local("上传日志失败: %s" % e)
            report_result(task_id, "error", "上传日志失败: %s" % e)


# ===============================
# 执行任务
# ===============================
def run_task(task):
    global _current_task_id, _current_proc, _pending_cancel_id
    task_id = task.get("task_id", str(time.time()))
    action = task.get("action")
    _current_task_id = str(task_id)
    try:
        if task_cancelled(task_id):
            report_cancelled(task_id)
            return
        if action == "deploy_folder":
            deploy_folder(task)
        elif action == "run_command":
            cmd = task["command"]
            if task_cancelled(task_id):
                report_cancelled(task_id)
                return
            proc = subprocess.Popen(cmd, shell=True, creationflags=CREATE_NO_WINDOW)
            with _current_proc_lock:
                _current_proc = proc
            while proc.poll() is None:
                if task_cancelled(task_id):
                    kill_proc(proc)
                    report_cancelled(task_id)
                    return
                time.sleep(0.4)
            report_result(task_id, "success", "命令已执行: %s" % cmd)
        elif action == "self_update":
            self_update(task)
        elif action == "screenshot":
            do_screenshot(task)
        else:
            report_result(task_id, "error", "未知任务类型: %s" % action)
    except Exception as e:
        if task_cancelled(task_id):
            report_cancelled(task_id)
        else:
            log_local("任务异常: %s" % e)
            report_result(task_id, "error", str(e))
    finally:
        with _current_proc_lock:
            _current_proc = None
        if _pending_cancel_id == str(task_id):
            _pending_cancel_id = ""
        _current_task_id = ""
        _cancel_event.clear()
        _busy.clear()


def apply_assigned_machine(name):
    name = (name or "").strip()
    if not name or name == MACHINE_NAME:
        return
    if not valid_machine_name(name):
        return
    persist_machine_name(name)
    log_local("设备编码已与控制端同步为 %s" % name)


def agent_loop():
    global LOCAL_IP
    while True:
        try:
            LOCAL_IP = get_local_ip()
            busy_flag = "1" if _busy.is_set() else "0"
            url = "%s/task?machine=%s&ip=%s&ver=%s&agent_id=%s&busy=%s" % (
                CONTROL_SERVER,
                quote(MACHINE_NAME),
                quote(LOCAL_IP),
                quote(AGENT_VERSION),
                quote(AGENT_ID),
                busy_flag,
            )
            resp = session.get(url, timeout=8)
            if resp.status_code == 200:
                task = resp.json() or {}
                if isinstance(task, dict):
                    apply_assigned_machine(task.get("machine"))
                    cancel_id = (task.get("cancel_task_id") or "").strip()
                    if cancel_id:
                        request_task_cancel(cancel_id)
                    action = task.get("action")
                    if action == "fetch_logs":
                        threading.Thread(target=do_fetch_logs, args=(task,), daemon=True).start()
                    elif action and not _busy.is_set():
                        _busy.set()
                        threading.Thread(target=run_task, args=(task,), daemon=True).start()
        except Exception as e:
            log_local("获取任务失败: %s" % e)
        time.sleep(POLL_INTERVAL)


def tray_icon_size():
    try:
        n = int(ctypes.windll.user32.GetSystemMetrics(49))  # SM_CXSMICON
        if n < 16:
            n = 16
        if n > 64:
            n = 64
        return n
    except Exception:
        return 32


def fit_tray_image(img):
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    size = tray_icon_size()
    if img.size == (size, size):
        return img
    resample = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS
    return img.resize((size, size), resample)


def create_image():
    icon_file = icon_file_path()
    if icon_file:
        try:
            return fit_tray_image(Image.open(icon_file))
        except Exception as e:
            log_local("加载 icon.ico 失败: %s" % e)
    size = tray_icon_size()
    block = max(8, size // 2)
    img = Image.new("RGBA", (size, size), (11, 110, 79, 255))
    d = ImageDraw.Draw(img)
    x = (size - block) // 2
    y = (size - block) // 2
    d.rectangle([x, y, x + block, y + block], fill=(255, 255, 255, 255))
    return img


_tray_icon = None
_rename_busy = threading.Event()


def persist_machine_name(name):
    global MACHINE_NAME
    MACHINE_NAME = name
    cfg = load_ini()
    if "device" not in cfg:
        cfg["device"] = {}
    cfg["device"]["name"] = name
    if AGENT_ID:
        cfg["device"]["id"] = AGENT_ID
    try:
        save_ini(cfg)
    except Exception as e:
        log_local("保存设备编码失败: %s" % e)
    if _tray_icon is not None:
        try:
            _tray_icon.title = "Agent %s (%s) %s" % (AGENT_VERSION, MACHINE_NAME, LOCAL_IP)
        except Exception:
            pass


def change_machine_name_ui():
    if _rename_busy.is_set():
        return
    _rename_busy.set()
    try:
        err = ""
        name = MACHINE_NAME
        while True:
            asked = ask_machine_name(prev=name, err=err)
            if asked.get("cancelled") or not asked.get("val"):
                return
            name = asked["val"]
            if name == MACHINE_NAME:
                return
            ok, err, claimed = claim_machine_name(CONTROL_SERVER, name, AGENT_ID, LOCAL_IP, force=True)
            if ok:
                persist_machine_name(claimed or name)
                log_local("设备编码已改为 %s" % MACHINE_NAME)
                show_popup("info", "Agent", "设备编码已改为 %s" % MACHINE_NAME)
                return
    finally:
        _rename_busy.clear()


def on_tray_rename(icon, item):
    threading.Thread(target=change_machine_name_ui, daemon=True).start()


def run_tray():
    global _tray_icon
    title = "Agent %s (%s) %s" % (AGENT_VERSION, MACHINE_NAME, LOCAL_IP)
    menu = pystray.Menu(
        pystray.MenuItem(
            lambda item: "更改设备编码（%s）" % MACHINE_NAME,
            on_tray_rename,
        ),
    )
    _tray_icon = pystray.Icon("Agent", create_image(), title, menu)
    _tray_icon.run()


if __name__ == "__main__":
    log_local("启动 Agent %s 机台=%s 服务器=%s IP=%s" % (AGENT_VERSION, MACHINE_NAME, CONTROL_SERVER, LOCAL_IP))
    print("[*] Agent %s 已启动，机器名 %s ，控制端 %s" % (AGENT_VERSION, MACHINE_NAME, CONTROL_SERVER))
    finish_pending_update()
    threading.Thread(target=retry_pending_update, daemon=True).start()
    threading.Thread(target=agent_loop, daemon=True).start()
    run_tray()
