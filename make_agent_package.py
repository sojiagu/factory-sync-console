# -*- coding: utf-8 -*-
"""把 dist/agent（onedir）打成 packages/agent_x.y.z.zip，并写 sha256 json。"""
import hashlib
import json
import os
import sys
import zipfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist", "agent")
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
    exe = os.path.join(DIST, "agent.exe")
    internal = os.path.join(DIST, "_internal")
    if not os.path.isfile(exe) or not os.path.isdir(internal):
        print("未找到 dist\\agent\\agent.exe 与 _internal，请先运行 build_agent.bat")
        return 1
    os.makedirs(PKG_DIR, exist_ok=True)
    zip_path = os.path.join(PKG_DIR, "agent_%s.zip" % version)
    meta_path = os.path.join(PKG_DIR, "agent_%s.json" % version)
    print("打包 %s ..." % zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(exe, "agent.exe")
        icon = os.path.join(DIST, "icon.ico")
        if os.path.isfile(icon):
            zf.write(icon, "icon.ico")
        else:
            internal_icon = os.path.join(internal, "icon.ico")
            if os.path.isfile(internal_icon):
                zf.write(internal_icon, "icon.ico")
        for root, dirs, files in os.walk(internal):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, DIST).replace("\\", "/")
                zf.write(full, rel)
    digest = sha256_file(zip_path)
    meta = {
        "version": version,
        "sha256": digest,
        "size": os.path.getsize(zip_path),
        "notes": "onedir agent.exe + _internal",
        "uploaded": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("完成: %s" % zip_path)
    print("sha256: %s" % digest)
    print("size: %s bytes" % meta["size"])
    print("把该 zip 放到控制端 packages\\ 目录（或网页上传）即可推送升级")
    return 0


if __name__ == "__main__":
    sys.exit(main())
