# -*- coding: utf-8 -*-
"""把 dist/agent_setup（启动器 + win10 + win7）打成 packages/agent_x.y.z.zip。"""
import hashlib
import json
import os
import sys
import zipfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(ROOT, "dist", "agent_setup")
PKG_DIR = os.path.join(ROOT, "packages")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main():
    version = (sys.argv[1] if len(sys.argv) > 1 else "2.0.7").strip()
    launcher = os.path.join(SETUP, "agent.exe")
    win10_exe = os.path.join(SETUP, "_internal", "win10", "agent.exe")
    win10_internal = os.path.join(SETUP, "_internal", "win10", "_internal")
    win7_exe = os.path.join(SETUP, "_internal", "win7", "agent.exe")
    win7_internal = os.path.join(SETUP, "_internal", "win7", "_internal")
    if not os.path.isfile(win10_exe):
        win10_exe = os.path.join(SETUP, "win10", "agent.exe")
        win10_internal = os.path.join(SETUP, "win10", "_internal")
    if not os.path.isfile(win7_exe):
        win7_exe = os.path.join(SETUP, "win7", "agent.exe")
        win7_internal = os.path.join(SETUP, "win7", "_internal")
    if not os.path.isfile(launcher):
        print("未找到 dist\\agent_setup\\agent.exe，请先运行 build_agent.bat")
        return 1
    if not os.path.isfile(win10_exe) or not os.path.isdir(win10_internal):
        print("未找到 dist\\agent_setup 的 win10 运行时，请先运行 build_agent.bat")
        return 1
    if not os.path.isfile(win7_exe) or not os.path.isdir(win7_internal):
        print("未找到 dist\\agent_setup 的 win7 运行时，请先运行 build_agent.bat")
        return 1
    os.makedirs(PKG_DIR, exist_ok=True)
    zip_path = os.path.join(PKG_DIR, "agent_%s.zip" % version)
    meta_path = os.path.join(PKG_DIR, "agent_%s.json" % version)
    print("打包 %s ..." % zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(SETUP):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, SETUP).replace("\\", "/")
                if name.lower() == "mftscan.exe":
                    continue
                zf.write(full, rel)
    digest = sha256_file(zip_path)
    meta = {
        "version": version,
        "sha256": digest,
        "size": os.path.getsize(zip_path),
        "notes": "统一包：启动器 + win10 + win7，安装/升级按系统自适应",
        "os": "any",
        "uploaded": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("完成: %s" % zip_path)
    print("sha256: %s" % digest)
    print("size: %s bytes" % meta["size"])
    print("网页升级与手工安装用同一份包，机台按 Win7/Win10 自动选用运行时")
    return 0


if __name__ == "__main__":
    sys.exit(main())
