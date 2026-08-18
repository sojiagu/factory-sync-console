import os
import sys
import time
import threading
import requests
import shutil
import configparser
import subprocess

# 托盘相关
import pystray
from PIL import Image, ImageDraw

# ===============================
# 配置
# ===============================
CONTROL_SERVER = "http://127.0.0.1:5000"
REPORT_ENDPOINT = "/report"
POLL_INTERVAL = 5
SHARE_USER = "1"
SHARE_PASS = "1"

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
os.makedirs(BASE_DIR, exist_ok=True)
LOG_FILE = os.path.join(BASE_DIR, "robocopy_log.txt")
DEVICE_INI = os.path.join(BASE_DIR, "device.ini")

# ===============================
# 读取/保存机器名
# ===============================
def get_machine_name():
    config = configparser.ConfigParser()
    if os.path.exists(DEVICE_INI):
        try:
            config.read(DEVICE_INI, encoding="utf-8")
            if "device" in config and "name" in config["device"]:
                return config["device"]["name"]
        except Exception:
            pass

    machine = input("请输入本机标识(MACHINE_NAME): ").strip()
    if not machine:
        machine = "UNKNOWN"

    config["device"] = {"name": machine}
    with open(DEVICE_INI, "w", encoding="utf-8") as f:
        config.write(f)

    return machine


MACHINE_NAME = get_machine_name()

# ===============================
# 回传结果
# ===============================
def report_result(task_id, status, message, progress=None):
    try:
        data = {
            "task_id": task_id,
            "machine": MACHINE_NAME,
            "status": status,
            "message": message,
        }
        if progress is not None:
            data["progress"] = progress
        requests.post(CONTROL_SERVER + REPORT_ENDPOINT, json=data, timeout=5)
    except Exception as e:
        print("[!] 回传失败:", e)

# ===============================
# 网络共享连接
# ===============================
def connect_share(share_path):
    try:
        cmd = f'net use "{share_path}" /user:{SHARE_USER} {SHARE_PASS} /persistent:no'
        os.system(cmd)
        return True
    except Exception as e:
        report_result("", "error", f"连接共享失败: {e}")
        return False

# ===============================
# 遍历目录统计总字节数
# ===============================
def count_bytes(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total

# ===============================
# 执行任务
# ===============================
def run_task(task):
    task_id = task.get("task_id", str(time.time()))
    action = task.get("action")

    try:
        if action == "deploy_folder":
            src = task["source"]
            dst = task["destination"]

            connect_share(src)
            os.makedirs(dst, exist_ok=True)

            report_result(task_id, "progress", "开始复制文件夹...", progress=0)

            total_bytes = count_bytes(src)
            copied_bytes = 0

            for root, dirs, files in os.walk(src):
                rel_path = os.path.relpath(root, src)
                target_dir = os.path.join(dst, rel_path)
                os.makedirs(target_dir, exist_ok=True)

                for f in files:
                    src_file = os.path.join(root, f)
                    dst_file = os.path.join(target_dir, f)
                    try:
                        shutil.copy2(src_file, dst_file)
                        copied_bytes += os.path.getsize(src_file)
                        progress_percent = int(copied_bytes / total_bytes * 100) if total_bytes else 100
                        report_result(task_id, "progress", f"已复制 {copied_bytes}/{total_bytes} bytes", progress_percent)
                    except Exception as e:
                        report_result(task_id, "error", f"复制失败: {src_file} -> {dst_file}, {e}")

            report_result(task_id, "success", f"文件夹已同步 {src} -> {dst}", 100)

        elif action == "run_command":
            cmd = task["command"]
            os.system(cmd)
            report_result(task_id, "success", f"命令已执行: {cmd}")

        elif action == "exec_cmd":
            cmd = task["command"]
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate(timeout=120)
            result_msg = stdout if stdout else stderr
            report_result(task_id, "success", f"执行结果: {result_msg}")

        else:
            report_result(task_id, "error", f"未知任务类型: {action}")

    except Exception as e:
        report_result(task_id, "error", str(e))

# ===============================
# 主循环
# ===============================
def agent_loop():
    while True:
        try:
            url = f"{CONTROL_SERVER}/task?machine={MACHINE_NAME}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                task = resp.json()
                if task and "action" in task:
                    threading.Thread(target=run_task, args=(task,)).start()
        except Exception as e:
            print("[!] 获取任务失败:", e)
        time.sleep(POLL_INTERVAL)

# ===============================
# 托盘图标
# ===============================
def create_image():
    size, block = 64, 32
    img = Image.new("RGB", (size, size), "blue")
    d = ImageDraw.Draw(img)
    x = (size - block) // 2
    y = (size - block) // 2
    d.rounded_rectangle([x, y, x + block, y + block], radius=6, fill="red")
    return img

def run_tray():
    icon = pystray.Icon("Agent",
                        create_image(),
                        f"Agent ({MACHINE_NAME})",)
    icon.run()

if __name__ == "__main__":
    print(f"[*] Agent 已启动，机器名 {MACHINE_NAME}")
    threading.Thread(target=agent_loop, daemon=True).start()
    run_tray()
