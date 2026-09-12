from flask import Flask, request, jsonify, render_template_string, redirect, url_for, Response, send_from_directory, send_file
from waitress import serve
import time
import uuid
import os
import sys
import json
import threading
import subprocess
import ctypes
import webbrowser
import winreg
import hashlib
import zipfile
import shutil
import re
from urllib.parse import quote
import tkinter as tk
from tkinter import messagebox

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024

APP_TITLE = "工厂同步控制台"
APP_REG_NAME = "FactorySyncControl"
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5000


def app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    if getattr(sys, "frozen", False):
        cands = [
            os.path.join(os.path.dirname(sys.executable), name),
            os.path.join(getattr(sys, "_MEIPASS", ""), name),
        ]
        for p in cands:
            if p and os.path.exists(p):
                return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


BASE_DIR = app_base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_DIR = os.path.join(BASE_DIR, "log")
AUDIT_DIR = os.path.join(LOG_DIR, "audit")
PACKAGE_DIR = os.path.join(BASE_DIR, "packages")
SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")
AGENT_LOG_DIR = os.path.join(DATA_DIR, "agent_logs")
AGENT_FILE_DIR = os.path.join(DATA_DIR, "agent_files")
LIGHT_ACTIONS = frozenset({"fetch_logs"})
COLLECT_MAX_BYTES = 80 * 1024 * 1024
DRIVE_RE = re.compile(r"^[A-Za-z]:$")
LOCAL_ABS_RE = re.compile(r"^[A-Za-z]:\\")
WEB_LOG_KEEP_DAYS = 90
ONLINE_THRESHOLD = 20
MAX_RESULTS = 2000
CONTROL_PUBLIC_HOST = "192.168.36.248"
STALE_AFTER_OFFLINE = 10
# 升级就地替换 Agent 所在目录（安装包默认 %LOCALAPPDATA%\agent）
_audit_lock = threading.Lock()
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# 公盘浏览根目录（网页端用账号访问列目录）
SHARE_ROOT = r"\\192.168.36.248\test"
SHARE_USER = "1"
SHARE_PASS = "1"
_share_lock = threading.Lock()

# 常用 CMD 模板（选中后填入输入框，仍可再改）
CMD_PRESETS = [
    {"name": "启动程序", "command": r'start "" "D:\TE\填写exe路径"', "danger": False},
    {"name": "结束进程", "command": r"taskkill /f /im 程序名.exe", "danger": False},
    {"name": "打开目录", "command": r'explorer "D:\TE"', "danger": False},
    {"name": "新建目录", "command": r'mkdir "D:\TE\新目录"', "danger": False},
    {"name": "删除文件", "command": r'del /f /q "D:\TE\填写文件名"', "danger": True},
    {"name": "删除文件夹", "command": r'rd /s /q "D:\TE\填写文件夹"', "danger": True},
    {"name": "清空目录内容", "command": r'del /f /q "D:\TE\*" & for /d %i in ("D:\TE\*") do rd /s /q "%i"', "danger": True},
]

lock = threading.RLock()
_last_web_log_cleanup = 0
tasks = {}          # { machine: [task_json, ...] }
results = []
agents_status = {}  # { machine: {"last_seen": ts} }
screenshot_watch = {}  # { machine: last_heartbeat_ts }
cancel_by_machine = {}  # { machine: task_id }  正在执行、等待 Agent 停掉的任务
MACHINE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z][A-Za-z0-9]*-[0-9]+$")


def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip() or "-"
    return request.remote_addr or "-"


