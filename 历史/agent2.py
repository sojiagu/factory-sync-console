import os
import sys
import time
import subprocess
import threading
import requests
import re

# ===============================
# 配置
# ===============================
CONTROL_SERVER = "http://127.0.0.1:5000"  # 控制端服务地址
REPORT_ENDPOINT = "/report"               # 回传结果接口
POLL_INTERVAL = 10                        # 秒，轮询任务

# 机器名可固定，也可以用环境变量或 IP 自动生成
MACHINE_NAME = os.environ.get("MACHINE_NAME", "T1-ATA-01")

SHARE_USER = "1"
SHARE_PASS = "1"

# 当前脚本/可执行文件所在目录（用于保存日志）
BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
os.makedirs(BASE_DIR, exist_ok=True)
LOG_FILE = os.path.join(BASE_DIR, "robocopy_log.txt")

# ===============================
# 任务执行 & 回传
# ===============================
def report_result(status, message):
    try:
        data = {
            "machine": MACHINE_NAME,
            "status": status,
            "message": message
        }
        url = CONTROL_SERVER + REPORT_ENDPOINT
        requests.post(url, json=data, timeout=5)
    except Exception as e:
        print("[!] 回传失败:", e)

def connect_share(share_path):
    """先连接网络共享，避免权限问题"""
    try:
        cmd = f'net use "{share_path}" /user:{SHARE_USER} {SHARE_PASS} /persistent:no'
        subprocess.run(cmd, shell=True, capture_output=True)
        return True
    except Exception as e:
        report_result("error", f"连接共享失败: {e}")
        return False

def parse_robocopy_progress(log_file):
    """解析 robocopy 日志，提取已复制文件数"""
    if not os.path.exists(log_file):
        return 0
    copied = 0
    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            # 匹配复制文件行
            if re.search(r'^\s+\d+\s+\d+\s+\d+\s+\d+', line):
                copied += 1
    return copied

def run_task(task):
    try:
        if task["action"] == "deploy_folder":
            src = task["source"]
            dst = task["destination"]
            mode = task.get("mode", "overwrite")

            # 先连接共享
            connect_share(src)

            # 本地目录不存在则创建
            os.makedirs(dst, exist_ok=True)

            # robocopy 命令，/COPY:DAT 兼容 Win10，无 ACL/Owner/Audit
            if mode == "mirror":
                cmd = f'robocopy "{src}" "{dst}" /MIR /COPY:DAT /R:5 /W:5 /LOG:"{LOG_FILE}" /NFL /NDL /NP'
            else:
                cmd = f'robocopy "{src}" "{dst}" /E /COPY:DAT /R:5 /W:5 /LOG:"{LOG_FILE}" /NFL /NDL /NP'

            # 启动子进程
            proc = subprocess.Popen(cmd, shell=True)

            # 轮询进度
            last_report = 0
            while proc.poll() is None:
                time.sleep(3)
                copied = parse_robocopy_progress(LOG_FILE)
                if copied != last_report:
                    report_result("progress", f"已复制文件数: {copied}")
                    last_report = copied

            # 结束
            if proc.returncode < 8:
                report_result("success", f"文件夹已同步 {src} -> {dst}")
            else:
                report_result("error", f"robocopy失败, 请看 {LOG_FILE}")

        elif task["action"] == "run_command":
            subprocess.Popen(task["command"], shell=True)
            report_result("success", f"命令已执行: {task['command']}")

        else:
            report_result("error", "未知任务类型")

    except Exception as e:
        report_result("error", str(e))

# ===============================
# Agent 主循环
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
# 启动 Agent
# ===============================
if __name__ == "__main__":
    print(f"[*] Agent 已启动，机器名 {MACHINE_NAME}")
    agent_loop()
