# -*- coding: utf-8 -*-
"""把 Win10 / Win7 两套 onedir 和启动器拼成 dist/agent_setup。

网页升级 zip 与 SoftwarePack 手工安装都用这一份，启动器按系统选运行时。
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
SETUP = os.path.join(DIST, "agent_setup")
WIN10 = os.path.join(DIST, "agent")
WIN7 = os.path.join(DIST, "agent_win7")
LAUNCHER = os.path.join(DIST, "agent_launcher.exe")


def copy_tree(src, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def strip_mftscan(root):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower() == "mftscan.exe":
                os.remove(os.path.join(dirpath, name))


def main():
    if not os.path.isfile(os.path.join(WIN10, "agent.exe")):
        print("缺少 dist\\agent\\agent.exe")
        return 1
    if not os.path.isfile(os.path.join(WIN7, "agent.exe")):
        print("缺少 dist\\agent_win7\\agent.exe")
        return 1
    if not os.path.isfile(LAUNCHER):
        print("缺少 dist\\agent_launcher.exe（先编译 C 启动器）")
        return 1
    if os.path.isdir(SETUP):
        shutil.rmtree(SETUP)
    os.makedirs(SETUP, exist_ok=True)
    shutil.copy2(LAUNCHER, os.path.join(SETUP, "agent.exe"))
    # 旧现场 Agent 只认「根目录 agent.exe + _internal」，两套运行时必须放进 _internal
    copy_tree(WIN10, os.path.join(SETUP, "_internal", "win10"))
    copy_tree(WIN7, os.path.join(SETUP, "_internal", "win7"))
    strip_mftscan(SETUP)
    print("OK dist\\agent_setup")
    print("  agent.exe                 启动器（旧升级也能替换）")
    print("  _internal\\win10\\         Python 3.11 + mftparser")
    print("  _internal\\win7\\          Python 3.8 + 纯 Python MFT")
    print("网页升级与 SoftwarePack 都用 dist\\agent_setup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