def audit_log(event, detail=""):
    """记录网页访问与操作，便于追溯。写入 log/audit/audit_日期.txt"""
    try:
        cleanup_web_logs()
        os.makedirs(AUDIT_DIR, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ip = client_ip()
        detail = str(detail or "").replace("\r", " ").replace("\n", " ")
        line = f"{ts} | {ip} | {event} | {detail}\n"
        path = os.path.join(AUDIT_DIR, f"audit_{time.strftime('%Y%m%d')}.txt")
        with _audit_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        print(f"[AUDIT] {line.strip()}")
    except Exception as e:
        print("[!] 审计日志写入失败:", e)


def _file_older_than(path, cutoff):
    try:
        name = os.path.basename(path)
        for part in (name.replace("audit_", "").split("_")[0], name[:8]):
            if len(part) >= 8 and part[:8].isdigit():
                try:
                    ts = time.mktime(time.strptime(part[:8], "%Y%m%d"))
                    return ts < cutoff
                except Exception:
                    pass
        return os.path.getmtime(path) < cutoff
    except Exception:
        return False


def cleanup_web_logs(keep_days=WEB_LOG_KEEP_DAYS):
    """控制端本地日志、审计、已拉取的机台日志，保留最近 90 天。"""
    global _last_web_log_cleanup
    now = time.time()
    if _last_web_log_cleanup and now - _last_web_log_cleanup < 3600:
        return
    _last_web_log_cleanup = now
    cutoff = now - max(1, int(keep_days)) * 86400

    def drop_old(path):
        try:
            if os.path.isfile(path) and _file_older_than(path, cutoff):
                os.remove(path)
        except Exception:
            pass

    try:
        if os.path.isdir(AUDIT_DIR):
            for name in os.listdir(AUDIT_DIR):
                if name.endswith(".txt"):
                    drop_old(os.path.join(AUDIT_DIR, name))
        if os.path.isdir(LOG_DIR):
            for name in os.listdir(LOG_DIR):
                path = os.path.join(LOG_DIR, name)
                if os.path.isfile(path) and name.endswith(".txt"):
                    drop_old(path)
        if os.path.isdir(AGENT_LOG_DIR):
            for machine in os.listdir(AGENT_LOG_DIR):
                folder = os.path.join(AGENT_LOG_DIR, machine)
                if not os.path.isdir(folder):
                    continue
                for name in os.listdir(folder):
                    if name.endswith(".txt"):
                        drop_old(os.path.join(folder, name))
    except Exception as e:
        print("[!] 清理控制端日志失败:", e)


def public_base_url():
    host = (request.host or "").split(":")[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        return "http://%s:%s" % (CONTROL_PUBLIC_HOST, SERVER_PORT)
    return request.scheme + "://" + request.host


def parse_version_tuple(ver):
    try:
        parts = [int(x) for x in str(ver).strip().split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)
    except Exception:
        return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def package_paths(version):
    return (
        os.path.join(PACKAGE_DIR, "agent_%s.zip" % version),
        os.path.join(PACKAGE_DIR, "agent_%s.json" % version),
    )


def load_package_meta(version):
    zip_path, meta_path = package_paths(version)
    if not os.path.isfile(zip_path):
        return None
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:
            meta = {}
    meta.setdefault("version", version)
    meta.setdefault("size", os.path.getsize(zip_path))
    if not meta.get("sha256"):
        meta["sha256"] = sha256_file(zip_path)
    return meta


def list_packages():
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    items = []
    seen = set()
    for name in os.listdir(PACKAGE_DIR):
        if not name.lower().endswith(".zip"):
            continue
        m = re.match(r"agent_(\d+\.\d+\.\d+)\.zip$", name, re.I)
        if not m:
            continue
        ver = m.group(1)
        if ver in seen:
            continue
        seen.add(ver)
        meta = load_package_meta(ver)
        if meta:
            items.append(meta)
    items.sort(key=lambda x: parse_version_tuple(x.get("version")) or (0, 0, 0), reverse=True)
    return items


def sort_machines_by_number(machines):
    def get_sort_key(machine):
        parts = machine.split("-")
        if len(parts) >= 3:
            try:
                num = int(parts[-1])
                return (parts[0], parts[1], num)
            except ValueError:
                return (parts[0], parts[1], 999999)
        return (machine,)

    return sorted(machines, key=get_sort_key)


def normalize_unc(path):
    path = (path or "").strip().strip('"').strip("'").replace("/", "\\")
    if not path:
        return SHARE_ROOT
    if path.startswith("\\\\"):
        rest = path[2:]
        while "\\\\" in rest:
            rest = rest.replace("\\\\", "\\")
        path = "\\\\" + rest
    if len(path) > 3 and path.endswith("\\"):
        path = path.rstrip("\\")
    return path


def normalize_win_dest(path):
    p = (path or "").strip().strip('"').strip("'").replace("/", "\\")
    if not p:
        return ""
    if p.startswith("\\\\"):
        return normalize_unc(p)
    while "\\\\" in p:
        p = p.replace("\\\\", "\\")
    p = p.rstrip("\\")
    if len(p) == 2 and p[1] == ":":
        return p + "\\"
    try:
        return os.path.normpath(p)
    except Exception:
        return p


def normalize_search_drive(drive):
    d = (drive or "").strip().upper().replace("/", "\\").rstrip("\\")
    if len(d) == 1 and d.isalpha():
        d += ":"
    if DRIVE_RE.match(d):
        return d
    return ""


def parse_search_drives(raw):
    if isinstance(raw, (list, tuple)):
        parts = [str(x or "").strip() for x in raw if str(x or "").strip()]
        raw = ",".join(parts)
    s = (raw or "").strip()
    if not s:
        return "D:"
    tokens = [p.strip() for p in re.split(r"[,;\s]+", s) if p.strip()]
    if any(t.lower() in ("all", "*", "全部") for t in tokens):
        return "all"
    out = []
    for t in tokens:
        d = normalize_search_drive(t)
        if d and d not in out:
            out.append(d)
    return ",".join(out)


def first_search_drive(drive):
    d = normalize_search_drive(drive)
    if d:
        return d
    m = re.search(r"([A-Za-z]):", drive or "")
    if m:
        return m.group(1).upper() + ":"
    return ""


_KW_SPLIT_RE = re.compile(r"[,;\s\u3000\uff0c\u3001]+")


def parse_search_keywords(raw, limit=20):
    seen = set()
    out = []
    for part in _KW_SPLIT_RE.split(raw or ""):
        w = part.strip().strip('"').strip("'")
        if len(w) < 2:
            continue
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def parse_max_hits(raw, default=200):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(1, min(500, n))


def abs_local_hit_path(path, drive=""):
    p = (path or "").strip().strip('"').strip("'").replace("/", "\\")
    if not p or p.startswith("\\\\"):
        return ""
    if LOCAL_ABS_RE.match(p):
        return normalize_win_dest(p)
    d = first_search_drive(drive)
    if not d:
        return ""
    if p.startswith("\\"):
        return normalize_win_dest(d + p)
    return normalize_win_dest(d + "\\" + p)


def valid_collect_path(path, drive=""):
    p = abs_local_hit_path(path, drive) or normalize_win_dest(path)
    if not p or p.startswith("\\\\"):
        return ""
    if not LOCAL_ABS_RE.match(p):
        return ""
    if len(p) <= 3:
        return ""
    return p


def agent_file_dir(machine):
    return os.path.join(AGENT_FILE_DIR, safe_machine_filename(machine))


def safe_stored_filename(name):
    base = os.path.basename(name or "") or "file"
    base = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", base, flags=re.U)
    if not base or base in (".", ".."):
        base = "file"
    if len(base) > 120:
        root, ext = os.path.splitext(base)
        base = root[:100] + ext[:20]
    return base or "file"


def under_share(path):
    root = normalize_unc(SHARE_ROOT).lower()
    p = normalize_unc(path).lower()
    return p == root or p.startswith(root + "\\")


def path_leaf_name(path):
    p = (path or "").replace("/", "\\").rstrip("\\")
    if not p:
        return ""
    return p.rsplit("\\", 1)[-1]


def source_is_file(source):
    src = (source or "").strip().replace("/", "\\")
    if src.startswith("\\\\"):
        src = normalize_unc(src)
    try:
        if os.path.isfile(src):
            return True
        if os.path.isdir(src):
            return False
    except Exception:
        pass
    leaf = path_leaf_name(src)
    return bool(leaf and re.search(r"\.[A-Za-z0-9]{1,8}$", leaf))


def follow_destination(source, destination):
    """公盘路径若是文件夹，目标自动跟上最后一级目录名。"""
    src = (source or "").strip()
    dst = (destination or "").strip()
    if not src or not dst:
        return destination
    if source_is_file(src):
        return destination
    leaf = path_leaf_name(src)
    if not leaf:
        return destination
    dst_norm = dst.replace("/", "\\").rstrip("\\")
    if path_leaf_name(dst_norm).lower() == leaf.lower():
        return dst_norm
    return dst_norm + "\\" + leaf


def ensure_share_connected():
    """用账号1/密码1连接公盘，供控制端列目录。"""
    root = normalize_unc(SHARE_ROOT)
    with _share_lock:
        if os.path.isdir(root):
            return None
        cmd = f'net use "{root}" /user:{SHARE_USER} {SHARE_PASS} /persistent:no'
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if os.path.isdir(root):
            return None
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit={proc.returncode}"
        return f"连接公盘失败: {detail}"


def build_classes():
    classes = {}
    with lock:
        names = list(agents_status.keys())
    for m in names:
        parts = m.split("-")
        if len(parts) >= 2:
            cls, sub = parts[0], parts[1]
            classes.setdefault(cls, {}).setdefault(sub, []).append(m)
        else:
            classes.setdefault("未分组", {}).setdefault("-", []).append(m)
    for cls in classes:
        for sub in classes[cls]:
            classes[cls][sub] = sort_machines_by_number(classes[cls][sub])
    return classes


def queue_len(machine):
    q = tasks.get(machine, [])
    if isinstance(q, dict):
        return 1
    return len(q)


def is_busy(machine):
    """任一未完成且已开始执行的任务即视为执行中（避免只看最新一条漏判）。"""
    for r in results:
        if r.get("machine") != machine or r.get("status") != "progress":
            continue
        if r.get("exec_ts") or (r.get("message") or "") != "任务已下发，等待执行...":
            return True
    return False


def safe_machine_filename(name):
    return re.sub(r"[^\w.\-]+", "_", str(name or ""), flags=re.U) or "unknown"


def is_ipv4(ip):
    parts = str(ip or "").split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def agent_status_online(st, now=None):
    now = now or time.time()
    last_seen = float((st or {}).get("last_seen") or 0)
    return bool(last_seen) and (now - last_seen) < ONLINE_THRESHOLD


def machine_online(machine, now=None):
    return agent_status_online(agents_status.get(machine) or {}, now)


def fail_machine_progress(machine, message="机台编码已被新 Agent 覆盖，原任务中断"):
    """调用方需已持有 lock。"""
    changed = False
    for r in results:
        if r.get("machine") != machine or r.get("status") != "progress":
            continue
        apply_report_timing(r, {
            "status": "error",
            "progress": r.get("progress") if r.get("progress") not in (None, "") else 0,
            "message": message,
            "machine": machine,
        })
        drop_task_from_queue(machine, r.get("task_id"))
        r.pop("cancel_requested", None)
        clear_cancel_request(machine, r.get("task_id"))
        changed = True
    return changed


def valid_machine_name(name):
    name = (name or "").strip()
    return bool(name) and name.upper() != "UNKNOWN" and bool(MACHINE_NAME_RE.match(name))


def find_machine_by_agent_id(agent_id):
    """调用方需已持有 lock。"""
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return ""
    for m, st in agents_status.items():
        if (st.get("agent_id") or "").strip() == agent_id:
            return m
    return ""


def can_replace_online(st, agent_id, ip=""):
    """1.0 升 2.0 / 同机重装：心跳未过期时也允许顶替，避免误报已在线。"""
    existing_id = ((st or {}).get("agent_id") or "").strip()
    agent_id = (agent_id or "").strip()
    if existing_id and agent_id and existing_id == agent_id:
        return True
    if not existing_id:
        return bool(agent_id)
    old_ip = ((st or {}).get("ip") or "").strip()
    ip = (ip or "").strip()
    if is_ipv4(old_ip) and is_ipv4(ip) and old_ip == ip:
        return True
    return False


def takeover_offline_machine(machine, agent_id, now=None, ip="", ver=""):
    """离线或可顶替的在线机台编码允许被新 Agent 覆盖。调用方需已持有 lock。"""
    now = now or time.time()
    st = agents_status.setdefault(machine, {})
    old_id = (st.get("agent_id") or "").strip()
    owner_changed = bool(agent_id) and old_id != agent_id
    if owner_changed:
        fail_machine_progress(machine)
        st["watch_task_id"] = ""
        screenshot_watch.pop(machine, None)
        if ip:
            st["ip"] = ip
        else:
            st.pop("ip", None)
        if ver:
            st["version"] = ver
        else:
            st.pop("version", None)
    st["last_seen"] = now
    if agent_id:
        st["agent_id"] = agent_id
    return owner_changed


def screenshot_paths(machine):
    base = safe_machine_filename(machine)
    return (
        os.path.join(SCREENSHOT_DIR, base + ".jpg"),
        os.path.join(SCREENSHOT_DIR, base + ".json"),
    )


def agent_log_dir(machine):
    return os.path.join(AGENT_LOG_DIR, safe_machine_filename(machine))


def agent_log_meta_path(machine):
    return os.path.join(agent_log_dir(machine), "meta.json")


def agent_log_day_path(machine, day):
    return os.path.join(agent_log_dir(machine), "%s.txt" % day)


def valid_log_day(day):
    return bool(day) and len(str(day)) == 8 and str(day).isdigit()


def list_cached_log_dates(machine):
    dates = []
    folder = agent_log_dir(machine)
    try:
        if not os.path.isdir(folder):
            return dates
        for name in os.listdir(folder):
            if name.endswith(".txt") and valid_log_day(name[:8]) and len(name) == 12:
                dates.append(name[:8])
    except Exception:
        pass
    dates.sort(reverse=True)
    return dates


def load_agent_log_meta(machine):
    path = agent_log_meta_path(machine)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_agent_log_meta(machine, meta):
    folder = agent_log_dir(machine)
    os.makedirs(folder, exist_ok=True)
    path = agent_log_meta_path(machine)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, path)


def pop_next_task(machine, heartbeat_only=False):
    """优先弹出轻量任务（如拉日志）。heartbeat_only 时不弹出复制等重任务。"""
    q = tasks.get(machine)
    if not q:
        return None
    if isinstance(q, dict):
        if heartbeat_only and (q.get("action") not in LIGHT_ACTIONS):
            return None
        tasks.pop(machine, None)
        return q
    if not isinstance(q, list):
        return None
    for i, t in enumerate(q):
        if isinstance(t, dict) and t.get("action") in LIGHT_ACTIONS:
            item = q.pop(i)
            if not q:
                tasks.pop(machine, None)
            return item
    if heartbeat_only:
        return None
    item = q.pop(0)
    if not q:
        tasks.pop(machine, None)
    return item


def _relocate_screenshot_files(old_name, new_name):
    old_img, old_meta = screenshot_paths(old_name)
    new_img, new_meta = screenshot_paths(new_name)
    if old_img == new_img:
        return
    for src, dst in ((old_img, new_img), (old_meta, new_meta)):
        if os.path.isfile(src):
            try:
                if os.path.isfile(dst):
                    os.remove(dst)
                os.replace(src, dst)
            except Exception:
                pass


def _relocate_agent_logs(old_name, new_name):
    old_d = agent_log_dir(old_name)
    new_d = agent_log_dir(new_name)
    if old_d == new_d or not os.path.isdir(old_d):
        return
    try:
        if not os.path.isdir(new_d):
            os.replace(old_d, new_d)
            return
        for name in os.listdir(old_d):
            src = os.path.join(old_d, name)
            dst = os.path.join(new_d, name)
            if os.path.isfile(src):
                if os.path.isfile(dst):
                    os.remove(dst)
                os.replace(src, dst)
        shutil.rmtree(old_d, ignore_errors=True)
    except Exception:
        pass


def _relocate_agent_files(old_name, new_name):
    old_d = agent_file_dir(old_name)
    new_d = agent_file_dir(new_name)
    if old_d == new_d or not os.path.isdir(old_d):
        return
    try:
        if not os.path.isdir(new_d):
            os.replace(old_d, new_d)
            return
        for name in os.listdir(old_d):
            src = os.path.join(old_d, name)
            dst = os.path.join(new_d, name)
            if os.path.isfile(src):
                if os.path.isfile(dst):
                    os.remove(dst)
                os.replace(src, dst)
        shutil.rmtree(old_d, ignore_errors=True)
    except Exception:
        pass


def rename_machine_record(old_name, new_name):
    """调用方需已持有 lock。"""
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if old_name == new_name:
        return
    st = agents_status.pop(old_name, None)
    if st is None:
        return
    agents_status[new_name] = st
    q = tasks.pop(old_name, None)
    if q is not None:
        items = q if isinstance(q, list) else [q]
        for t in items:
            if isinstance(t, dict):
                t["machine"] = new_name
                t["machines"] = [new_name]
        tasks[new_name] = q
    for r in results:
        if r.get("machine") == old_name:
            r["machine"] = new_name
    if old_name in screenshot_watch:
        screenshot_watch[new_name] = screenshot_watch.pop(old_name)
    if old_name in cancel_by_machine:
        cancel_by_machine[new_name] = cancel_by_machine.pop(old_name)
    _relocate_screenshot_files(old_name, new_name)
    _relocate_agent_logs(old_name, new_name)
    _relocate_agent_files(old_name, new_name)


def delete_machine_record(machine):
    """调用方需已持有 lock。仅用于离线机台。"""
    fail_machine_progress(machine, "机台已从列表删除，任务中断")
    tasks.pop(machine, None)
    agents_status.pop(machine, None)
    screenshot_watch.pop(machine, None)
    cancel_by_machine.pop(machine, None)
    img, meta = screenshot_paths(machine)
    for p in (img, meta):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass
    shutil.rmtree(agent_log_dir(machine), ignore_errors=True)
    shutil.rmtree(agent_file_dir(machine), ignore_errors=True)


def enqueue_machine_task(machine, task_data, message="任务已下发，等待执行...", batch_id=None):
    """调用方需已持有 lock。"""
    task_id = task_data.get("task_id") or str(uuid.uuid4())
    task_data["task_id"] = task_id
    task_data.setdefault("machines", [machine])
    q = tasks.get(machine)
    if isinstance(q, list):
        q.append(task_data)
    elif isinstance(q, dict):
        tasks[machine] = [q, task_data]
    else:
        tasks[machine] = [task_data]
    now_ts = time.time()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    st = agents_status.get(machine) or {}
    results.append({
        "task_id": task_id,
        "batch_id": batch_id or str(uuid.uuid4()),
        "machine": machine,
        "ip": st.get("ip") or "",
        "status": "progress",
        "message": message,
        "time": now_str,
        "progress": 0,
        "start_ts": now_ts,
        "start_time": now_str,
        "action": task_data.get("action") or "",
        "collect_path": task_data.get("path") or "",
        "keyword": task_data.get("keyword") or "",
        "drive": task_data.get("drive") or "",
    })
    if len(results) > MAX_RESULTS:
        del results[:-MAX_RESULTS]
    return task_id


def drop_task_from_queue(machine, task_id):
    q = tasks.get(machine)
    if isinstance(q, list):
        tasks[machine] = [t for t in q if t.get("task_id") != task_id]
        if not tasks[machine]:
            tasks.pop(machine, None)
    elif isinstance(q, dict) and q.get("task_id") == task_id:
        tasks.pop(machine, None)


def task_still_queued(machine, task_id):
    q = tasks.get(machine)
    if isinstance(q, list):
        return any((t or {}).get("task_id") == task_id for t in q)
    if isinstance(q, dict):
        return q.get("task_id") == task_id
    return False


def task_has_started(r):
    if (r or {}).get("status") != "progress":
        return False
    if r.get("exec_ts"):
        return True
    return (r.get("message") or "") != "任务已下发，等待执行..."


def clear_cancel_request(machine, task_id=None):
    cur = cancel_by_machine.get(machine)
    if not cur:
        return
    if task_id is None or cur == task_id:
        cancel_by_machine.pop(machine, None)


def cancel_one_result(r):
    """调用方需已持有 lock。返回 queued / running / skip。"""
    if not r or r.get("status") != "progress":
        return "skip"
    task_id = r.get("task_id")
    machine = r.get("machine")
    still_queued = task_still_queued(machine, task_id)
    drop_task_from_queue(machine, task_id)
    st = agents_status.get(machine) or {}
    if st.get("watch_task_id") == task_id:
        st["watch_task_id"] = ""
        screenshot_watch.pop(machine, None)
    if (not still_queued) or task_has_started(r) or r.get("cancel_requested"):
        r["cancel_requested"] = True
        r["message"] = "正在取消，等待机台停止..."
        if task_id:
            cancel_by_machine[machine] = task_id
        return "running"
    apply_report_timing(r, {
        "status": "cancelled",
        "progress": r.get("progress") if r.get("progress") not in (None, "") else 0,
        "message": "任务已取消（未开始执行）",
        "machine": machine,
    })
    return "queued"


def fail_stale_progress():
    """Agent 被 kill / 离线后，把一直停在进行中的任务结成失败，避免进度条永远卡住。"""
    now = time.time()
    limit = ONLINE_THRESHOLD + STALE_AFTER_OFFLINE
    update_limit = 180
    changed = False
    with lock:
        for r in results:
            if r.get("status") != "progress":
                continue
            machine = r.get("machine")
            st = agents_status.get(machine) or {}
            if _self_update_finished_online(r, st, now):
                ver = (st.get("version") or "").strip()
                apply_report_timing(r, {
                    "status": "success",
                    "progress": 100,
                    "message": "已升级到 %s（机台已恢复在线）" % ver,
                    "version": ver,
                    "machine": machine,
                })
                r.pop("cancel_requested", None)
                clear_cancel_request(machine, r.get("task_id"))
                changed = True
                continue
            last_seen = float(st.get("last_seen") or 0)
            start_ts = float(r.get("start_ts") or 0)
            row_limit = update_limit if _is_self_update_row(r) else limit
            if last_seen:
                if now - last_seen < row_limit:
                    continue
            elif start_ts and now - start_ts < row_limit:
                continue
            msg = "替换超时，可能被杀毒软件拦截，请在机台查看 Agent 是否已启动"
            if not _is_self_update_row(r):
                msg = "机台离线或 Agent 已退出，任务中断"
            apply_report_timing(r, {
                "status": "error",
                "progress": r.get("progress") if r.get("progress") not in (None, "") else 0,
                "message": msg,
                "machine": machine,
            })
            drop_task_from_queue(machine, r.get("task_id"))
            r.pop("cancel_requested", None)
            clear_cancel_request(machine, r.get("task_id"))
            changed = True
    if changed:
        save_state()


def _is_self_update_row(r):
    if (r or {}).get("action") == "self_update":
        return True
    msg = (r or {}).get("message") or ""
    return ("替换 Agent" in msg) or ("正在备份并替换" in msg) or ("正在启动新 Agent" in msg) or ("杀毒" in msg)


def _update_target_version(r):
    v = ((r or {}).get("update_version") or "").strip()
    if v:
        return v
    msg = (r or {}).get("message") or ""
    m = re.search(r"(\d+\.\d+\.\d+)", msg)
    return m.group(1) if m else ""


def _self_update_finished_online(r, st, now):
    if not _is_self_update_row(r):
        return False
    if not agent_status_online(st, now):
        return False
    ver = (st.get("version") or "").strip()
    target = _update_target_version(r)
    return bool(ver and target and ver == target)


def save_state():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with lock:
            payload = {
                "tasks": tasks,
                "results": results[-MAX_RESULTS:],
                "agents_status": agents_status,
            }
            raw = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("[!] 保存状态失败:", e)


def load_state():
    global tasks, results, agents_status
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        loaded_tasks = data.get("tasks", {}) or {}
        normalized = {}
        for m, v in loaded_tasks.items():
            if isinstance(v, list):
                normalized[m] = v
            elif isinstance(v, dict) and v:
                normalized[m] = [v]
            else:
                normalized[m] = []
        with lock:
            tasks = normalized
            results = data.get("results", []) or []
            agents_status = data.get("agents_status", {}) or {}
        print(f"[*] 已恢复状态: 机台 {len(agents_status)}, 排队任务 {sum(len(q) for q in tasks.values())}, 日志 {len(results)}")
    except Exception as e:
        print("[!] 加载状态失败:", e)


def format_duration(sec):
    try:
        sec = int(max(0, round(float(sec))))
    except (TypeError, ValueError):
        return "-"
    if sec < 60:
        return f"{sec}秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}分{s}秒"
    h, m = divmod(m, 60)
    return f"{h}小时{m}分{s}秒"


def normalize_result_hits(row):
    hits = (row or {}).get("hits")
    drive = (row or {}).get("drive") or ""
    if not isinstance(hits, list):
        return
    for h in hits:
        if not isinstance(h, dict):
            continue
        fixed = abs_local_hit_path(h.get("path") or "", drive)
        if fixed:
            h["path"] = fixed


def apply_report_timing(existing, data):
    """耗时优先用 Agent 实测 elapsed_sec（纯复制时间），避免回传延迟把几秒显示成几十秒。"""
    now_ts = time.time()
    start_ts = existing.get("start_ts")
    start_time = existing.get("start_time")
    exec_ts = existing.get("exec_ts")
    old_duration_sec = existing.get("duration_sec")
    old_duration_text = existing.get("duration_text")
    old_end_time = existing.get("end_time")

    status = data.get("status")
    msg = data.get("message") or ""
    try:
        agent_elapsed = data.get("elapsed_sec")
        if agent_elapsed is not None:
            agent_elapsed = float(agent_elapsed)
    except (TypeError, ValueError):
        agent_elapsed = None
    # 首次真正开始执行（Agent 回传，且不是本地占位文案）
    if exec_ts is None and status in ("progress", "success", "error", "cancelled"):
        if msg != "任务已下发，等待执行...":
            exec_ts = now_ts

    existing.update(data)

    if start_ts is not None:
        existing["start_ts"] = start_ts
    if start_time:
        existing["start_time"] = start_time
    if exec_ts is not None:
        existing["exec_ts"] = exec_ts

    if status in ("success", "error", "cancelled"):
        if agent_elapsed is not None:
            dur = max(0, int(round(agent_elapsed)))
            existing["duration_sec"] = dur
            existing["duration_text"] = format_duration(dur)
            existing["elapsed_sec"] = agent_elapsed
            existing["end_time"] = data.get("time") or time.strftime("%Y-%m-%d %H:%M:%S")
            if existing.get("start_ts") is not None and existing.get("exec_ts") is not None:
                existing["queue_sec"] = max(0, int(round(existing["exec_ts"] - existing["start_ts"])))
        else:
            base = existing.get("exec_ts") or existing.get("start_ts")
            if base is not None:
                dur = max(0, int(round(now_ts - base)))
                existing["duration_sec"] = dur
                existing["duration_text"] = format_duration(dur)
                existing["end_time"] = data.get("time") or time.strftime("%Y-%m-%d %H:%M:%S")
                if existing.get("start_ts") is not None and existing.get("exec_ts") is not None:
                    existing["queue_sec"] = max(0, int(round(existing["exec_ts"] - existing["start_ts"])))
            elif old_duration_sec is not None:
                existing["duration_sec"] = old_duration_sec
                existing["duration_text"] = old_duration_text or format_duration(old_duration_sec)
                if old_end_time:
                    existing["end_time"] = old_end_time
    else:
        # 进行中：优先用 Agent 实测复制秒数，避免 HTTP/排队把耗时拉长
        if agent_elapsed is not None:
            existing["elapsed_sec"] = agent_elapsed
            existing["duration_sec"] = max(0, int(round(agent_elapsed)))
            existing["duration_text"] = format_duration(agent_elapsed) + "…"
        elif old_duration_sec is not None:
            existing["duration_sec"] = old_duration_sec
        if agent_elapsed is None and old_duration_text:
            existing["duration_text"] = old_duration_text
        if old_end_time:
            existing["end_time"] = old_end_time
    return existing


def archive_results(rows):
    if not rows:
        return None
    os.makedirs(LOG_DIR, exist_ok=True)
    filename = os.path.join(LOG_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}.txt")
    with open(filename, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(
                f"{r.get('time', '')} {r.get('machine', '')} {r.get('status', '')} "
                f"{r.get('progress', '')} {r.get('duration_text', '')} {r.get('message', '')}\n"
            )
    return filename


def agents_snapshot():
    fail_stale_progress()
    now = time.time()
    with lock:
        items = []
        online = offline = queued = busy = 0
        for m, st in agents_status.items():
            last_seen = st.get("last_seen", 0)
            is_online = (now - last_seen) < ONLINE_THRESHOLD
            qlen = queue_len(m)
            machine_busy = is_busy(m)
            if is_online:
                online += 1
            else:
                offline += 1
            queued += qlen
            if machine_busy:
                busy += 1
            items.append({
                "name": m,
                "online": is_online,
                "last_seen": last_seen,
                "queue": qlen,
                "busy": machine_busy,
                "ip": st.get("ip") or "",
                "version": st.get("version") or "",
                "os": st.get("os") or "",
            })
        return {
            "now": now,
            "summary": {
                "online": online,
                "offline": offline,
                "queued": queued,
                "busy": busy,
                "total": len(agents_status),
            },
            "agents": items,
            "classes": build_classes(),
        }


# ===============================
# Agent 拉取任务（兼容：仍返回单条 JSON）
# ===============================
@app.route("/agent/claim", methods=["GET", "POST"])
def agent_claim():
    machine = (request.args.get("machine") or request.form.get("machine") or "").strip()
    agent_id = (request.args.get("agent_id") or request.form.get("agent_id") or "").strip()
    ip = (request.args.get("ip") or request.form.get("ip") or "").strip()
    force = (request.args.get("force") or request.form.get("force") or "").strip().lower() in ("1", "true", "yes")
    if not machine:
        return jsonify({"ok": False, "error": "机台名不能为空"}), 400
    now = time.time()
    with lock:
        bound = find_machine_by_agent_id(agent_id) if agent_id else ""
        if bound:
            st = agents_status.get(bound) or {}
            st["last_seen"] = now
            if ip:
                st["ip"] = ip
            if agent_id:
                st["agent_id"] = agent_id
            if machine and machine != bound:
                if force:
                    if not valid_machine_name(machine):
                        return jsonify({
                            "ok": False,
                            "error": "格式必须是 线体-站位-编号，例如 T1-DL-01",
                        }), 400
                    if machine in agents_status:
                        target = agents_status.get(machine) or {}
                        if agent_status_online(target, now) and not can_replace_online(target, agent_id, ip):
                            return jsonify({
                                "ok": False,
                                "error": "机台名「%s」已在线，请更换（不能与已有在线机台重复）" % machine,
                            }), 409
                        delete_machine_record(machine)
                    rename_machine_record(bound, machine)
                    bound = machine
                    st = agents_status.get(bound) or {}
                    st["last_seen"] = now
                    if ip:
                        st["ip"] = ip
                    if agent_id:
                        st["agent_id"] = agent_id
            save_needed = True
        else:
            if not valid_machine_name(machine):
                return jsonify({
                    "ok": False,
                    "error": "格式必须是 线体-站位-编号，例如 T1-DL-01",
                }), 400
            st = agents_status.get(machine) or {}
            online = agent_status_online(st, now)
            if online and not can_replace_online(st, agent_id, ip):
                return jsonify({
                    "ok": False,
                    "error": "机台名「%s」已在线，请更换（不能与已有在线机台重复）" % machine,
                }), 409
            takeover_offline_machine(machine, agent_id, now=now, ip=ip)
            bound = machine
            save_needed = True
    if save_needed:
        save_state()
    return jsonify({"ok": True, "machine": bound})


@app.route("/api/machine_rename", methods=["POST"])
def api_machine_rename():
    data = request.get_json(force=True, silent=True) or {}
    old_name = (data.get("old") or data.get("machine") or "").strip()
    new_name = (data.get("new") or data.get("name") or "").strip()
    if not old_name or not new_name:
        return jsonify({"ok": False, "error": "请填写原编码和新编码"}), 400
    if old_name == new_name:
        return jsonify({"ok": True, "machine": new_name})
    if not valid_machine_name(new_name):
        return jsonify({"ok": False, "error": "新编码格式必须是 线体-站位-编号，例如 T1-DL-01"}), 400
    with lock:
        if old_name not in agents_status:
            return jsonify({"ok": False, "error": "机台不存在: %s" % old_name}), 404
        if new_name in agents_status:
            return jsonify({"ok": False, "error": "机台名「%s」已存在，不能重复" % new_name}), 409
        rename_machine_record(old_name, new_name)
    save_state()
    audit_log("MACHINE_RENAME", "old=%s new=%s" % (old_name, new_name))
    return jsonify({"ok": True, "old": old_name, "machine": new_name})


@app.route("/api/machine_delete", methods=["POST"])
def api_machine_delete():
    data = request.get_json(force=True, silent=True) or {}
    machine = (data.get("machine") or "").strip()
    if not machine:
        return jsonify({"ok": False, "error": "未指定机台"}), 400
    with lock:
        if machine not in agents_status:
            return jsonify({"ok": False, "error": "机台不存在: %s" % machine}), 404
        if machine_online(machine):
            return jsonify({"ok": False, "error": "在线机台不能删除，请先离线"}), 409
        delete_machine_record(machine)
    save_state()
    audit_log("MACHINE_DELETE", "machine=%s" % machine)
    return jsonify({"ok": True, "machine": machine})


@app.route("/api/task_cancel", methods=["POST"])
def api_task_cancel():
    data = request.get_json(force=True, silent=True) or {}
    task_id = (data.get("task_id") or "").strip()
    machine = (data.get("machine") or "").strip()
    cancel_all = bool(data.get("all"))
    queued = running = skipped = 0
    with lock:
        targets = []
        if cancel_all:
            targets = [r for r in results if r.get("status") == "progress"]
        elif task_id:
            targets = [r for r in results if r.get("task_id") == task_id]
            if not targets:
                return jsonify({"ok": False, "error": "任务不存在或已结束"}), 404
        elif machine:
            targets = [r for r in results if r.get("machine") == machine and r.get("status") == "progress"]
        else:
            return jsonify({"ok": False, "error": "请指定 task_id / machine / all"}), 400
        if not targets:
            return jsonify({"ok": False, "error": "没有进行中的任务"}), 404
        for r in targets:
            kind = cancel_one_result(r)
            if kind == "queued":
                queued += 1
            elif kind == "running":
                running += 1
            else:
                skipped += 1
    save_state()
    audit_log(
        "TASK_CANCEL",
        "task_id=%s machine=%s all=%s queued=%s running=%s" % (
            task_id or "-", machine or "-", int(cancel_all), queued, running,
        ),
    )
    return jsonify({
        "ok": True,
        "queued": queued,
        "running": running,
        "skipped": skipped,
        "message": "已取消等待中 %d 条，正在通知机台停止 %d 条" % (queued, running),
    })


@app.route("/task", methods=["GET"])
def get_task():
    machine = request.args.get("machine")
    if not machine:
        return jsonify({})
    ip = (request.args.get("ip") or "").strip()
    ver = (request.args.get("ver") or "").strip()
    incoming_id = (request.args.get("agent_id") or "").strip()
    os_tag = (request.args.get("os") or "").strip().lower()
    heartbeat_only = (request.args.get("busy") or "").strip().lower() in ("1", "true", "yes")
    now = time.time()
    with lock:
        if incoming_id:
            aliased = find_machine_by_agent_id(incoming_id)
            if aliased:
                machine = aliased
        st = agents_status.get(machine)
        if st:
            online = agent_status_online(st, now)
            existing_id = (st.get("agent_id") or "").strip()
            if online:
                if can_replace_online(st, incoming_id, ip):
                    if incoming_id and existing_id != incoming_id:
                        takeover_offline_machine(machine, incoming_id, now=now, ip=ip, ver=ver)
                elif incoming_id or existing_id:
                    return jsonify({"ok": False, "error": "机台名已被其它 Agent 占用"}), 409
            elif incoming_id and incoming_id != existing_id:
                takeover_offline_machine(machine, incoming_id, now=now, ip=ip, ver=ver)
        if machine not in agents_status:
            agents_status[machine] = {"last_seen": 0}
        agents_status[machine]["last_seen"] = now
        if ip:
            agents_status[machine]["ip"] = ip
        if ver:
            agents_status[machine]["version"] = ver
        if incoming_id:
            agents_status[machine]["agent_id"] = incoming_id
        if os_tag in ("win7", "win10"):
            agents_status[machine]["os"] = os_tag
        task = pop_next_task(machine, heartbeat_only=heartbeat_only)
        assigned = machine
    if task is not None:
        save_state()
    body = dict(task) if isinstance(task, dict) else {}
    body["machine"] = assigned
    cancel_id = cancel_by_machine.get(assigned) or ""
    if cancel_id:
        body["cancel_task_id"] = cancel_id
    return jsonify(body)


@app.route("/report", methods=["POST"])
def report_result():
    data = request.get_json(force=True)
    data["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"status": "error", "msg": "缺少 task_id"})
    with lock:
        m = (data.get("machine") or "").strip()
        if m and m not in agents_status:
            ip = (data.get("ip") or "").strip()
            for name, st in agents_status.items():
                if ip and (st.get("ip") or "") == ip:
                    data["machine"] = name
                    break
        updated = False
        for r in results:
            if r.get("task_id") == task_id:
                # 完成后忽略迟到的进度包，避免进度条被刷回
                if r.get("status") in ("success", "cancelled") and data.get("status") == "progress":
                    updated = True
                    break
                if r.get("cancel_requested") and data.get("status") == "progress":
                    updated = True
                    break
                incoming = data.get("status")
                if r.get("cancel_requested") and incoming == "error":
                    data = dict(data)
                    data["status"] = "cancelled"
                    if "取消" not in (data.get("message") or ""):
                        data["message"] = "任务已取消"
                    incoming = "cancelled"
                apply_report_timing(r, data)
                normalize_result_hits(r)
                if incoming in ("success", "error", "cancelled"):
                    r.pop("cancel_requested", None)
                    clear_cancel_request(r.get("machine"), task_id)
                updated = True
                break
        if not updated:
            # Agent 直接上报且本地无下发记录时，以当前为起点
            now_ts = time.time()
            data["start_ts"] = now_ts
            data["start_time"] = data["time"]
            if data.get("status") in ("progress", "success", "error", "cancelled"):
                data["exec_ts"] = now_ts
            if data.get("status") in ("success", "error", "cancelled"):
                data["duration_sec"] = 0
                data["duration_text"] = "0秒"
                data["end_time"] = data["time"]
            results.append(data)
            normalize_result_hits(data)
        machine = data.get("machine")
        if machine:
            st = agents_status.setdefault(machine, {"last_seen": time.time()})
            st["last_seen"] = time.time()
            if data.get("ip"):
                st["ip"] = data.get("ip")
            if data.get("version"):
                st["version"] = data.get("version")
            if data.get("status") in ("success", "error", "cancelled"):
                clear_cancel_request(machine, task_id)
        if len(results) > MAX_RESULTS:
            del results[:-MAX_RESULTS]
    print(f"[REPORT] {data}")
    # 进度上报很频繁，仅在结束态落盘，减轻磁盘压力
    if data.get("status") in ("success", "error", "cancelled"):
        save_state()
    return jsonify({"status": "received"})


@app.route("/results_json")
def results_json():
    fail_stale_progress()
    with lock:
        for row in results:
            normalize_result_hits(row)
        return jsonify(list(results))


@app.route("/agents_json")
def agents_json():
    return jsonify(agents_snapshot())


@app.route("/export_logs")
def export_logs():
    with lock:
        rows = list(results)
    lines = []
    for r in rows:
        lines.append(
            f"{r.get('time', '')}\t{r.get('machine', '')}\t{r.get('status', '')}\t"
            f"{r.get('progress', '')}\t{r.get('duration_text', '')}\t{r.get('message', '')}"
        )
    content = "\n".join(lines)
    filename = f"logs_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    audit_log("EXPORT_LOGS", f"导出条数={len(rows)} file={filename}")
    return Response(
        content,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/screenshot", methods=["POST"])
def api_screenshot():
    data = request.get_json(force=True, silent=True) or {}
    machine = (data.get("machine") or "").strip()
    if not machine:
        return jsonify({"ok": False, "error": "未指定机台"}), 400
    with lock:
        if not machine_online(machine):
            return jsonify({"ok": False, "error": "机台不在线"}), 400
        ver = (agents_status.get(machine) or {}).get("version") or ""
        if not parse_version_tuple(ver):
            return jsonify({"ok": False, "error": "旧版 Agent 不支持截图，请先升级"}), 400
        screenshot_watch[machine] = time.time()
        st = agents_status.get(machine) or {}
        existing_id = st.get("watch_task_id")
        if existing_id:
            for r in results:
                if r.get("task_id") == existing_id and r.get("status") == "progress":
                    return jsonify({"ok": True, "task_id": existing_id, "machine": machine, "live": True})
        task_id = enqueue_machine_task(
            machine,
            {"action": "screenshot", "machines": [machine], "live": True},
            message="正在查看屏幕（连续截图）...",
        )
        agents_status.setdefault(machine, {})["watch_task_id"] = task_id
    save_state()
    audit_log("SCREENSHOT", "machine=%s task_id=%s live=1" % (machine, task_id))
    return jsonify({"ok": True, "task_id": task_id, "machine": machine, "live": True})


@app.route("/api/screenshot_watch", methods=["POST"])
def api_screenshot_watch():
    data = request.get_json(force=True, silent=True) or {}
    machine = (data.get("machine") or "").strip()
    stop = bool(data.get("stop"))
    if not machine:
        return jsonify({"ok": False, "error": "未指定机台"}), 400
    with lock:
        if stop:
            screenshot_watch.pop(machine, None)
        else:
            screenshot_watch[machine] = time.time()
    return jsonify({"ok": True})


@app.route("/screenshot", methods=["POST"])
def screenshot_upload():
    task_id = (request.form.get("task_id") or "").strip()
    machine = (request.form.get("machine") or "").strip()
    f = request.files.get("file")
    if not task_id or not machine or not f:
        return jsonify({"ok": False, "error": "缺少 task_id/machine/file"}), 400
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    img_path, meta_path = screenshot_paths(machine)
    tmp = img_path + ".uploading"
    try:
        f.save(tmp)
        os.replace(tmp, img_path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500
    now = time.time()
    meta = {
        "machine": machine,
        "task_id": task_id,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": now,
        "size": os.path.getsize(img_path) if os.path.isfile(img_path) else 0,
    }
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(meta, mf, ensure_ascii=False)
    with lock:
        hb = float(screenshot_watch.get(machine) or 0)
        continue_live = (now - hb) < 12
        st = agents_status.setdefault(machine, {})
        st["last_seen"] = now
        st["last_shot_ts"] = meta["ts"]
        st["last_shot_task"] = task_id
        if not continue_live:
            screenshot_watch.pop(machine, None)
            if st.get("watch_task_id") == task_id:
                st["watch_task_id"] = ""
            for r in results:
                if r.get("task_id") == task_id:
                    apply_report_timing(r, {
                        "status": "success",
                        "progress": 100,
                        "message": "屏幕查看结束",
                        "machine": machine,
                    })
                    break
    if not continue_live:
        save_state()
    return jsonify({"ok": True, "continue": continue_live})


@app.route("/screenshot/<machine>")
def screenshot_get(machine):
    img_path, meta_path = screenshot_paths(machine)
    if not os.path.isfile(img_path):
        return jsonify({"ok": False, "error": "暂无截图"}), 404
    return send_file(img_path, mimetype="image/jpeg", max_age=0, as_attachment=False)


@app.route("/screenshot_meta/<machine>")
def screenshot_meta(machine):
    img_path, meta_path = screenshot_paths(machine)
    wait_task = (request.args.get("task_id") or "").strip()
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:
            meta = {}
    ready = os.path.isfile(img_path)
    if wait_task:
        ready = ready and meta.get("task_id") == wait_task
    return jsonify({
        "ok": True,
        "ready": ready,
        "time": (meta.get("time") or "") if ready else "",
        "task_id": meta.get("task_id") or "",
        "ts": (meta.get("ts") or 0) if ready else 0,
    })


def merge_log_dates(*groups):
    dates = set()
    for group in groups:
        for d in group or []:
            if valid_log_day(d):
                dates.add(str(d))
    return sorted(dates, reverse=True)


@app.route("/api/agent_log", methods=["POST"])
def api_agent_log():
    data = request.get_json(force=True, silent=True) or {}
    machine = (data.get("machine") or "").strip()
    day = (data.get("date") or "").strip()
    if day and not valid_log_day(day):
        return jsonify({"ok": False, "error": "日期格式应为 YYYYMMDD"}), 400
    if not machine:
        return jsonify({"ok": False, "error": "未指定机台"}), 400
    with lock:
        if not machine_online(machine):
            return jsonify({"ok": False, "error": "机台不在线"}), 400
        ver = (agents_status.get(machine) or {}).get("version") or ""
        if not parse_version_tuple(ver):
            return jsonify({"ok": False, "error": "旧版 Agent 不支持远程日志，请先升级"}), 400
        st = agents_status.get(machine) or {}
        existing_id = st.get("log_task_id")
        if existing_id:
            for r in results:
                if r.get("task_id") != existing_id or r.get("status") != "progress":
                    continue
                pending_day = ""
                q = tasks.get(machine)
                items = q if isinstance(q, list) else ([q] if isinstance(q, dict) else [])
                for t in items:
                    if isinstance(t, dict) and t.get("task_id") == existing_id:
                        pending_day = (t.get("date") or "").strip()
                        break
                if (pending_day or "") == (day or ""):
                    return jsonify({"ok": True, "task_id": existing_id, "machine": machine, "date": day})
        payload = {"action": "fetch_logs", "machines": [machine]}
        if day:
            payload["date"] = day
        task_id = enqueue_machine_task(
            machine,
            payload,
            message="正在拉取机台日志%s..." % ((" " + day) if day else ""),
        )
        agents_status.setdefault(machine, {})["log_task_id"] = task_id
    save_state()
    audit_log("AGENT_LOG", "machine=%s task_id=%s date=%s" % (machine, task_id, day or "latest"))
    return jsonify({"ok": True, "task_id": task_id, "machine": machine, "date": day})


@app.route("/agent_log", methods=["POST"])
def agent_log_upload():
    data = request.get_json(force=True, silent=True) or {}
    task_id = (data.get("task_id") or "").strip()
    machine = (data.get("machine") or "").strip()
    day = (data.get("date") or "").strip()
    text = data.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    if not task_id or not machine:
        return jsonify({"ok": False, "error": "缺少 task_id/machine"}), 400
    if day and not valid_log_day(day):
        day = time.strftime("%Y%m%d")
    if not day:
        day = time.strftime("%Y%m%d")
    max_chars = 1_500_000
    truncated = bool(data.get("truncated"))
    if len(text) > max_chars:
        text = "…（服务端截断，仅保留末尾）\n" + text[-max_chars:]
        truncated = True
    dates = data.get("dates") if isinstance(data.get("dates"), list) else []
    folder = agent_log_dir(machine)
    os.makedirs(folder, exist_ok=True)
    day_path = agent_log_day_path(machine, day)
    tmp = day_path + ".uploading"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, day_path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500
    now = time.time()
    cached = list_cached_log_dates(machine)
    meta = {
        "machine": machine,
        "task_id": task_id,
        "date": day,
        "dates": merge_log_dates(dates, cached),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": now,
        "truncated": truncated,
        "size": os.path.getsize(day_path) if os.path.isfile(day_path) else 0,
        "version": (data.get("version") or ""),
        "ip": (data.get("ip") or ""),
    }
    save_agent_log_meta(machine, meta)
    with lock:
        st = agents_status.setdefault(machine, {})
        st["last_seen"] = now
        st["last_log_ts"] = now
        st["last_log_task"] = task_id
        st["last_log_date"] = day
        if st.get("log_task_id") == task_id:
            st["log_task_id"] = ""
    return jsonify({"ok": True, "date": day, "dates": meta["dates"]})


@app.route("/agent_log_meta/<machine>")
def agent_log_meta(machine):
    wait_task = (request.args.get("task_id") or "").strip()
    want_date = (request.args.get("date") or "").strip()
    meta = load_agent_log_meta(machine)
    cached = list_cached_log_dates(machine)
    dates = merge_log_dates(meta.get("dates"), cached)
    date = want_date if valid_log_day(want_date) else (meta.get("date") or (dates[0] if dates else ""))
    day_ready = bool(date) and os.path.isfile(agent_log_day_path(machine, date))
    ready = day_ready
    if wait_task:
        ready = day_ready and meta.get("task_id") == wait_task
        if want_date and valid_log_day(want_date):
            ready = ready and meta.get("date") == want_date
    return jsonify({
        "ok": True,
        "ready": ready,
        "cached": day_ready,
        "date": date if day_ready else "",
        "dates": dates,
        "time": (meta.get("time") or "") if ready or day_ready else "",
        "task_id": meta.get("task_id") or "",
        "ts": (meta.get("ts") or 0) if ready or day_ready else 0,
        "truncated": bool(meta.get("truncated")) if (ready or (day_ready and meta.get("date") == date)) else False,
    })


@app.route("/agent_log/<machine>")
def agent_log_get(machine):
    want_date = (request.args.get("date") or "").strip()
    meta = load_agent_log_meta(machine)
    cached = list_cached_log_dates(machine)
    date = want_date if valid_log_day(want_date) else (meta.get("date") or (cached[0] if cached else ""))
    if not date:
        return jsonify({"ok": False, "error": "暂无机台日志"}), 404
    path = agent_log_day_path(machine, date)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "该日期暂无缓存，请先刷新"}), 404
    if request.args.get("download"):
        filename = "agent_%s_%s.txt" % (safe_machine_filename(machine), date)
        try:
            with open(path, "rb") as f:
                content = f.read()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return Response(
            content,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=%s" % filename},
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({
        "ok": True,
        "machine": machine,
        "date": date,
        "dates": merge_log_dates(meta.get("dates"), cached),
        "text": text,
        "time": meta.get("time") or "",
        "ts": meta.get("ts") or 0,
        "truncated": bool(meta.get("truncated")) and meta.get("date") == date,
        "task_id": meta.get("task_id") or "",
    })


def _enqueue_collect_file(machine, path, batch_id=None):
    """调用方需已持有 lock。"""
    task_id = enqueue_machine_task(
        machine,
        {"action": "collect_file", "machines": [machine], "path": path},
        message="正在回传文件...",
        batch_id=batch_id,
    )
    return task_id


@app.route("/api/collect_file", methods=["POST"])
def api_collect_file():
    data = request.get_json(force=True, silent=True) or {}
    machine = (data.get("machine") or "").strip()
    path = valid_collect_path(data.get("path") or "", data.get("drive") or "")
    if not machine:
        return jsonify({"ok": False, "error": "未指定机台"}), 400
    if not path:
        return jsonify({"ok": False, "error": "路径无效：只接受本地盘绝对路径（如 D:\\TE\\a.txt）"}), 400
    with lock:
        if not machine_online(machine):
            return jsonify({"ok": False, "error": "机台不在线"}), 400
        task_id = _enqueue_collect_file(machine, path)
    save_state()
    audit_log("COLLECT_FILE", "dispatch machine=%s path=%s task_id=%s" % (machine, path, task_id))
    return jsonify({"ok": True, "task_id": task_id, "machine": machine, "path": path})


@app.route("/agent/file", methods=["POST"])
def agent_file_upload():
    task_id = (request.form.get("task_id") or "").strip()
    machine = (request.form.get("machine") or "").strip()
    src_path = valid_collect_path(request.form.get("path") or "")
    orig_name = (request.form.get("name") or "").strip()
    f = request.files.get("file")
    if not task_id or not machine or not f:
        return jsonify({"ok": False, "error": "缺少 task_id/machine/file"}), 400
    clen = request.content_length
    if clen and clen > COLLECT_MAX_BYTES + 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "文件超过 %dMB 上限" % (COLLECT_MAX_BYTES // (1024 * 1024))}), 413
    if not orig_name:
        orig_name = os.path.basename(src_path) or f.filename or "file"
    stored = "%s_%s" % (re.sub(r"[^A-Za-z0-9]", "", task_id)[:8] or "file", safe_stored_filename(orig_name))
    folder = agent_file_dir(machine)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, stored)
    tmp = dest + ".uploading"
    try:
        f.save(tmp)
        size = os.path.getsize(tmp)
        if size > COLLECT_MAX_BYTES:
            os.remove(tmp)
            return jsonify({"ok": False, "error": "文件超过 %dMB 上限" % (COLLECT_MAX_BYTES // (1024 * 1024))}), 413
        os.replace(tmp, dest)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500
    file_url = "/agent_file/%s/%s" % (quote(machine, safe=""), quote(stored, safe=""))
    if orig_name:
        file_url += "?name=" + quote(orig_name, safe="")
    with lock:
        for r in results:
            if r.get("task_id") == task_id:
                r["file_url"] = file_url
                r["file_name"] = orig_name
                r["file_size"] = size
                r["stored_name"] = stored
                if src_path:
                    r["collect_path"] = src_path
                st = agents_status.setdefault(machine, {"last_seen": time.time()})
                st["last_seen"] = time.time()
                break
    save_state()
    audit_log(
        "COLLECT_FILE",
        "upload machine=%s path=%s size=%s stored=%s task_id=%s" % (machine, src_path or orig_name, size, stored, task_id),
    )
    return jsonify({
        "ok": True,
        "file_url": file_url,
        "file_name": orig_name,
        "file_size": size,
        "stored_name": stored,
    })


@app.route("/agent_file/<machine>/<filename>")
def agent_file_get(machine, filename):
    if not machine or not filename:
        return jsonify({"ok": False, "error": "参数无效"}), 400
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        return jsonify({"ok": False, "error": "文件名无效"}), 400
    folder = agent_file_dir(machine)
    path = os.path.abspath(os.path.join(folder, filename))
    root = os.path.abspath(folder)
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    download_name = (request.args.get("name") or "").strip() or filename
    download_name = os.path.basename(download_name) or filename
    return send_from_directory(folder, filename, as_attachment=True, download_name=download_name)


@app.route("/rdp/<machine>")
def rdp_file(machine):
    with lock:
        st = agents_status.get(machine) or {}
        ip = (st.get("ip") or "").strip()
    if not is_ipv4(ip):
        return jsonify({"ok": False, "error": "该机台还没有上报有效 IP（需新版 Agent）"}), 400
    body = (
        "screen mode id:i:1\r\n"
        "use multimon:i:0\r\n"
        "session bpp:i:32\r\n"
        "full address:s:%s\r\n"
        "audiomode:i:2\r\n"
        "authentication level:i:2\r\n"
        "prompt for credentials:i:1\r\n"
        "negotiate security layer:i:1\r\n"
        "enablecredsspsupport:i:1\r\n"
    ) % ip
    fname = "%s.rdp" % safe_machine_filename(machine)
    audit_log("RDP_FILE", "machine=%s ip=%s" % (machine, ip))
    return Response(
        body,
        mimetype="application/rdp",
        headers={
            "Content-Disposition": "attachment; filename=%s" % fname,
            "Cache-Control": "no-store",
        },
    )


@app.route("/agent/packages_json")
def agent_packages_json():
    return jsonify({"ok": True, "packages": list_packages()})


@app.route("/agent/package/<version>")
def agent_package_download(version):
    if not VERSION_RE.match(version or ""):
        return jsonify({"ok": False, "error": "版本号无效"}), 400
    zip_path, _ = package_paths(version)
    if not os.path.isfile(zip_path):
        return jsonify({"ok": False, "error": "升级包不存在"}), 404
    return send_from_directory(
        PACKAGE_DIR,
        os.path.basename(zip_path),
        as_attachment=True,
        download_name="agent_%s.zip" % version,
        mimetype="application/zip",
    )


@app.route("/agent/package/<version>/meta")
def agent_package_meta(version):
    if not VERSION_RE.match(version or ""):
        return jsonify({"ok": False, "error": "版本号无效"}), 400
    meta = load_package_meta(version)
    if not meta:
        return jsonify({"ok": False, "error": "升级包不存在"}), 404
    return jsonify(meta)


@app.route("/agent/upload", methods=["POST"])
def agent_package_upload():
    f = request.files.get("file")
    version = (request.form.get("version") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "请选择 zip 文件"}), 400
    name = f.filename.lower()
    if not name.endswith(".zip"):
        return jsonify({"ok": False, "error": "只接受 zip 升级包"}), 400
    if not version:
        m = re.search(r"(\d+\.\d+\.\d+)", f.filename)
        version = m.group(1) if m else ""
    if not VERSION_RE.match(version):
        return jsonify({"ok": False, "error": "版本号格式应为 x.y.z，例如 2.0.0"}), 400
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    zip_path, meta_path = package_paths(version)
    tmp = zip_path + ".uploading"
    try:
        f.save(tmp)
        with zipfile.ZipFile(tmp, "r") as zf:
            names = zf.namelist()
        joined = "\n".join(n.replace("\\", "/").lower() for n in names)
        has_exe = "agent.exe" in joined
        has_internal = any(
            n.replace("\\", "/").rstrip("/").lower().endswith("_internal")
            or "/_internal/" in n.replace("\\", "/").lower()
            for n in names
        )
        has_win10 = "win10/_internal/" in joined or joined.endswith("win10/_internal")
        has_win7 = "win7/_internal/" in joined or joined.endswith("win7/_internal")
        # 兼容布局：_internal/win10/_internal 与顶层 win10/_internal 都能过
        looks_bundle = "win10/" in joined or "win7/" in joined
        has_mftscan = any(n.replace("\\", "/").lower().endswith("/mftscan.exe") or n.lower() == "mftscan.exe" for n in names)
        if not has_exe:
            os.remove(tmp)
            return jsonify({"ok": False, "error": "zip 内未找到 agent.exe"}), 400
        if has_mftscan:
            os.remove(tmp)
            return jsonify({"ok": False, "error": "升级包含 mftscan.exe，请用当前不含独立扫描 exe 的统一包"}), 400
        if looks_bundle and not (has_win10 and has_win7):
            os.remove(tmp)
            return jsonify({"ok": False, "error": "统一包需同时包含 win10 与 win7 运行时"}), 400
        if not has_internal:
            os.remove(tmp)
            return jsonify({"ok": False, "error": "zip 内未找到 _internal（需统一包或旧版 onedir）"}), 400
        os.replace(tmp, zip_path)
        digest = sha256_file(zip_path)
        meta = {
            "version": version,
            "sha256": digest,
            "size": os.path.getsize(zip_path),
            "notes": notes,
            "os": "any" if has_win10 and has_win7 else "legacy",
            "uploaded": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(meta_path, "w", encoding="utf-8") as mf:
            json.dump(meta, mf, ensure_ascii=False, indent=2)
        audit_log("AGENT_PACKAGE_UPLOAD", "version=%s size=%s sha256=%s" % (version, meta["size"], digest))
        return jsonify({"ok": True, "package": meta})
    except zipfile.BadZipFile:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return jsonify({"ok": False, "error": "不是有效的 zip 文件"}), 400
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/browse")
def browse_share():
    """浏览公盘目录，供网页选择源路径。"""
    root = normalize_unc(SHARE_ROOT)
    path = normalize_unc(request.args.get("path") or root)
    if not under_share(path):
        return jsonify({"ok": False, "error": "只能浏览公盘目录: " + root}), 400

    err = ensure_share_connected()
    if err:
        return jsonify({"ok": False, "error": err}), 500

    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": f"目录不存在或无法访问: {path}"}), 404

    entries = []
    try:
        for name in os.listdir(path):
            full = path + "\\" + name
            try:
                is_dir = os.path.isdir(full)
            except Exception:
                is_dir = False
            entries.append({
                "name": name,
                "path": full,
                "type": "dir" if is_dir else "file",
            })
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取目录失败: {e}"}), 500

    entries.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))

    parent = None
    if path.lower() != root.lower():
        parent = path.rsplit("\\", 1)[0]
        if not under_share(parent):
            parent = root

    return jsonify({
        "ok": True,
        "root": root,
        "path": path,
        "parent": parent,
        "entries": entries,
    })


@app.route("/", methods=["GET", "POST"])
def dashboard():
    global results
    error = request.args.get("error", "")

    if request.method == "POST":
        if "clear_logs" in request.form:
            with lock:
                archived = list(results)
                results = []
            archive_results(archived)
            save_state()
            audit_log("CLEAR_ALL_LOGS", f"归档条数={len(archived)}")
            return redirect(url_for("dashboard"))

        if "clear_done" in request.form:
            with lock:
                keep = [r for r in results if r.get("status") == "progress"]
                done = [r for r in results if r.get("status") != "progress"]
                results = keep
            archive_results(done)
            save_state()
            audit_log("CLEAR_DONE_LOGS", f"清理已完成={len(done)} 保留进行中={len(keep)}")
            return redirect(url_for("dashboard"))

        machines = request.form.getlist("machines")
        action = request.form.get("action", "")
        source = (request.form.get("source") or "").strip()
        destination = (request.form.get("destination") or "").strip()
        mode = request.form.get("mode") or "overwrite"
        command = (request.form.get("command") or "").strip()
        update_version = (request.form.get("update_version") or "").strip()
        keyword = (request.form.get("keyword") or "").strip()
        drive = parse_search_drives(request.form.getlist("drive") or request.form.get("drive"))
        max_hits = parse_max_hits(request.form.get("max_hits"), 200)
        collect_path = valid_collect_path(request.form.get("collect_path") or "")

        if not machines:
            audit_log("DISPATCH_REJECT", "未选择机台")
            return redirect(url_for("dashboard", error="请至少选择一台在线机台"))
        update_meta = None
        if action == "deploy_folder":
            if not source:
                audit_log("DISPATCH_REJECT", "复制任务未选公盘路径")
                return redirect(url_for("dashboard", error="请选择公盘文件或文件夹"))
            if not destination:
                audit_log("DISPATCH_REJECT", "复制任务缺少目标路径")
                return redirect(url_for("dashboard", error="请填写目标路径"))
            source = source.strip().strip('"').strip("'")
            if source.startswith("\\\\"):
                source = normalize_unc(source)
                ensure_share_connected()
            destination = normalize_win_dest(destination)
            if not os.path.isfile(source) and not os.path.isdir(source):
                audit_log("DISPATCH_REJECT", "公盘路径不存在 " + source)
                return redirect(url_for("dashboard", error="公盘路径不存在或无法访问: " + source))
            if os.path.isdir(source) and os.path.isfile(destination):
                audit_log("DISPATCH_REJECT", "目标路径是文件 " + destination)
                return redirect(url_for("dashboard", error="目标路径是文件，不能复制文件夹: " + destination))
            destination = follow_destination(source, destination)
            destination = normalize_win_dest(destination)
        elif action == "run_command":
            if not command:
                audit_log("DISPATCH_REJECT", "命令为空")
                return redirect(url_for("dashboard", error="执行命令不能为空"))
        elif action == "self_update":
            if not VERSION_RE.match(update_version or ""):
                audit_log("DISPATCH_REJECT", "升级版本号无效")
                return redirect(url_for("dashboard", error="请选择有效的 Agent 升级包版本"))
            update_meta = load_package_meta(update_version)
            if not update_meta:
                audit_log("DISPATCH_REJECT", "升级包不存在 " + update_version)
                return redirect(url_for("dashboard", error="升级包不存在: " + update_version))
        elif action == "search_files":
            keywords = parse_search_keywords(keyword)
            if not keywords:
                audit_log("DISPATCH_REJECT", "搜索关键字过短")
                return redirect(url_for("dashboard", error="请填写关键字，多个用逗号或空格分隔，每个至少 2 个字符"))
            keyword = ",".join(keywords)
            if not drive:
                audit_log("DISPATCH_REJECT", "搜索盘符无效")
                return redirect(url_for("dashboard", error="请至少选择一个盘符，或选全部本地磁盘"))
        elif action == "collect_file":
            if not collect_path:
                audit_log("DISPATCH_REJECT", "回传路径无效")
                return redirect(url_for("dashboard", error="请填写本地盘绝对路径（如 D:\\TE\\a.txt），不能是 UNC 或目录"))
        else:
            audit_log("DISPATCH_REJECT", f"未知任务类型 action={action}")
            return redirect(url_for("dashboard", error="未知任务类型"))

        pkg_url = ""
        if action == "self_update":
            pkg_url = public_base_url().rstrip("/") + "/agent/package/" + update_version

        with lock:
            batch_id = str(uuid.uuid4())
            now_ts = time.time()
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            skipped = []
            skipped_names = set()
            for m in machines:
                if action == "self_update":
                    cur_ver = (agents_status.get(m) or {}).get("version") or ""
                    if not parse_version_tuple(cur_ver):
                        skipped.append(m + "(旧版无自升级)")
                        skipped_names.add(m)
                        continue
                    if parse_version_tuple(cur_ver) >= parse_version_tuple(update_version):
                        skipped.append(m + "(已是" + cur_ver + ")")
                        skipped_names.add(m)
                        continue
                task_id = str(uuid.uuid4())
                task_data = {
                    "task_id": task_id,
                    "machines": [m],
                    "action": action,
                    "source": source,
                    "destination": destination,
                    "mode": mode,
                    "command": command,
                }
                if action == "search_files":
                    task_data.update({
                        "keyword": keyword,
                        "drive": drive,
                        "max_hits": max_hits,
                    })
                if action == "collect_file":
                    task_data["path"] = collect_path
                if action == "self_update":
                    task_data.update({
                        "version": update_version,
                        "url": pkg_url,
                        "sha256": update_meta.get("sha256"),
                    })
                q = tasks.get(m)
                if isinstance(q, list):
                    q.append(task_data)
                elif isinstance(q, dict):
                    tasks[m] = [q, task_data]
                else:
                    tasks[m] = [task_data]
                st = agents_status.get(m) or {}
                results.append({
                    "task_id": task_id,
                    "batch_id": batch_id,
                    "machine": m,
                    "ip": st.get("ip") or "",
                    "status": "progress",
                    "message": "任务已下发，等待执行...",
                    "time": now_str,
                    "progress": 0,
                    "start_ts": now_ts,
                    "start_time": now_str,
                    "action": action,
                    "update_version": update_version if action == "self_update" else "",
                    "keyword": keyword if action == "search_files" else "",
                    "drive": drive if action == "search_files" else "",
                    "max_hits": max_hits if action == "search_files" else "",
                    "collect_path": collect_path if action == "collect_file" else "",
                })
            if len(results) > MAX_RESULTS:
                del results[:-MAX_RESULTS]
        save_state()

        machines_txt = ",".join(machines)
        if action == "run_command":
            audit_log(
                "DISPATCH",
                f"action=run_command machines=[{machines_txt}] command={command}",
            )
        elif action == "self_update":
            audit_log(
                "DISPATCH",
                f"action=self_update version={update_version} machines=[{machines_txt}] "
                f"skipped=[{','.join(skipped)}]",
            )
        elif action == "search_files":
            audit_log(
                "SEARCH_FILES",
                f"keyword={keyword} drive={drive} max_hits={max_hits} machines=[{machines_txt}]",
            )
        elif action == "collect_file":
            audit_log(
                "COLLECT_FILE",
                f"path={collect_path} machines=[{machines_txt}]",
            )
        else:
            audit_log(
                "DISPATCH",
                f"action=deploy_folder mode={mode} source={source} destination={destination} "
                f"machines=[{machines_txt}]",
            )
        if action == "self_update" and skipped_names:
            dispatched = [m for m in machines if m not in skipped_names]
            if not dispatched:
                return redirect(url_for("dashboard", error="没有可升级机台：" + "；".join(skipped)))
        return redirect(url_for("dashboard"))

    # 仅记录打开控制台页面，不记录轮询接口
    if not error:
        audit_log("PAGE_VIEW", "打开控制台")
    else:
        audit_log("PAGE_VIEW", f"打开控制台 error={error}")

    classes = build_classes()
    return render_template_string(
        HTML_TEMPLATE,
        results=results,
        agents_status=agents_status,
        time=time,
        classes=classes,
        error=error,
        online_threshold=ONLINE_THRESHOLD,
        share_root=SHARE_ROOT,
        cmd_presets=CMD_PRESETS,
    )


HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>工厂同步控制台</title>
<style>
:root {
  --bg: #e8edf2;
  --panel: #ffffff;
  --line: #c5ced8;
  --text: #1f2a37;
  --muted: #5b6b7c;
  --accent: #0b6e4f;
  --accent2: #0a4d8c;
  --warn: #b45309;
  --danger: #b91c1c;
  --ok: #15803d;
  --offline: #94a3b8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  background: linear-gradient(180deg, #d9e2ec 0%, var(--bg) 40%, #dfe6ee 100%);
  color: var(--text);
  min-height: 100vh;
}
.topbar {
  background: #123047;
  color: #fff;
  padding: 14px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  border-bottom: 4px solid #0b6e4f;
}
.topbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.topbar h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 1px; }
.btn-top {
  height: 36px;
  padding: 0 14px;
  border: 1px solid rgba(255,255,255,0.35);
  background: rgba(255,255,255,0.12);
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}
.btn-top:hover { background: rgba(255,255,255,0.22); }
.panel-head {
  margin: 0;
  padding: 10px 14px;
  font-size: 15px;
  background: #f3f6f9;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.panel-head span { font-weight: 700; }
.btn-mini {
  height: 30px;
  padding: 0 10px;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--text);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
.btn-mini:hover { background: #eef2f6; }
.summary { display: flex; flex-wrap: wrap; gap: 8px; }
.stat {
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.2);
  padding: 6px 12px;
  min-width: 88px;
  text-align: center;
}
.stat b { display: block; font-size: 20px; line-height: 1.2; }
.stat span { font-size: 12px; opacity: 0.85; }
.wrap { padding: 16px 20px 28px; max-width: 1400px; margin: 0 auto; }
.alert {
  background: #fef3c7;
  border: 1px solid #f59e0b;
  color: #92400e;
  padding: 10px 14px;
  margin-bottom: 14px;
  font-weight: 600;
}
.grid {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr;
  gap: 14px;
}
@media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  box-shadow: 0 1px 0 rgba(255,255,255,0.7) inset;
}
.panel h2 {
  margin: 0;
  padding: 10px 14px;
  font-size: 15px;
  background: #f3f6f9;
  border-bottom: 1px solid var(--line);
}
.panel-body { padding: 14px; }
.machine-tree { max-height: 420px; overflow: auto; border: 1px solid var(--line); padding: 8px; background: #fafbfc; }
.class-block { margin-bottom: 8px; }
.class-header, .sub-header { cursor: pointer; user-select: none; }
.class-title, .sub-title { font-weight: 700; }
.sub-container { margin-left: 18px; margin-top: 4px; }
.machine-container { margin-left: 18px; margin-top: 4px; }
.machine-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px dashed #e5eaf0;
  font-size: 14px;
}
.badge {
  display: inline-block;
  min-width: 42px;
  text-align: center;
  font-size: 12px;
  font-weight: 700;
  padding: 2px 6px;
  color: #fff;
}
.badge.online { background: var(--ok); }
.badge.offline { background: var(--offline); }
.btn-act {
  height: 24px;
  padding: 0 8px;
  font-size: 12px;
  font-weight: 700;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--text);
  cursor: pointer;
  flex-shrink: 0;
}
.btn-act.view { color: var(--accent2); }
.btn-act.log { color: #7c3aed; }
.btn-act.rdp { color: var(--accent); }
.btn-act.edit { color: #475569; }
.btn-act.del { color: var(--danger); }
.btn-act:disabled { opacity: 0.4; cursor: not-allowed; }
.shot-card { width: min(1100px, 96%); max-height: 92vh; display: flex; flex-direction: column; }
.shot-card:fullscreen, .shot-card:-webkit-full-screen {
  width: 100%;
  height: 100%;
  max-height: none;
  border: none;
}
.shot-img-wrap {
  overflow: auto;
  max-height: 72vh;
  min-height: 180px;
  background: #111;
  text-align: center;
  flex: 1;
  cursor: zoom-in;
}
.shot-card:fullscreen .shot-img-wrap, .shot-card:-webkit-full-screen .shot-img-wrap {
  max-height: none;
}
.shot-img-wrap img {
  height: auto;
  vertical-align: middle;
  transform-origin: top center;
}
.shot-img-wrap img.fit { max-width: 100%; width: 100%; }
.shot-status { color: var(--muted); font-size: 12px; }
.log-card { width: min(980px, 96%); max-height: 92vh; display: flex; flex-direction: column; }
.log-pre {
  flex: 1;
  overflow: auto;
  max-height: 72vh;
  min-height: 280px;
  margin: 0;
  padding: 12px 14px;
  background: #0f172a;
  color: #e2e8f0;
  font: 12px/1.5 Consolas, "Courier New", monospace;
  white-space: pre-wrap;
  word-break: break-all;
}
#log_date {
  height: 28px;
  min-width: 140px;
  border: 1px solid var(--line);
  background: #fff;
  padding: 0 8px;
}
.badge.queue { background: var(--accent2); }
.badge.busy { background: var(--warn); }
.badge.ip { background: #334155; font-weight: 500; min-width: auto; }
.badge.ver { background: #0a4d8c; min-width: auto; }
.badge.ver.old { background: #64748b; }
.m-name { min-width: 110px; }
.drive-picks { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; }
.drive-opt { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; cursor: pointer; }
.drive-opt input { margin: 0; }
.form-row { margin-bottom: 12px; }
.form-row label { display: block; font-weight: 700; margin-bottom: 4px; font-size: 13px; }
.form-row input[type="text"], .form-row select {
  width: 100%;
  height: 38px;
  border: 1px solid var(--line);
  padding: 0 10px;
  font-size: 14px;
  background: #fff;
}
.path-input-wrap {
  display: flex;
  gap: 0;
  align-items: stretch;
}
.path-input-wrap input[type="text"] {
  flex: 1;
  border-right: none;
  width: auto;
}
.btn-drop {
  width: 42px;
  height: 38px;
  border: 1px solid var(--line);
  background: #eef2f6;
  color: var(--text);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  padding: 0;
  flex-shrink: 0;
}
.btn-drop:hover { background: #e2e8f0; }
.cmd-preset-wrap { position: relative; }
.cmd-preset-menu {
  display: none;
  position: absolute;
  right: 0;
  top: 40px;
  z-index: 50;
  min-width: 220px;
  max-width: 100%;
  background: #fff;
  border: 1px solid var(--line);
  box-shadow: 0 8px 24px rgba(0,0,0,0.12);
}
.cmd-preset-menu.show { display: block; }
.cmd-preset-item {
  display: block;
  width: 100%;
  text-align: left;
  border: none;
  border-bottom: 1px solid #eef2f6;
  background: #fff;
  padding: 10px 12px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  color: var(--text);
  height: auto;
}
.cmd-preset-item:hover { background: #f3f6f9; }
.cmd-preset-item.danger { color: var(--danger); }
.cmd-preset-item small {
  display: block;
  margin-top: 2px;
  font-weight: 400;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(18, 48, 71, 0.45);
  z-index: 1000;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.modal-mask.show { display: flex; }
.modal-card {
  width: min(720px, 100%);
  max-height: 80vh;
  background: #fff;
  border: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  box-shadow: 0 12px 40px rgba(0,0,0,0.25);
}
.modal-head, .modal-foot {
  padding: 10px 12px;
  background: #f3f6f9;
  border-bottom: 1px solid var(--line);
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.modal-foot { border-bottom: none; border-top: 1px solid var(--line); }
.modal-path {
  flex: 1;
  font-size: 12px;
  color: var(--muted);
  word-break: break-all;
  min-width: 120px;
}
.browse-list {
  overflow: auto;
  max-height: 48vh;
  padding: 4px 0;
}
.browse-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  cursor: pointer;
  font-size: 14px;
  border-bottom: 1px dashed #eef2f6;
  white-space: nowrap;
}
.browse-item:hover { background: #f3f6f9; }
.browse-item.active { background: #dbeafe; }
.browse-item .ico {
  flex: 0 0 52px;
  width: 52px;
  text-align: center;
  font-size: 12px;
  font-weight: 700;
  line-height: 22px;
  white-space: nowrap;
  color: #fff;
  background: var(--accent2);
}
.browse-item.file .ico {
  background: #64748b;
  color: #fff;
}
.browse-item .name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.browse-empty, .browse-error {
  padding: 20px 14px;
  color: var(--muted);
  text-align: center;
}
.browse-error { color: var(--danger); font-weight: 600; }
.hint { color: var(--muted); font-size: 12px; margin-top: 4px; }
.btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
button, .btn {
  height: 40px;
  padding: 0 16px;
  border: 1px solid transparent;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-secondary { background: #eef2f6; color: var(--text); border-color: var(--line); }
.btn-danger { background: #fff; color: var(--danger); border-color: #f1a8a8; }
.logs-panel { margin-top: 14px; }
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.toolbar input, .toolbar select { height: 34px; border: 1px solid var(--line); padding: 0 8px; }
.log-container { height: 360px; overflow: auto; border: 1px solid var(--line); }
table { border-collapse: collapse; width: 100%; table-layout: fixed; }
th, td { border-bottom: 1px solid #e5eaf0; padding: 8px; text-align: left; word-break: break-all; font-size: 13px; }
th { background: #f3f6f9; position: sticky; top: 0; z-index: 1; }
tr:nth-child(even) { background: #fafbfc; }
.status-ok { color: var(--ok); font-weight: 700; }
.status-fail { color: var(--danger); font-weight: 700; }
.status-progress { color: var(--warn); font-weight: 700; }
.status-cancel { color: #64748b; font-weight: 700; }
.bar {
  width: 100%;
  height: 16px;
  background: #e5eaf0;
  border: 1px solid #d0d7e0;
  overflow: hidden;
  position: relative;
}
.bar > i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #0b6e4f, #17a27a);
}
.bar.indeterminate > i {
  width: 35% !important;
  animation: bar-indeterminate 1.2s ease-in-out infinite;
}
@keyframes bar-indeterminate {
  0% { transform: translateX(-120%); }
  100% { transform: translateX(320%); }
}
.bar > span {
  position: absolute;
  inset: 0;
  text-align: center;
  font-size: 11px;
  line-height: 16px;
  font-weight: 700;
  color: #123047;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 4px;
}
input[type="checkbox"]:disabled { opacity: 0.45; cursor: not-allowed; }
.hits-box { margin-top: 6px; font-size: 12px; }
.hits-box summary { cursor: pointer; color: var(--accent); font-weight: 700; }
.hits-list { margin: 6px 0 0; padding: 0; list-style: none; max-height: 220px; overflow: auto; }
.hits-list li {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  padding: 4px 0;
  border-bottom: 1px dashed #eef2f6;
}
.hit-kw { color: var(--accent); font-weight: 700; flex-shrink: 0; }
.hit-path { flex: 1; min-width: 0; word-break: break-all; }
.hit-meta { color: var(--muted); flex-shrink: 0; white-space: nowrap; }
.file-dl { margin-top: 4px; }
.file-dl a { color: var(--accent); font-weight: 700; }
</style>
</head>
<body>
<div class="topbar">
  <h1>工厂同步控制台</h1>
  <div class="topbar-right">
    <div class="summary" id="summary">
      <div class="stat"><b id="s_online">0</b><span>在线</span></div>
      <div class="stat"><b id="s_offline">0</b><span>离线</span></div>
      <div class="stat"><b id="s_busy">0</b><span>执行中</span></div>
      <div class="stat"><b id="s_queued">0</b><span>排队</span></div>
    </div>
    <button type="button" class="btn-top" id="btn_global_refresh" title="刷新整个页面">刷新页面</button>
  </div>
</div>
<div style="background:#0f2433;color:#9fb3c8;font-size:12px;padding:6px 20px;">
  操作审计已开启：访问 IP 与下发记录保存在服务器 log/audit/ 目录（按日归档）。Agent 升级包放在 packages\ 目录。可按关键字搜索机台本机文件（MFT），再点命中项回传（单文件约 80MB 内）。
</div>

<div class="wrap">
  {% if error %}
  <div class="alert">{{ error }}</div>
  {% endif %}

  <form method="post" id="task_form">
  <div class="grid">
    <div class="panel">
      <div class="panel-head">
        <span>机台选择（点击分类展开，离线不可选）</span>
        <button type="button" class="btn-mini" id="btn_machine_refresh" title="重新拉取机台列表，新上线机会出现">刷新机台</button>
      </div>
      <div class="panel-body">
        <div class="machine-tree" id="machine_tree">
          {% for cls, subs in classes.items() %}
          <div class="class-block" data-class="{{ cls }}">
            <div>
              <input type="checkbox" class="class_chk" data-class="{{ cls }}">
              <span class="class-header class-title">{{ cls }}</span>
            </div>
            <div class="sub-container" style="display:none;">
              {% for sub, machines in subs.items() %}
              <div>
                <input type="checkbox" class="sub_chk" data-class="{{ cls }}" value="{{ sub }}">
                <span class="sub-header sub-title">{{ sub }}</span>
                <div class="machine-container" data-class="{{ cls }}" data-sub="{{ sub }}" style="display:none;">
                  {% for m in machines %}
                  {% set is_online = (time.time() - agents_status[m]['last_seen']) < online_threshold %}
                  <div class="machine-row" data-machine="{{ m }}">
                    <input type="checkbox" class="machine_chk" data-class="{{ cls }}" data-sub="{{ sub }}"
                           name="machines" value="{{ m }}" {{ '' if is_online else 'disabled' }}>
                    <span class="m-name">{{ m }}</span>
                    <span class="badge {{ 'online' if is_online else 'offline' }}">{{ '在线' if is_online else '离线' }}</span>
                    <span class="badge ip ip-badge" style="display:none;"></span>
                    <span class="badge ver ver-badge" style="display:none;"></span>
                    <span class="badge queue q-badge" style="display:none;">排队0</span>
                    <span class="badge busy b-badge" style="display:none;">执行中</span>
                    <button type="button" class="btn-act view btn-view" title="查看屏幕（需新版 Agent）">查看屏幕</button>
                    <button type="button" class="btn-act log btn-log" title="查阅该机台 Agent 本地日志（最近 30 天）">日志</button>
                    <button type="button" class="btn-act rdp btn-rdp" title="下载 RDP，用本机 mstsc 打开并填入 IP">远程</button>
                    <button type="button" class="btn-act edit btn-rename" title="修改设备编码">改编码</button>
                    <button type="button" class="btn-act del btn-del" title="删除离线机台">删除</button>
                  </div>
                  {% endfor %}
                </div>
              </div>
              {% endfor %}
            </div>
          </div>
          {% endfor %}
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>任务下发</h2>
      <div class="panel-body">
        <div class="form-row">
          <label>任务类型</label>
          <select name="action" id="action_sel">
            <option value="deploy_folder">复制文件/文件夹</option>
            <option value="run_command">执行命令</option>
            <option value="self_update">推送 Agent 升级</option>
            <option value="search_files">搜索文件</option>
            <option value="collect_file">回传文件</option>
          </select>
        </div>

        <div id="deploy_fields">
          <div class="form-row">
            <label>公盘路径</label>
            <div class="path-input-wrap">
              <input type="text" name="source" id="source_inp" value="" placeholder="请选择公盘文件或文件夹">
              <button type="button" class="btn-drop" id="browse_btn" title="浏览公盘">▼</button>
            </div>
            <div class="hint">点右侧 ▼ 浏览 {{ share_root }}（账号 1 / 密码 1），也可手工填写。必选文件或文件夹。</div>
          </div>
          <div class="form-row">
            <label>目标路径</label>
            <input type="text" name="destination" id="dest_inp" value="D:\\TE">
            <div class="hint">公盘选的是文件夹时，会自动跟上最后一级目录名，例如 D:\\TE\\SL003MT4。选文件则保持目标根目录不变。</div>
          </div>
          <div class="form-row">
            <label>模式</label>
            <select name="mode" id="mode_sel">
              <option value="overwrite">覆盖（只复制/覆盖，不删本地多余）</option>
              <option value="mirror">镜像（与源一致，会删除本地多余文件）</option>
            </select>
            <div class="hint">镜像模式会删除目标中源没有的文件，下发前会再次确认。</div>
          </div>
        </div>

        <div id="cmd_fields" style="display:none;">
          <div class="form-row">
            <label>远程 CMD 命令</label>
            <div class="path-input-wrap cmd-preset-wrap">
              <input type="text" name="command" id="command_inp" value='start "" "D:\TE\填写exe路径"' placeholder="可手工填写，或点右侧 ▼ 选常用命令">
              <button type="button" class="btn-drop" id="cmd_preset_btn" title="常用命令">▼</button>
              <div class="cmd-preset-menu" id="cmd_preset_menu"></div>
            </div>
            <div class="hint">点 ▼ 可一键填入常用命令，填入后仍可修改路径/文件名再下发。</div>
          </div>
        </div>

        <div id="update_fields" style="display:none;">
          <div class="form-row">
            <label>升级包版本</label>
            <select name="update_version" id="update_version_sel">
              <option value="">（请先上传升级包）</option>
            </select>
            <div class="hint">网页升级与手工安装用同一份包。机台按系统自动选 Win7 或 Win10 运行时。仅对已上报版本号的 Agent 生效。新安装包默认装到当前用户 %LOCALAPPDATA%\agent。</div>
          </div>
          <div class="form-row">
            <label>上传新包（一份 zip：启动器 + win10 + win7，机台自适应）</label>
            <input type="file" id="pkg_file" accept=".zip">
            <input type="text" id="pkg_version" placeholder="版本号，如 2.0.0（可从文件名识别）" style="margin-top:6px;">
            <input type="text" id="pkg_notes" placeholder="备注（可选）" style="margin-top:6px;">
            <div class="btn-row">
              <button type="button" class="btn-secondary" id="pkg_upload_btn">上传升级包</button>
              <button type="button" class="btn-secondary" id="btn_select_outdated">勾选可升级机台</button>
            </div>
            <div class="hint" id="pkg_upload_msg"></div>
          </div>
        </div>

        <div id="search_fields" style="display:none;">
          <div class="form-row">
            <label>关键字</label>
            <input type="text" name="keyword" id="keyword_inp" value="" placeholder="多个关键字用逗号或空格分隔，如 RustDesk, host">
            <div class="hint">任一关键字命中即可。每个至少 2 个字符，总共最多返回 200 条。两侧都走 MFT（需管理员），不落地独立扫描 exe。</div>
          </div>
          <div class="form-row">
            <label>盘符</label>
            <div class="drive-picks">
              <label class="drive-opt"><input type="checkbox" class="drive_pick" name="drive" value="D:" checked> D:</label>
              <label class="drive-opt"><input type="checkbox" class="drive_pick" name="drive" value="C:"> C:</label>
              <label class="drive-opt"><input type="checkbox" class="drive_pick" name="drive" value="E:"> E:</label>
              <label class="drive-opt"><input type="checkbox" id="drive_all" name="drive" value="all"> 全部本地磁盘</label>
            </div>
            <div class="hint">可多选。默认 D:。选「全部本地磁盘」时由机台扫描本机固定盘和移动盘（不含光驱/网络盘）。</div>
          </div>
          <div class="form-row">
            <label>最多返回</label>
            <input type="number" name="max_hits" id="max_hits_inp" value="200" min="1" max="500" style="width:100px;">
            <div class="hint">默认 200，上限 500。超出时只返回前 N 条。</div>
          </div>
        </div>

        <div id="collect_fields" style="display:none;">
          <div class="form-row">
            <label>本机绝对路径</label>
            <input type="text" name="collect_path" id="collect_path_inp" value="" placeholder="例如 D:\\TE\\xxx.txt">
            <div class="hint">只收本地盘文件（X:\\...），拒绝 UNC 和目录，单文件约 80MB 以内。也可在搜索命中里点「回传」。</div>
          </div>
        </div>

        <div class="btn-row">
          <button type="submit" class="btn-primary" id="submit_btn">下发任务</button>
          <button type="button" class="btn-danger" id="btn_cancel_running">取消进行中</button>
          <button type="submit" class="btn-secondary" name="clear_done" value="1">清理已完成</button>
          <button type="submit" class="btn-danger" name="clear_logs" value="1"
                  onclick="return confirm('将归档并清空全部日志，确定？')">清理全部日志</button>
          <a class="btn btn-secondary" href="/export_logs">导出日志</a>
        </div>
      </div>
    </div>
  </div>
  </form>

  <div class="panel logs-panel">
    <h2>执行日志（最新在上）</h2>
    <div class="panel-body">
      <div class="toolbar">
        <input type="text" id="filter_machine" placeholder="按机台或 IP 筛选">
        <select id="filter_status">
          <option value="">全部状态</option>
          <option value="progress">进行中</option>
          <option value="success">成功</option>
          <option value="cancelled">已取消</option>
          <option value="error">失败</option>
        </select>
        <span class="hint" id="log_count"></span>
      </div>
      <div class="hint" id="duration_summary" style="margin:0 0 10px 0;"></div>
      <div class="log-container">
        <table>
          <colgroup>
            <col style="width:150px;">
            <col style="width:120px;">
            <col style="width:80px;">
            <col style="width:200px;">
            <col style="width:90px;">
            <col>
            <col style="width:72px;">
          </colgroup>
          <thead>
            <tr><th>时间</th><th>机器</th><th>状态</th><th>进度</th><th>耗时</th><th>信息</th><th>操作</th></tr>
          </thead>
          <tbody id="results_body"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="modal-mask" id="browse_modal">
  <div class="modal-card">
    <div class="modal-head">
      <button type="button" class="btn-secondary" id="browse_up">上级</button>
      <button type="button" class="btn-secondary" id="browse_root">根目录</button>
      <div class="modal-path" id="browse_path"></div>
      <button type="button" class="btn-secondary" id="browse_close">关闭</button>
    </div>
    <div class="browse-list" id="browse_list"></div>
    <div class="modal-foot">
      <button type="button" class="btn-secondary" id="browse_select_dir">选择当前文件夹</button>
      <button type="button" class="btn-primary" id="browse_select_item" disabled>选择选中项</button>
    </div>
  </div>
</div>

<div class="modal-mask" id="shot_modal">
  <div class="modal-card shot-card" id="shot_card">
    <div class="modal-head">
      <div class="modal-path" id="shot_title">查看屏幕</div>
      <span class="shot-status" id="shot_status"></span>
      <button type="button" class="btn-secondary" id="shot_zoom_out" title="缩小">－</button>
      <button type="button" class="btn-secondary" id="shot_zoom_in" title="放大">＋</button>
      <button type="button" class="btn-secondary" id="shot_zoom_fit" title="适应窗口">适应</button>
      <button type="button" class="btn-secondary" id="shot_zoom_100" title="实际大小">100%</button>
      <button type="button" class="btn-secondary" id="shot_fullscreen" title="全屏">全屏</button>
      <button type="button" class="btn-secondary" id="shot_refresh">刷新</button>
      <button type="button" class="btn-secondary" id="shot_rdp">远程桌面</button>
      <button type="button" class="btn-secondary" id="shot_close">关闭</button>
    </div>
    <div class="shot-img-wrap" id="shot_wrap">
      <div class="browse-empty" id="shot_empty">等待截图...</div>
      <img id="shot_img" class="fit" alt="屏幕截图" style="display:none;">
    </div>
    <div class="modal-foot">
      <div class="hint">查看屏幕：打开后会连续截图，约每秒更新。滚轮或＋－可缩放，全屏可铺满显示器。远程：下载 .rdp 用本机 mstsc 打开（目标机需已开启远程桌面）。</div>
    </div>
  </div>
</div>

<div class="modal-mask" id="log_modal">
  <div class="modal-card log-card">
    <div class="modal-head">
      <div class="modal-path" id="log_title">机台日志</div>
      <select id="log_date" title="选择日期"></select>
      <span class="shot-status" id="log_status"></span>
      <button type="button" class="btn-secondary" id="log_refresh">刷新</button>
      <button type="button" class="btn-secondary" id="log_download">下载</button>
      <button type="button" class="btn-secondary" id="log_close">关闭</button>
    </div>
    <pre id="log_pre" class="log-pre">等待拉取日志...</pre>
    <div class="modal-foot">
      <div class="hint">Agent 在本机保留最近 30 天日志。复制进行中也可拉取。切换日期会向该机台再取一次；已缓存的日期可直接查阅。</div>
    </div>
  </div>
</div>

<script>
const SHARE_ROOT = {{ share_root|tojson }};
const CMD_PRESETS = {{ cmd_presets|tojson }};
let browseCurrent = SHARE_ROOT;
let browseSelected = null;

(function buildCmdPresetMenu() {
  const menu = document.getElementById('cmd_preset_menu');
  menu.innerHTML = '';
  (CMD_PRESETS || []).forEach(p => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cmd-preset-item' + (p.danger ? ' danger' : '');
    btn.innerHTML = '<span></span><small></small>';
    btn.querySelector('span').textContent = p.name || '';
    btn.querySelector('small').textContent = p.command || '';
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      const cmd = p.command || '';
      if (p.danger && !confirm('该命令可能删除文件/文件夹，确认填入？\n\n' + cmd)) return;
      document.getElementById('command_inp').value = cmd;
      menu.classList.remove('show');
      document.getElementById('command_inp').focus();
    });
    menu.appendChild(btn);
  });
})();

function bindTreeEvents() {
  document.querySelectorAll('.class-header').forEach(h => {
    h.onclick = function () {
      const subDiv = this.closest('.class-block').querySelector('.sub-container');
      if (subDiv) subDiv.style.display = subDiv.style.display === 'none' ? 'block' : 'none';
    };
  });
  document.querySelectorAll('.sub-header').forEach(h => {
    h.onclick = function () {
      const mDiv = this.parentElement.querySelector('.machine-container');
      if (mDiv) mDiv.style.display = mDiv.style.display === 'none' ? 'block' : 'none';
    };
  });
  document.querySelectorAll('.class_chk').forEach(cls => {
    cls.onchange = function () {
      const cls_val = this.dataset.class;
      const checked = this.checked;
      document.querySelectorAll(`.sub_chk[data-class='${cls_val}']`).forEach(el => {
        el.checked = checked; el.indeterminate = false;
      });
      document.querySelectorAll(`.machine_chk[data-class='${cls_val}']:not(:disabled)`).forEach(el => {
        el.checked = checked; el.indeterminate = false;
      });
    };
  });
  document.querySelectorAll('.sub_chk').forEach(sub => {
    sub.onchange = function () {
      const cls_val = this.dataset.class;
      const sub_val = this.value;
      const checked = this.checked;
      document.querySelectorAll(`.machine_chk[data-class='${cls_val}'][data-sub='${sub_val}']:not(:disabled)`).forEach(m => {
        m.checked = checked; m.indeterminate = false;
      });
      updateClassState(cls_val);
    };
  });
  document.querySelectorAll('.machine_chk').forEach(m => {
    m.onchange = function () {
      updateSubState(this.dataset.class, this.dataset.sub);
      updateClassState(this.dataset.class);
    };
  });
}

function updateSubState(cls_val, sub_val) {
  const machines = document.querySelectorAll(`.machine_chk[data-class='${cls_val}'][data-sub='${sub_val}']:not(:disabled)`);
  const sub = document.querySelector(`.sub_chk[data-class='${cls_val}'][value='${sub_val}']`);
  if (!sub) return;
  const checkedCount = Array.from(machines).filter(m => m.checked).length;
  if (machines.length === 0) { sub.checked = false; sub.indeterminate = false; }
  else if (checkedCount === machines.length) { sub.checked = true; sub.indeterminate = false; }
  else if (checkedCount === 0) { sub.checked = false; sub.indeterminate = false; }
  else { sub.checked = false; sub.indeterminate = true; }
}

function updateClassState(cls_val) {
  const subs = document.querySelectorAll(`.sub_chk[data-class='${cls_val}']`);
  const cls = document.querySelector(`.class_chk[data-class='${cls_val}']`);
  if (!cls) return;
  const checkedCount = Array.from(subs).filter(s => s.checked).length;
  const indeterminateCount = Array.from(subs).filter(s => s.indeterminate).length;
  if (subs.length && checkedCount === subs.length) { cls.checked = true; cls.indeterminate = false; }
  else if (checkedCount === 0 && indeterminateCount === 0) { cls.checked = false; cls.indeterminate = false; }
  else { cls.checked = false; cls.indeterminate = true; }
}

function toggleActionFields() {
  const action = document.getElementById('action_sel').value;
  document.getElementById('deploy_fields').style.display = action === 'deploy_folder' ? 'block' : 'none';
  document.getElementById('cmd_fields').style.display = action === 'run_command' ? 'block' : 'none';
  document.getElementById('update_fields').style.display = action === 'self_update' ? 'block' : 'none';
  document.getElementById('search_fields').style.display = action === 'search_files' ? 'block' : 'none';
  document.getElementById('collect_fields').style.display = action === 'collect_file' ? 'block' : 'none';
}

document.getElementById('action_sel').addEventListener('change', toggleActionFields);

function syncDrivePicks() {
  const all = document.getElementById('drive_all');
  const on = !!(all && all.checked);
  const picks = document.querySelectorAll('.drive_pick');
  picks.forEach(el => {
    el.disabled = on;
    if (on) el.checked = false;
  });
  if (!on && !Array.from(picks).some(el => el.checked)) {
    const d = document.querySelector('.drive_pick[value="D:"]');
    if (d) d.checked = true;
  }
}
const driveAll = document.getElementById('drive_all');
if (driveAll) driveAll.addEventListener('change', syncDrivePicks);
document.querySelectorAll('.drive_pick').forEach(el => {
  el.addEventListener('change', function () {
    if (this.checked && driveAll) driveAll.checked = false;
    syncDrivePicks();
  });
});

const cmdPresetBtn = document.getElementById('cmd_preset_btn');
const cmdPresetMenu = document.getElementById('cmd_preset_menu');
cmdPresetBtn.addEventListener('click', function (e) {
  e.stopPropagation();
  cmdPresetMenu.classList.toggle('show');
});
document.addEventListener('click', function () {
  cmdPresetMenu.classList.remove('show');
});

function openBrowseModal() {
  document.getElementById('browse_modal').classList.add('show');
  const cur = (document.getElementById('source_inp').value || '').trim();
  const start = cur && cur.toLowerCase().indexOf(SHARE_ROOT.toLowerCase()) === 0 ? cur : SHARE_ROOT;
  loadBrowse(start);
}

const DEFAULT_DEST_ROOT = 'D:\\TE';
let destRoot = DEFAULT_DEST_ROOT;
let destFollowLeaf = '';

function normWinPath(p) {
  p = String(p || '').trim().replace(/^["']|["']$/g, '').replace(/\//g, '\\');
  if (!p) return '';
  if (p.indexOf('\\\\') === 0) {
    return '\\\\' + p.slice(2).replace(/\\+/g, '\\').replace(/\\+$/, '');
  }
  return p.replace(/\\+/g, '\\').replace(/\\+$/, '');
}
function pathLeaf(p) {
  const n = normWinPath(p);
  const i = n.lastIndexOf('\\');
  return i < 0 ? n : n.slice(i + 1);
}
function pathParent(p) {
  const n = normWinPath(p);
  const i = n.lastIndexOf('\\');
  if (i < 0) return n;
  if (n.length >= 2 && n.charAt(1) === ':' && i === 2) return n.slice(0, 3);
  return n.slice(0, i);
}
function looksLikeFileName(name, type) {
  if (type === 'file') return true;
  if (type === 'dir') return false;
  return /\.[A-Za-z0-9]{1,8}$/.test(name || '');
}
function followDestForSource(src, type) {
  src = normWinPath(src);
  const leaf = pathLeaf(src);
  if (!leaf || looksLikeFileName(leaf, type)) return;
  const destEl = document.getElementById('dest_inp');
  const dest = normWinPath(destEl.value);
  if (destFollowLeaf && dest.toLowerCase().endsWith('\\' + destFollowLeaf.toLowerCase())) {
    destRoot = pathParent(dest) || destRoot;
  } else if (dest && dest.toLowerCase() !== normWinPath(destRoot).toLowerCase()) {
    destRoot = dest;
  }
  destRoot = normWinPath(destRoot) || DEFAULT_DEST_ROOT;
  destFollowLeaf = leaf;
  destEl.value = destRoot + '\\' + leaf;
}
function setSourcePath(path, type) {
  document.getElementById('source_inp').value = path;
  followDestForSource(path, type);
}
function closeBrowseModal() {
  document.getElementById('browse_modal').classList.remove('show');
  browseSelected = null;
}
function loadBrowse(path) {
  const list = document.getElementById('browse_list');
  const pathEl = document.getElementById('browse_path');
  list.innerHTML = '<div class="browse-empty">加载中...</div>';
  pathEl.textContent = path || '';
  browseSelected = null;
  document.getElementById('browse_select_item').disabled = true;
  fetch('/browse?path=' + encodeURIComponent(path || SHARE_ROOT))
    .then(r => r.json())
    .then(data => {
      if (!data.ok) {
        list.innerHTML = '<div class="browse-error">' + (data.error || '浏览失败') + '</div>';
        return;
      }
      browseCurrent = data.path;
      pathEl.textContent = data.path;
      const upBtn = document.getElementById('browse_up');
      upBtn.disabled = !data.parent;
      upBtn.dataset.parent = data.parent || '';
      if (!data.entries || !data.entries.length) {
        list.innerHTML = '<div class="browse-empty">（空目录）</div>';
        return;
      }
      list.innerHTML = '';
      data.entries.forEach(ent => {
        const row = document.createElement('div');
        row.className = 'browse-item' + (ent.type === 'file' ? ' file' : '');
        row.dataset.path = ent.path;
        row.dataset.type = ent.type;
        row.innerHTML = '<span class="ico">' + (ent.type === 'dir' ? '目录' : '文件') + '</span><span class="name">' + esc(ent.name) + '</span>';
        row.onclick = function () {
          list.querySelectorAll('.browse-item').forEach(x => x.classList.remove('active'));
          row.classList.add('active');
          browseSelected = { path: ent.path, type: ent.type };
          document.getElementById('browse_select_item').disabled = false;
        };
        row.ondblclick = function () {
          if (ent.type === 'dir') loadBrowse(ent.path);
          else {
            setSourcePath(ent.path, 'file');
            closeBrowseModal();
          }
        };
        list.appendChild(row);
      });
    })
    .catch(err => {
      list.innerHTML = '<div class="browse-error">请求失败: ' + err + '</div>';
    });
}
document.getElementById('browse_btn').addEventListener('click', openBrowseModal);
document.getElementById('browse_close').addEventListener('click', closeBrowseModal);
document.getElementById('browse_modal').addEventListener('click', function (e) {
  if (e.target === this) closeBrowseModal();
});
document.getElementById('browse_up').addEventListener('click', function () {
  if (this.dataset.parent) loadBrowse(this.dataset.parent);
});
document.getElementById('browse_root').addEventListener('click', function () {
  loadBrowse(SHARE_ROOT);
});
document.getElementById('browse_select_dir').addEventListener('click', function () {
  setSourcePath(browseCurrent, 'dir');
  closeBrowseModal();
});
document.getElementById('browse_select_item').addEventListener('click', function () {
  if (!browseSelected) return;
  setSourcePath(browseSelected.path, browseSelected.type);
  closeBrowseModal();
});
document.getElementById('source_inp').addEventListener('change', function () {
  followDestForSource(this.value);
});
document.getElementById('source_inp').addEventListener('blur', function () {
  followDestForSource(this.value);
});
document.getElementById('dest_inp').addEventListener('change', function () {
  const dest = normWinPath(this.value);
  const srcLeaf = pathLeaf(document.getElementById('source_inp').value);
  if (srcLeaf && dest.toLowerCase().endsWith('\\' + srcLeaf.toLowerCase())) {
    destRoot = pathParent(dest) || destRoot;
    destFollowLeaf = srcLeaf;
  } else {
    destRoot = dest || DEFAULT_DEST_ROOT;
    destFollowLeaf = '';
  }
});
followDestForSource(document.getElementById('source_inp').value);

document.getElementById('task_form').addEventListener('submit', function (e) {
  const submitter = e.submitter || document.activeElement;
  if (submitter && (submitter.name === 'clear_logs' || submitter.name === 'clear_done')) return;

  const action = document.getElementById('action_sel').value;
  const selected = document.querySelectorAll('.machine_chk:checked:not(:disabled)');
  if (!selected.length) {
    e.preventDefault();
    alert('请至少选择一台在线机台');
    return;
  }
  if (action === 'deploy_folder') {
    document.getElementById('source_inp').value = normWinPath(document.getElementById('source_inp').value);
    document.getElementById('dest_inp').value = normWinPath(document.getElementById('dest_inp').value);
    if (!document.getElementById('source_inp').value.trim()) {
      e.preventDefault();
      alert('请选择公盘文件或文件夹');
      return;
    }
    if (!document.getElementById('dest_inp').value.trim()) {
      e.preventDefault();
      alert('请填写目标路径');
      return;
    }
    if (document.getElementById('mode_sel').value === 'mirror') {
      if (!confirm('镜像模式会删除目标目录中源没有的文件/文件夹，确认下发？')) {
        e.preventDefault();
        return;
      }
    }
  }
  if (action === 'run_command' && !document.getElementById('command_inp').value.trim()) {
    e.preventDefault();
    alert('请填写要执行的命令');
    return;
  }
  if (action === 'self_update') {
    const ver = document.getElementById('update_version_sel').value;
    if (!ver) {
      e.preventDefault();
      alert('请先上传并选择要推送的 Agent 版本');
      return;
    }
    if (!confirm('将向选中机台推送 Agent ' + ver + '。旧版（无版本号）会被跳过。同一份 zip 在 Win7/Win10 上自动选用运行时后重启。确认？')) {
      e.preventDefault();
      return;
    }
  }
  if (action === 'search_files') {
    const kw = (document.getElementById('keyword_inp').value || '').trim();
    const allDrv = document.getElementById('drive_all');
    const picks = Array.from(document.querySelectorAll('.drive_pick:checked')).map(el => el.value);
    const words = kw.split(/[,;\s\u3000\uFF0C\u3001]+/).map(s => s.replace(/^["']|["']$/g, '')).filter(s => s.length >= 2);
    if (!words.length) {
      e.preventDefault();
      alert('请填写关键字，多个用逗号或空格分隔，每个至少 2 个字符');
      return;
    }
    if (!(allDrv && allDrv.checked) && !picks.length) {
      e.preventDefault();
      alert('请至少选择一个盘符，或选全部本地磁盘');
      return;
    }
  }
  if (action === 'collect_file') {
    const p = (document.getElementById('collect_path_inp').value || '').trim();
    if (!/^[A-Za-z]:[\\/]/.test(p) || p.indexOf('\\\\') === 0) {
      e.preventDefault();
      alert('请填写本地盘绝对路径（如 D:\\TE\\a.txt），不能是 UNC 或目录');
      return;
    }
  }
  const btn = document.getElementById('submit_btn');
  btn.disabled = true;
  btn.textContent = '下发中...';
});

let allResults = [];

function renderProgress(r) {
  if (r.status === 'success') {
    return '<div class="bar"><i style="width:100%"></i><span>已完成</span></div>';
  }
  if (r.status === 'error') {
    return '<div class="bar"><i style="width:100%;background:#dc2626"></i><span>失败</span></div>';
  }
  const msg = String(r.message || '');
  const p = (r.progress === undefined || r.progress === null || r.progress === '') ? 0 : Number(r.progress);
  const pct = isNaN(p) ? 0 : Math.max(0, Math.min(100, p));
  let label = pct + '%';
  let indeterminate = false;

  if (r.copied_bytes != null && r.total_bytes) {
    label = pct + '% ' + formatBytes(r.copied_bytes) + '/' + formatBytes(r.total_bytes);
  } else {
    const mBytes = msg.match(/已复制\s+(\d+)\s*\/\s*(\d+)\s*bytes/i);
    const mCopy = msg.match(/复制中\s+(\S+)\s*\/\s*(\S+)/);
    if (mBytes) {
      label = pct + '% ' + formatBytes(mBytes[1]) + '/' + formatBytes(mBytes[2]);
    } else if (mCopy) {
      label = pct + '% ' + mCopy[1] + '/' + mCopy[2];
    }
  }

  if (msg.indexOf('等待执行') >= 0) {
    label = '等待执行';
    indeterminate = pct <= 0;
  } else if (/统计源目录/.test(msg)) {
    const scan = msg.match(/已扫描\s+(\d+)\s*个文件\s+(\S+)/);
    label = scan ? ('统计中 ' + scan[1] + '个 ' + scan[2]) : '统计中';
    indeterminate = pct <= 0;
  } else if (/连接共享/.test(msg) && pct <= 0) {
    label = '连接共享';
    indeterminate = true;
  } else if (/开始复制/.test(msg) && pct <= 0) {
    label = msg.replace(/^开始复制[，,]\s*/, '准备复制 ');
    if (label.length > 22) label = '准备复制';
    indeterminate = true;
  } else if (msg.indexOf('清理多余') >= 0) {
    label = pct + '% 清理中';
  } else if (/即将重启替换|正在备份并替换|正在启动新 Agent|等待新进程启动/.test(msg)) {
    label = pct + '% 替换中';
  } else if (/扫描|MFT|过滤|遍历/.test(msg)) {
    label = pct > 0 ? (pct + '% 搜索中') : '搜索中';
    indeterminate = pct <= 0;
  } else if (/回传/.test(msg)) {
    label = pct > 0 ? (pct + '% 回传中') : '回传中';
    indeterminate = pct <= 0;
  } else if (/下载|校验|解压|升级|替换 Agent/.test(msg)) {
    label = pct + '% 升级中';
  } else if (Number(r.copied_files) > 0 && r.status === 'progress' && !(r.copied_bytes > 0 && r.total_bytes > 0) && msg.indexOf('复制中') < 0) {
    label = pct + '% 已处理' + r.copied_files + '个';
  } else if (msg.indexOf('已处理') >= 0 && msg.indexOf('复制中') < 0) {
    label = pct + '% 复制中';
  }

  const barClass = indeterminate ? 'bar indeterminate' : 'bar';
  const width = indeterminate ? 35 : pct;
  return `<div class="${barClass}" title="${esc(msg)}"><i style="width:${width}%"></i><span>${esc(label)}</span></div>`;
}

function formatBytes(n) {
  n = Number(n) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return (i === 0 ? Math.round(n) : n.toFixed(1)) + units[i];
}

function formatDurationSec(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  if (sec < 60) return sec + '秒';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return m + '分' + s + '秒';
  const h = Math.floor(m / 60);
  return h + '小时' + (m % 60) + '分' + s + '秒';
}

function rowDurationText(r) {
  if (r.status === 'success' || r.status === 'error' || r.status === 'cancelled') {
    if (r.duration_text) return String(r.duration_text).replace(/…$/, '');
    if (r.elapsed_sec !== undefined && r.elapsed_sec !== null && r.elapsed_sec !== '') {
      return formatDurationSec(r.elapsed_sec);
    }
    if (r.duration_sec !== undefined && r.duration_sec !== null && r.duration_sec !== '') {
      return formatDurationSec(r.duration_sec);
    }
    return '-';
  }
  if (r.elapsed_sec !== undefined && r.elapsed_sec !== null && r.elapsed_sec !== '') {
    return formatDurationSec(r.elapsed_sec) + '…';
  }
  if (r.duration_text) return r.duration_text;
  if (r.duration_sec !== undefined && r.duration_sec !== null && r.duration_sec !== '') {
    return formatDurationSec(r.duration_sec);
  }
  // 进行中且尚无实测秒数：从首次执行/下发时间起算
  if (r.status === 'progress') {
    const base = r.exec_ts || r.start_ts;
    if (base) return formatDurationSec((Date.now() / 1000) - Number(base)) + '…';
  }
  return '-';
}

function pickLatestBatch(rows) {
  if (!rows.length) return [];
  let maxTs = -1;
  rows.forEach(r => {
    const ts = Number(r.start_ts) || 0;
    if (ts > maxTs) maxTs = ts;
  });
  if (maxTs < 0) return [];
  const newest = rows.filter(r => Math.abs((Number(r.start_ts) || 0) - maxTs) <= 3);
  const bid = (newest.find(r => r.batch_id) || {}).batch_id;
  if (bid) return rows.filter(r => r.batch_id === bid);
  return newest;
}

function buildDurationSummary(allRows) {
  const machineFilter = (document.getElementById('filter_machine').value || '').trim().toLowerCase();
  const scoped = machineFilter
    ? allRows.filter(r => String(r.machine || '').toLowerCase().indexOf(machineFilter) >= 0)
    : allRows;
  const batch = pickLatestBatch(scoped);
  if (!batch.length) return '耗时统计：暂无本次提交记录';

  const byMachine = {};
  batch.forEach(r => { byMachine[r.machine || '-'] = r; });
  const names = Object.keys(byMachine).sort();
  let done = 0;
  let running = 0;
  const doneSecs = [];
  const parts = names.map(m => {
    const r = byMachine[m];
    if (r.status === 'progress') {
      running += 1;
      return m + ' 进行中 ' + rowDurationText(r);
    }
    done += 1;
    const sec = Number(r.duration_sec);
    if (!isNaN(sec)) doneSecs.push(sec);
    return m + ' ' + rowDurationText(r);
  });
  const avgTxt = doneSecs.length
    ? '，平均' + formatDurationSec(doneSecs.reduce((a, b) => a + b, 0) / doneSecs.length)
    : '';
  const runTxt = running ? '，进行中' + running : '';
  return '耗时统计（本次提交）：共' + names.length + '台，完成' + done + runTxt + avgTxt + '　|　' + parts.join('　');
}

function statusText(r) {
  if (r.cancel_requested && r.status === 'progress') return '取消中';
  const map = { success: '成功', error: '失败', progress: '进行中', cancelled: '已取消' };
  return map[r.status] || (r.status || '');
}
function statusClass(r) {
  if (r.status === 'success') return 'status-ok';
  if (r.status === 'error') return 'status-fail';
  if (r.status === 'cancelled' || r.cancel_requested) return 'status-cancel';
  return 'status-progress';
}
function cancelTask(taskId, all) {
  const body = all ? { all: true } : { task_id: taskId };
  return fetch('/api/task_cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json().then(data => ({ ok: r.ok, data }))).then(res => {
    if (!res.data || !res.data.ok) {
      alert((res.data && res.data.error) || '取消失败');
      return;
    }
    fetchResults();
  }).catch(err => alert('取消失败: ' + err));
}

let lastResultsSig = '';
function resultRowKey(r) {
  return String(r.task_id || ((r.machine || '') + '|' + (r.time || '') + '|' + (r.message || '')));
}
function getHitsUiState() {
  const state = {};
  document.querySelectorAll('#results_body details.hits-box').forEach(el => {
    const tr = el.closest('tr');
    const key = tr && tr.dataset.resultKey;
    if (!key) return;
    const list = el.querySelector('.hits-list');
    state[key] = { open: !!el.open, scroll: list ? list.scrollTop : 0 };
  });
  return state;
}

function fetchResults() {
  fetch('/results_json').then(r => r.json()).then(data => {
    const next = data || [];
    const sig = JSON.stringify(next);
    allResults = next;
    if (sig === lastResultsSig) return;
    lastResultsSig = sig;
    drawResults();
  }).catch(() => {});
}

function drawResults() {
  const machineFilter = (document.getElementById('filter_machine').value || '').trim().toLowerCase();
  const statusFilter = document.getElementById('filter_status').value;
  const hitsUi = getHitsUiState();
  const tbody = document.querySelector('#results_body');
  tbody.innerHTML = '';
  let shown = 0;
  const filtered = [];
  allResults.slice().reverse().forEach(r => {
    if (machineFilter && String(r.machine || '').toLowerCase().indexOf(machineFilter) < 0
        && String(r.ip || '').toLowerCase().indexOf(machineFilter) < 0) return;
    if (statusFilter && r.status !== statusFilter) return;
    filtered.push(r);
    shown += 1;
    const tr = document.createElement('tr');
    const rowKey = resultRowKey(r);
    tr.dataset.resultKey = rowKey;
    const statusClassName = statusClass(r);
    const dur = rowDurationText(r);
    const tip = (r.start_time ? ('开始: ' + r.start_time) : '') + (r.end_time ? (' / 结束: ' + r.end_time) : '');
    const canCancel = r.status === 'progress' && r.task_id;
    const act = canCancel
      ? '<button type="button" class="btn-act del btn-cancel-task" data-task-id="' + esc(r.task_id) + '"'
        + (r.cancel_requested ? ' disabled' : '') + '>' + (r.cancel_requested ? '取消中' : '取消') + '</button>'
      : '';
    tr.innerHTML = `
      <td>${esc(r.time || '')}</td>
      <td>${esc(r.machine || '')}</td>
      <td class="${statusClassName}">${esc(statusText(r))}</td>
      <td>${renderProgress(r)}</td>
      <td title="${esc(tip)}">${esc(dur)}</td>
      <td class="log-msg">${renderLogMessage(r, hitsUi[rowKey])}</td>
      <td>${act}</td>`;
    tbody.appendChild(tr);
    const kept = hitsUi[rowKey];
    if (kept && kept.open && kept.scroll) {
      const list = tr.querySelector('.hits-list');
      if (list) list.scrollTop = kept.scroll;
    }
  });
  document.getElementById('log_count').textContent = `显示 ${shown} / 共 ${allResults.length} 条`;
  document.getElementById('duration_summary').textContent = buildDurationSummary(allResults);
}

document.getElementById('filter_machine').addEventListener('input', drawResults);
document.getElementById('filter_status').addEventListener('change', drawResults);
function renderLogMessage(r, hitsState) {
  let html = '<div>' + esc(r.message || '') + '</div>';
  if (r.file_url) {
    const name = r.file_name || '文件';
    const sizeTxt = r.file_size ? ('（' + formatBytes(r.file_size) + '）') : '';
    html += '<div class="file-dl"><a href="' + esc(r.file_url) + '">下载 ' + esc(name) + sizeTxt + '</a></div>';
  }
  const hits = Array.isArray(r.hits) ? r.hits : [];
  if (hits.length) {
    const n = Number(r.hit_count) || hits.length;
    const extra = r.truncated ? '（仅返回前 ' + hits.length + ' 条）' : '';
    const openAttr = hitsState && hitsState.open ? ' open' : '';
    html += '<details class="hits-box"' + openAttr + '><summary>命中 ' + n + ' 条' + extra + '</summary><ul class="hits-list">';
    hits.forEach(h => {
      const path = absHitPath((h && h.path) || '', r.drive);
      const name = (h && h.name) || '';
      const isDir = !!(h && h.is_dir);
      const sizeTxt = isDir ? '目录' : formatBytes(h && h.size);
      const enc = encodeURIComponent(path);
      const matched = (h && (h.matched || h.keyword)) || '';
      html += '<li>'
        + (matched ? '<span class="hit-kw">' + esc(matched) + '</span>' : '')
        + '<span class="hit-path">' + esc(path || name) + '</span>'
        + '<span class="hit-meta">' + esc(sizeTxt) + '</span>'
        + '<button type="button" class="btn-act btn-copy-path" data-path="' + enc + '">复制</button>';
      if (!isDir && path) {
        html += '<button type="button" class="btn-act btn-collect-file" data-machine="' + esc(r.machine || '')
          + '" data-path="' + enc + '" data-drive="' + encodeURIComponent(r.drive || '') + '">回传</button>';
      }
      html += '</li>';
    });
    html += '</ul></details>';
  }
  return html;
}

function fallbackCopyText(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (err) {}
  document.body.removeChild(ta);
  if (done) done();
}

function copyText(text, done) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopyText(text, done));
  } else {
    fallbackCopyText(text, done);
  }
}

function absHitPath(path, drive) {
  let p = String(path || '').replace(/\//g, '\\').trim();
  if (!p) return '';
  if (/^[A-Za-z]:\\/.test(p)) return p;
  let d = String(drive || '').trim().toUpperCase().replace(/[\\/]+$/, '');
  if (/^[A-Za-z]$/.test(d)) d += ':';
  if (!/^[A-Za-z]:$/.test(d)) {
    const m = d.match(/[A-Z]:/);
    d = m ? m[0] : 'D:';
  }
  if (p.charAt(0) === '\\') return d + p;
  return d + '\\' + p;
}
function readDataPath(el) {
  const raw = (el && el.getAttribute('data-path')) || '';
  try { return decodeURIComponent(raw); } catch (err) { return raw; }
}

function collectRemoteFile(machine, path, drive) {
  return fetch('/api/collect_file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machine: machine, path: path, drive: drive || '' })
  }).then(r => r.json().then(data => ({ ok: r.ok, data }))).then(res => {
    if (!res.data || !res.data.ok) {
      alert((res.data && res.data.error) || '回传任务下发失败');
      return;
    }
    fetchResults();
  }).catch(err => alert('回传任务下发失败: ' + err));
}

document.getElementById('results_body').addEventListener('click', function (e) {
  const copyBtn = e.target.closest('.btn-copy-path');
  if (copyBtn) {
    const path = readDataPath(copyBtn);
    if (!path) return;
    copyText(path, function () {
      copyBtn.textContent = '已复制';
      setTimeout(function () { copyBtn.textContent = '复制'; }, 1200);
    });
    return;
  }
  const collectBtn = e.target.closest('.btn-collect-file');
  if (collectBtn) {
    const machine = collectBtn.getAttribute('data-machine') || '';
    const path = readDataPath(collectBtn);
    let drive = collectBtn.getAttribute('data-drive') || '';
    try { drive = decodeURIComponent(drive); } catch (err) {}
    if (!machine || !path) return;
    if (!confirm('从 ' + machine + ' 回传该文件？\n' + path)) return;
    collectBtn.disabled = true;
    collectRemoteFile(machine, path, drive).finally(function () { collectBtn.disabled = false; });
    return;
  }
  const btn = e.target.closest('.btn-cancel-task');
  if (!btn || btn.disabled) return;
  const taskId = btn.getAttribute('data-task-id');
  if (!taskId) return;
  if (!confirm('确定取消该任务？正在复制的会通知机台停止。')) return;
  btn.disabled = true;
  cancelTask(taskId, false);
});
document.getElementById('btn_cancel_running').addEventListener('click', function () {
  const n = (allResults || []).filter(r => r.status === 'progress').length;
  if (!n) {
    alert('当前没有进行中的任务');
    return;
  }
  if (!confirm('确定取消全部 ' + n + ' 条进行中任务？')) return;
  cancelTask('', true);
});

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function getCheckedMachines() {
  return Array.from(document.querySelectorAll('.machine_chk:checked')).map(el => el.value);
}

function getExpandedState() {
  const classes = {};
  const subs = {};
  document.querySelectorAll('.class-block').forEach(block => {
    const cls = block.dataset.class;
    const subDiv = block.querySelector('.sub-container');
    if (subDiv && subDiv.style.display !== 'none') classes[cls] = true;
    block.querySelectorAll('.machine-container').forEach(mc => {
      if (mc.style.display !== 'none') subs[cls + '|' + mc.dataset.sub] = true;
    });
  });
  return { classes, subs };
}

function renderMachineTree(classes, agentMap, preserveChecked, expanded) {
  const tree = document.getElementById('machine_tree');
  const checkedSet = {};
  (preserveChecked || []).forEach(m => { checkedSet[m] = true; });
  const expCls = (expanded && expanded.classes) || {};
  const expSub = (expanded && expanded.subs) || {};

  const classNames = Object.keys(classes || {});
  if (!classNames.length) {
    tree.innerHTML = '<div class="browse-empty">暂无机台，等待 Agent 上线后点「刷新机台」</div>';
    bindTreeEvents();
    return;
  }

  let html = '';
  classNames.forEach(cls => {
    const subs = classes[cls] || {};
    const showSub = expCls[cls] ? 'block' : 'none';
    html += `<div class="class-block" data-class="${esc(cls)}">
      <div>
        <input type="checkbox" class="class_chk" data-class="${esc(cls)}">
        <span class="class-header class-title">${esc(cls)}</span>
      </div>
      <div class="sub-container" style="display:${showSub};">`;
    Object.keys(subs).forEach(sub => {
      const machines = subs[sub] || [];
      const showM = expSub[cls + '|' + sub] ? 'block' : 'none';
      html += `<div>
        <input type="checkbox" class="sub_chk" data-class="${esc(cls)}" value="${esc(sub)}">
        <span class="sub-header sub-title">${esc(sub)}</span>
        <div class="machine-container" data-class="${esc(cls)}" data-sub="${esc(sub)}" style="display:${showM};">`;
      machines.forEach(m => {
        const info = agentMap[m] || { online: false, queue: 0, busy: false };
        const disabled = info.online ? '' : 'disabled';
        const checked = (info.online && checkedSet[m]) ? 'checked' : '';
        const badgeCls = info.online ? 'online' : 'offline';
        const badgeTxt = info.online ? '在线' : '离线';
        const qStyle = info.queue > 0 ? 'inline-block' : 'none';
        const bStyle = info.busy ? 'inline-block' : 'none';
        const ip = info.ip || '';
        const ver = info.version || '';
        const ipStyle = ip ? 'inline-block' : 'none';
        const verTxt = ver || '旧版';
        const verCls = ver ? 'badge ver ver-badge' : 'badge ver ver-badge old';
        const osTag = info.os === 'win7' ? 'Win7' : (info.os === 'win10' ? 'Win10' : '');
        const osStyle = osTag ? 'inline-block' : 'none';
        html += `<div class="machine-row" data-machine="${esc(m)}">
          <input type="checkbox" class="machine_chk" data-class="${esc(cls)}" data-sub="${esc(sub)}"
                 name="machines" value="${esc(m)}" ${disabled} ${checked}>
          <span class="m-name">${esc(m)}</span>
          <span class="badge ${badgeCls}">${badgeTxt}</span>
          <span class="badge ip ip-badge" style="display:${ipStyle};">${esc(ip)}</span>
          <span class="${verCls}">${esc(verTxt)}</span>
          <span class="badge os-badge" style="display:${osStyle};">${esc(osTag)}</span>
          <span class="badge queue q-badge" style="display:${qStyle};">排队${info.queue || 0}</span>
          <span class="badge busy b-badge" style="display:${bStyle};">执行中</span>
          <button type="button" class="btn-act view btn-view" ${info.online && ver ? '' : 'disabled'} title="查看屏幕（需新版 Agent）">查看屏幕</button>
          <button type="button" class="btn-act log btn-log" ${info.online && ver ? '' : 'disabled'} title="查阅该机台 Agent 本地日志（最近 30 天）">日志</button>
          <button type="button" class="btn-act rdp btn-rdp" ${info.online && ip ? '' : 'disabled'} title="下载 RDP，用本机 mstsc 打开并填入 IP">远程</button>
          <button type="button" class="btn-act edit btn-rename" title="修改设备编码">改编码</button>
          <button type="button" class="btn-act del btn-del" ${info.online ? 'disabled' : ''} title="删除离线机台">删除</button>
        </div>`;
      });
      html += `</div></div>`;
    });
    html += `</div></div>`;
  });
  tree.innerHTML = html;
  bindTreeEvents();
}

function applyAgentStatus(agentMap) {
  document.querySelectorAll('.machine-row').forEach(row => {
    const name = row.dataset.machine;
    const info = agentMap[name];
    if (!info) return;
    const chk = row.querySelector('.machine_chk');
    const badge = row.querySelector('.badge.online, .badge.offline');
    const qBadge = row.querySelector('.q-badge');
    const bBadge = row.querySelector('.b-badge');
    const wasChecked = chk.checked;
    chk.disabled = !info.online;
    if (!info.online) chk.checked = false;
    else if (wasChecked) chk.checked = true;
    badge.className = 'badge ' + (info.online ? 'online' : 'offline');
    badge.textContent = info.online ? '在线' : '离线';
    if (info.queue > 0) {
      qBadge.style.display = 'inline-block';
      qBadge.textContent = '排队' + info.queue;
    } else {
      qBadge.style.display = 'none';
    }
    bBadge.style.display = info.busy ? 'inline-block' : 'none';
    const viewBtn = row.querySelector('.btn-view');
    const logBtn = row.querySelector('.btn-log');
    const rdpBtn = row.querySelector('.btn-rdp');
    const delBtn = row.querySelector('.btn-del');
    if (viewBtn) viewBtn.disabled = !(info.online && info.version);
    if (logBtn) logBtn.disabled = !(info.online && info.version);
    if (rdpBtn) rdpBtn.disabled = !(info.online && info.ip);
    if (delBtn) delBtn.disabled = !!info.online;
    const ipBadge = row.querySelector('.ip-badge');
    const verBadge = row.querySelector('.ver-badge');
    if (ipBadge) {
      if (info.ip) {
        ipBadge.style.display = 'inline-block';
        ipBadge.textContent = info.ip;
      } else {
        ipBadge.style.display = 'none';
      }
    }
    if (verBadge) {
      verBadge.style.display = 'inline-block';
      if (info.version) {
        verBadge.className = 'badge ver ver-badge';
        verBadge.textContent = info.version;
      } else {
        verBadge.className = 'badge ver ver-badge old';
        verBadge.textContent = '旧版';
      }
    }
    const osBadge = row.querySelector('.os-badge');
    if (osBadge) {
      if (info.os === 'win7' || info.os === 'win10') {
        osBadge.style.display = 'inline-block';
        osBadge.textContent = info.os === 'win7' ? 'Win7' : 'Win10';
      } else {
        osBadge.style.display = 'none';
      }
    }
  });
}

let knownMachines = new Set(Array.from(document.querySelectorAll('.machine-row')).map(r => r.dataset.machine));

let lastAgentMap = {};

function fetchAgents(forceRebuild) {
  return fetch('/agents_json').then(r => r.json()).then(data => {
    const s = data.summary || {};
    document.getElementById('s_online').textContent = s.online || 0;
    document.getElementById('s_offline').textContent = s.offline || 0;
    document.getElementById('s_busy').textContent = s.busy || 0;
    document.getElementById('s_queued').textContent = s.queued || 0;

    const map = {};
    const names = [];
    (data.agents || []).forEach(a => {
      map[a.name] = a;
      names.push(a.name);
    });
    lastAgentMap = map;

    let needRebuild = !!forceRebuild;
    if (!needRebuild) {
      if (knownMachines.size !== names.length) needRebuild = true;
      for (let i = 0; i < names.length; i++) {
        if (!knownMachines.has(names[i])) { needRebuild = true; break; }
      }
      if (!needRebuild) {
        knownMachines.forEach(function (n) {
          if (!map[n]) needRebuild = true;
        });
      }
    }

    if (needRebuild) {
      const checked = getCheckedMachines();
      const expanded = forceRebuild ? getExpandedState() : getExpandedState();
      renderMachineTree(data.classes || {}, map, checked, expanded);
      knownMachines = new Set(names);
    } else {
      applyAgentStatus(map);
    }
    return data;
  }).catch(() => null);
}

document.getElementById('btn_machine_refresh').addEventListener('click', function () {
  const btn = this;
  btn.disabled = true;
  btn.textContent = '刷新中...';
  fetchAgents(true).finally(() => {
    btn.disabled = false;
    btn.textContent = '刷新机台';
  });
});
document.getElementById('btn_global_refresh').addEventListener('click', function () {
  location.reload();
});

function parseVer(v) {
  const m = String(v || '').match(/(\d+)\.(\d+)\.(\d+)/);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}
function verLt(a, b) {
  const pa = parseVer(a), pb = parseVer(b);
  if (!pa || !pb) return false;
  for (let i = 0; i < 3; i++) {
    if (pa[i] < pb[i]) return true;
    if (pa[i] > pb[i]) return false;
  }
  return false;
}
function fillPackageSelect(packages) {
  const sel = document.getElementById('update_version_sel');
  const cur = sel.value;
  sel.innerHTML = '';
  if (!packages || !packages.length) {
    sel.innerHTML = '<option value="">（请先上传升级包）</option>';
    return;
  }
  packages.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.version;
    const sizeMb = p.size ? (' ' + (p.size / 1024 / 1024).toFixed(1) + 'MB') : '';
    opt.textContent = p.version + sizeMb + (p.notes ? (' ' + p.notes) : '');
    sel.appendChild(opt);
  });
  if (cur && packages.some(p => p.version === cur)) sel.value = cur;
}
function loadPackages() {
  return fetch('/agent/packages_json').then(r => r.json()).then(data => {
    fillPackageSelect((data && data.packages) || []);
  }).catch(() => {});
}
document.getElementById('pkg_upload_btn').addEventListener('click', function () {
  const fileEl = document.getElementById('pkg_file');
  const msg = document.getElementById('pkg_upload_msg');
  if (!fileEl.files || !fileEl.files[0]) {
    msg.textContent = '请选择 zip 文件';
    return;
  }
  const fd = new FormData();
  fd.append('file', fileEl.files[0]);
  fd.append('version', document.getElementById('pkg_version').value.trim());
  fd.append('notes', document.getElementById('pkg_notes').value.trim());
  msg.textContent = '上传中...';
  const btn = this;
  btn.disabled = true;
  fetch('/agent/upload', { method: 'POST', body: fd })
    .then(r => r.json().then(data => ({ ok: r.ok, data })))
    .then(res => {
      if (!res.data || !res.data.ok) {
        msg.textContent = (res.data && res.data.error) || '上传失败';
        return;
      }
      msg.textContent = '已上传 ' + res.data.package.version + '，SHA256 已计算';
      document.getElementById('action_sel').value = 'self_update';
      toggleActionFields();
      return loadPackages().then(() => {
        document.getElementById('update_version_sel').value = res.data.package.version;
      });
    })
    .catch(err => { msg.textContent = '上传失败: ' + err; })
    .finally(() => { btn.disabled = false; });
});
document.getElementById('btn_select_outdated').addEventListener('click', function () {
  const target = document.getElementById('update_version_sel').value;
  if (!target) {
    alert('请先选择目标版本');
    return;
  }
  let n = 0;
  document.querySelectorAll('.machine-row').forEach(row => {
    const chk = row.querySelector('.machine_chk');
    const info = lastAgentMap[row.dataset.machine] || {};
    const can = info.online && verLt(info.version, target);
    chk.checked = !!can;
    if (can) n += 1;
  });
  alert(n ? ('已勾选 ' + n + ' 台版本低于 ' + target + ' 的在线机台') : ('没有低于 ' + target + ' 的在线新版 Agent'));
});

let shotMachine = '';
let shotTaskId = '';
let shotTimer = null;
let shotHbTimer = null;
let shotLastTs = 0;
let shotZoom = 0; // 0 = 适应窗口

function renameMachine(machine) {
  const next = window.prompt('新的设备编码（线体-站位-编号，例如 T1-DL-01）', machine);
  if (next == null) return;
  const name = String(next).trim();
  if (!name || name === machine) return;
  fetch('/api/machine_rename', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old: machine, new: name }),
  }).then(r => r.json().then(data => ({ http: r.status, data })))
    .then(res => {
      if (!res.data || !res.data.ok) {
        alert((res.data && res.data.error) || '改编码失败');
        return;
      }
      fetchAgents(true);
    })
    .catch(err => alert('改编码失败: ' + err));
}

function deleteMachine(machine) {
  if (!confirm('确定从列表删除离线机台「' + machine + '」？不会卸载现场 Agent。')) return;
  fetch('/api/machine_delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machine: machine }),
  }).then(r => r.json().then(data => ({ http: r.status, data })))
    .then(res => {
      if (!res.data || !res.data.ok) {
        alert((res.data && res.data.error) || '删除失败');
        return;
      }
      fetchAgents(true);
    })
    .catch(err => alert('删除失败: ' + err));
}

function openRdp(machine) {
  const info = lastAgentMap[machine] || {};
  if (!info.ip) {
    alert('该机台还没有 IP（需新版 Agent 在线后才会上报）');
    return;
  }
  window.location.href = '/rdp/' + encodeURIComponent(machine);
}

function setShotStatus(text) {
  document.getElementById('shot_status').textContent = text || '';
}

function applyShotZoom() {
  const img = document.getElementById('shot_img');
  if (shotZoom <= 0) {
    img.className = 'fit';
    img.style.width = '';
    img.style.maxWidth = '100%';
    document.getElementById('shot_wrap').style.cursor = 'zoom-in';
  } else {
    img.className = '';
    img.style.maxWidth = 'none';
    const nw = img.naturalWidth || 0;
    img.style.width = nw ? (Math.round(nw * shotZoom) + 'px') : (Math.round(shotZoom * 100) + '%');
    document.getElementById('shot_wrap').style.cursor = 'grab';
  }
}

function showShotImage(machine) {
  const img = document.getElementById('shot_img');
  const empty = document.getElementById('shot_empty');
  img.style.display = 'inline';
  empty.style.display = 'none';
  img.onload = function () { applyShotZoom(); };
  img.src = '/screenshot/' + encodeURIComponent(machine) + '?t=' + Date.now();
}

function stopShotPoll() {
  if (shotTimer) { clearInterval(shotTimer); shotTimer = null; }
  if (shotHbTimer) { clearInterval(shotHbTimer); shotHbTimer = null; }
}

function shotHeartbeat(stop) {
  if (!shotMachine) return;
  fetch('/api/screenshot_watch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machine: shotMachine, stop: !!stop }),
  }).catch(() => {});
}

function openShotModal(machine) {
  shotMachine = machine;
  shotLastTs = 0;
  shotZoom = 0;
  document.getElementById('shot_title').textContent = '查看屏幕 — ' + machine;
  document.getElementById('shot_img').style.display = 'none';
  document.getElementById('shot_empty').style.display = 'block';
  document.getElementById('shot_empty').textContent = '正在请求截图...';
  setShotStatus('');
  document.getElementById('shot_modal').classList.add('show');
  shotHeartbeat(false);
  requestScreenshot(machine, true);
}

function closeShotModal() {
  stopShotPoll();
  shotHeartbeat(true);
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
  document.getElementById('shot_modal').classList.remove('show');
}

function pollShot(machine, taskId) {
  stopShotPoll();
  let waited = 0;
  let stale = 0;
  shotTimer = setInterval(function () {
    waited += 1;
    fetch('/screenshot_meta/' + encodeURIComponent(machine) + '?task_id=' + encodeURIComponent(taskId))
      .then(r => r.json())
      .then(data => {
        if (data && data.ready && data.ts && data.ts !== shotLastTs) {
          shotLastTs = data.ts;
          waited = 0;
          stale = 0;
          showShotImage(machine);
          setShotStatus((data.time || '') + '  连续刷新中');
        } else if (!shotLastTs) {
          setShotStatus('等待 Agent 回传画面... ' + Math.round(waited * 0.7) + 's');
          if (waited > 64) {
            setShotStatus('超时：Agent 可能正忙、未升级，或正在执行其它任务');
          }
        } else {
          stale += 1;
          if (stale > 12) {
            setShotStatus('画面已停止更新。连续动态查看需要 Agent 2.0.5+');
          }
        }
      })
      .catch(() => {});
  }, 700);
  shotHeartbeat(false);
  shotHbTimer = setInterval(function () { shotHeartbeat(false); }, 2000);
}

function requestScreenshot(machine, live) {
  setShotStatus('正在下发查看屏幕...');
  fetch('/api/screenshot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machine: machine, live: live !== false }),
  }).then(r => r.json().then(data => ({ http: r.status, data })))
    .then(res => {
      if (!res.data || !res.data.ok) {
        setShotStatus((res.data && res.data.error) || '下发失败');
        document.getElementById('shot_empty').textContent = (res.data && res.data.error) || '下发失败';
        return;
      }
      shotTaskId = res.data.task_id;
      pollShot(machine, shotTaskId);
    })
    .catch(err => {
      setShotStatus('请求失败: ' + err);
    });
}

let logMachine = '';
let logTaskId = '';
let logTimer = null;
let logLastTs = 0;
let logDateChanging = false;

function fmtLogDay(day) {
  const s = String(day || '');
  if (s.length === 8) return s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8);
  return s;
}

function setLogStatus(text) {
  document.getElementById('log_status').textContent = text || '';
}

function stopLogPoll() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}

function fillLogDates(dates, selected) {
  const sel = document.getElementById('log_date');
  const list = dates || [];
  logDateChanging = true;
  sel.innerHTML = '';
  if (!list.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '暂无日期';
    sel.appendChild(opt);
  } else {
    list.forEach(function (d) {
      const opt = document.createElement('option');
      opt.value = d;
      opt.textContent = fmtLogDay(d);
      if (d === selected) opt.selected = true;
      sel.appendChild(opt);
    });
    if (selected && list.indexOf(selected) < 0) {
      const opt = document.createElement('option');
      opt.value = selected;
      opt.textContent = fmtLogDay(selected);
      opt.selected = true;
      sel.insertBefore(opt, sel.firstChild);
    }
  }
  logDateChanging = false;
}

function showLogText(text, extra) {
  const pre = document.getElementById('log_pre');
  pre.textContent = text || '（空日志）';
  pre.scrollTop = pre.scrollHeight;
  if (extra) setLogStatus(extra);
}

function loadCachedLog(machine, date) {
  let url = '/agent_log/' + encodeURIComponent(machine);
  if (date) url += '?date=' + encodeURIComponent(date);
  return fetch(url).then(r => r.json()).then(data => {
    if (data && data.ok) {
      fillLogDates(data.dates || [], data.date || date || '');
      const extra = (data.time || '') + (data.truncated ? '  （已截断，仅显示末尾）' : '');
      showLogText(data.text, extra);
      return data;
    }
    return null;
  }).catch(() => null);
}

function pollAgentLog(machine, taskId, date) {
  stopLogPoll();
  let waited = 0;
  logTimer = setInterval(function () {
    waited += 1;
    let url = '/agent_log_meta/' + encodeURIComponent(machine) + '?task_id=' + encodeURIComponent(taskId);
    if (date) url += '&date=' + encodeURIComponent(date);
    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data && data.dates) fillLogDates(data.dates, date || data.date || '');
        if (data && data.ready && data.ts && data.ts !== logLastTs) {
          logLastTs = data.ts;
          stopLogPoll();
          loadCachedLog(machine, date || data.date || '').then(function () {
            setLogStatus((data.time || '已更新') + (data.truncated ? '  （已截断，仅显示末尾）' : ''));
          });
        } else if (waited > 40) {
          stopLogPoll();
          setLogStatus('超时：Agent 可能未升级，或正忙无法回传日志');
        } else {
          setLogStatus('等待 Agent 回传日志... ' + waited + 's');
        }
      })
      .catch(() => {});
  }, 700);
}

function requestAgentLog(machine, date) {
  setLogStatus('正在向机台拉取日志...');
  fetch('/api/agent_log', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machine: machine, date: date || '' }),
  }).then(r => r.json().then(data => ({ http: r.status, data })))
    .then(res => {
      if (!res.data || !res.data.ok) {
        setLogStatus((res.data && res.data.error) || '下发失败');
        return;
      }
      logTaskId = res.data.task_id;
      pollAgentLog(machine, logTaskId, date || '');
    })
    .catch(err => {
      setLogStatus('请求失败: ' + err);
    });
}

function openLogModal(machine) {
  logMachine = machine;
  logTaskId = '';
  logLastTs = 0;
  document.getElementById('log_title').textContent = '机台日志 — ' + machine;
  document.getElementById('log_pre').textContent = '正在请求日志...';
  fillLogDates([], '');
  setLogStatus('');
  document.getElementById('log_modal').classList.add('show');
  loadCachedLog(machine, '').then(function (cached) {
    const date = (cached && cached.date) || '';
    requestAgentLog(machine, date);
  });
}

function closeLogModal() {
  stopLogPoll();
  document.getElementById('log_modal').classList.remove('show');
}

document.getElementById('log_close').addEventListener('click', closeLogModal);
document.getElementById('log_modal').addEventListener('click', function (e) {
  if (e.target === this) closeLogModal();
});
document.getElementById('log_refresh').addEventListener('click', function () {
  if (!logMachine) return;
  const date = document.getElementById('log_date').value || '';
  requestAgentLog(logMachine, date);
});
document.getElementById('log_download').addEventListener('click', function () {
  if (!logMachine) return;
  const date = document.getElementById('log_date').value || '';
  let url = '/agent_log/' + encodeURIComponent(logMachine) + '?download=1';
  if (date) url += '&date=' + encodeURIComponent(date);
  window.location.href = url;
});
document.getElementById('log_date').addEventListener('change', function () {
  if (logDateChanging || !logMachine) return;
  const date = this.value || '';
  if (!date) return;
  loadCachedLog(logMachine, date).then(function (cached) {
    requestAgentLog(logMachine, date);
    if (!cached) document.getElementById('log_pre').textContent = '正在拉取 ' + fmtLogDay(date) + ' ...';
  });
});

document.getElementById('machine_tree').addEventListener('click', function (e) {
  const viewBtn = e.target.closest('.btn-view');
  const logBtn = e.target.closest('.btn-log');
  const rdpBtn = e.target.closest('.btn-rdp');
  const renameBtn = e.target.closest('.btn-rename');
  const delBtn = e.target.closest('.btn-del');
  if (!viewBtn && !logBtn && !rdpBtn && !renameBtn && !delBtn) return;
  e.preventDefault();
  e.stopPropagation();
  const row = e.target.closest('.machine-row');
  if (!row) return;
  const machine = row.dataset.machine;
  if (viewBtn && !viewBtn.disabled) openShotModal(machine);
  if (logBtn && !logBtn.disabled) openLogModal(machine);
  if (rdpBtn && !rdpBtn.disabled) openRdp(machine);
  if (renameBtn) renameMachine(machine);
  if (delBtn && !delBtn.disabled) deleteMachine(machine);
});
document.getElementById('shot_close').addEventListener('click', closeShotModal);
document.getElementById('shot_modal').addEventListener('click', function (e) {
  if (e.target === this) closeShotModal();
});
document.getElementById('shot_refresh').addEventListener('click', function () {
  if (shotMachine) requestScreenshot(shotMachine, true);
});
document.getElementById('shot_rdp').addEventListener('click', function () {
  if (shotMachine) openRdp(shotMachine);
});
document.getElementById('shot_zoom_in').addEventListener('click', function () {
  shotZoom = shotZoom <= 0 ? 1.25 : Math.min(4, Math.round((shotZoom + 0.25) * 100) / 100);
  applyShotZoom();
});
document.getElementById('shot_zoom_out').addEventListener('click', function () {
  if (shotZoom <= 0) shotZoom = 1;
  shotZoom = Math.max(0.25, Math.round((shotZoom - 0.25) * 100) / 100);
  applyShotZoom();
});
document.getElementById('shot_zoom_fit').addEventListener('click', function () {
  shotZoom = 0;
  applyShotZoom();
});
document.getElementById('shot_zoom_100').addEventListener('click', function () {
  shotZoom = 1;
  applyShotZoom();
});
document.getElementById('shot_fullscreen').addEventListener('click', function () {
  const card = document.getElementById('shot_card');
  if (!document.fullscreenElement) {
    const req = card.requestFullscreen || card.webkitRequestFullscreen;
    if (req) req.call(card);
  } else {
    document.exitFullscreen().catch(() => {});
  }
});
document.getElementById('shot_wrap').addEventListener('wheel', function (e) {
  if (!document.getElementById('shot_img').src) return;
  e.preventDefault();
  if (e.deltaY < 0) {
    shotZoom = shotZoom <= 0 ? 1.25 : Math.min(4, Math.round((shotZoom + 0.25) * 100) / 100);
  } else {
    if (shotZoom <= 0) shotZoom = 1;
    shotZoom = Math.max(0.25, Math.round((shotZoom - 0.25) * 100) / 100);
  }
  applyShotZoom();
}, { passive: false });
document.addEventListener('keydown', function (e) {
  if (document.getElementById('log_modal').classList.contains('show')) {
    if (e.key === 'Escape') closeLogModal();
    return;
  }
  if (!document.getElementById('shot_modal').classList.contains('show')) return;
  if (e.key === 'Escape') {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      closeShotModal();
    }
  }
});

bindTreeEvents();
toggleActionFields();
fetchResults();
fetchAgents();
loadPackages();
setInterval(fetchResults, 1000);
setInterval(function () { fetchAgents(false); }, 2000);
</script>
</body>
</html>
"""


load_state()
os.makedirs(PACKAGE_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(AGENT_LOG_DIR, exist_ok=True)
cleanup_web_logs()


def check_single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "FactorySyncControlWebMutex")
    if ctypes.GetLastError() == 183:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(APP_TITLE, "控制端已在运行，不必重复启动。")
            root.destroy()
        except Exception:
            pass
        sys.exit(0)


def get_launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def is_autostart_enabled():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        val, _ = winreg.QueryValueEx(key, APP_REG_NAME)
        winreg.CloseKey(key)
        return val == get_launch_command()
    except OSError:
        return False


def set_autostart_enabled(enabled):
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    try:
        if enabled:
            winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, get_launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_REG_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def create_tray_image():
    icon_file = resource_path("icon.ico")
    if Image is not None and os.path.exists(icon_file):
        try:
            return Image.open(icon_file)
        except Exception:
            pass
    if Image is None or ImageDraw is None:
        return None
    size = 64
    img = Image.new("RGB", (size, size), "#123047")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 10, 54, 54], radius=8, fill="#0b6e4f")
    d.rectangle([22, 28, 42, 36], fill="#ffffff")
    return img


def run_server():
    serve(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        threads=8,
        connection_limit=2000,
        channel_timeout=10,
        asyncore_use_poll=True,
    )


def open_dashboard():
    webbrowser.open(f"http://127.0.0.1:{SERVER_PORT}/")


def start_app_ui():
    if pystray is None:
        print("[!] 缺少 pystray/Pillow，将仅启动服务（无托盘）。可执行: pip install pystray pillow")
        run_server()
        return

    state = {
        "root": None,
        "icon": None,
        "autostart_var": None,
        "quitting": False,
    }

    def show_window(icon=None, item=None):
        root = state["root"]
        if root is None:
            return
        root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

    def hide_to_tray():
        root = state["root"]
        if root is None:
            return
        root.withdraw()
        icon = state["icon"]
        if icon is not None:
            try:
                icon.notify("已最小化到托盘，服务继续运行", APP_TITLE)
            except Exception:
                pass

    def on_close():
        hide_to_tray()

    def quit_app(icon=None, item=None):
        state["quitting"] = True
        icon = state["icon"]
        root = state["root"]
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        if root is not None:
            root.after(0, root.destroy)

    def toggle_autostart(icon=None, item=None):
        enabled = not is_autostart_enabled()
        set_autostart_enabled(enabled)
        var = state["autostart_var"]
        root = state["root"]
        if var is not None and root is not None:
            root.after(0, lambda: var.set(1 if enabled else 0))

    def on_autostart_check():
        enabled = bool(state["autostart_var"].get())
        set_autostart_enabled(enabled)

    root = tk.Tk()
    state["root"] = root
    root.title(APP_TITLE)
    root.geometry("420x220")
    root.resizable(False, False)
    try:
        root.iconbitmap(resource_path("icon.ico"))
    except Exception:
        pass

    frame = tk.Frame(root, padx=16, pady=14)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text=APP_TITLE, font=("Microsoft YaHei", 14, "bold")).pack(anchor="w")
    tk.Label(
        frame,
        text=f"服务已启动：http://本机IP:{SERVER_PORT}/",
        font=("Microsoft YaHei", 10),
        fg="#123047",
    ).pack(anchor="w", pady=(8, 2))
    tk.Label(
        frame,
        text="关闭窗口将最小化到托盘，不会退出服务。",
        font=("Microsoft YaHei", 9),
        fg="#5b6b7c",
    ).pack(anchor="w")

    btn_row = tk.Frame(frame)
    btn_row.pack(anchor="w", pady=(14, 8))
    tk.Button(btn_row, text="打开控制台网页", width=16, command=open_dashboard).pack(side="left")

    state["autostart_var"] = tk.IntVar(value=1 if is_autostart_enabled() else 0)
    tk.Checkbutton(
        frame,
        text="开机自启",
        variable=state["autostart_var"],
        command=on_autostart_check,
        font=("Microsoft YaHei", 10),
    ).pack(anchor="w", pady=(4, 0))

    tk.Label(
        frame,
        text="托盘菜单：打开网页 / 显示窗口 / 开机自启 / 退出",
        font=("Microsoft YaHei", 8),
        fg="#5b6b7c",
    ).pack(anchor="w", pady=(12, 0))

    root.protocol("WM_DELETE_WINDOW", on_close)

    menu = pystray.Menu(
        pystray.MenuItem("打开控制台网页", lambda icon, item: open_dashboard()),
        pystray.MenuItem("显示主窗口", show_window, default=True),
        pystray.MenuItem(
            "开机自启",
            toggle_autostart,
            checked=lambda item: is_autostart_enabled(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    image = create_tray_image()
    icon = pystray.Icon("FactorySyncControl", image, APP_TITLE, menu)
    state["icon"] = icon

    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=icon.run, daemon=True).start()
    root.mainloop()

    if not state["quitting"] and state["icon"] is not None:
        try:
            state["icon"].stop()
        except Exception:
            pass


if __name__ == "__main__":
    check_single_instance()
    print(f"[*] {APP_TITLE} 启动中，端口 {SERVER_PORT}")
    start_app_ui()

