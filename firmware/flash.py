#!/usr/bin/env python3
"""把固件装进 Alarmo（只在内存里，断电就没了，每次上电都要重新来一遍）。Debian / 树莓派 / Windows / macOS 通用。

    1. 同时按住 Alarmo 顶部三个键，插上 USB 线，滚轮亮红灯后松手 —— 它会变成一个小 U 盘
    2. python3 flash.py            自动找那个 U 盘（Linux 下需要时会提示 sudo）
       python3 flash.py E:\\        Windows 下手动指定盘符
       python3 flash.py /dev/sdX   Linux 下手动指定磁盘，或已挂载的目录

固件文件 a.bin / MarkFile 在 Debian 或树莓派上用 ./build.sh 编译出来，拷到别的电脑上也能用。
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SMALL = 1 << 30                       # Alarmo 的盘小于 1 GiB，用来和别的 U 盘区分


def run(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- 找盘
def find_windows():
    """已挂载（有盘符）的小容量可移动盘。"""
    ps = ("Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' | "
          "Select-Object DeviceID,Size,VolumeName | ConvertTo-Json -Compress")
    out = run("powershell", "-NoProfile", "-Command", ps).stdout.strip()
    if not out:
        return []
    rows = json.loads(out)
    rows = rows if isinstance(rows, list) else [rows]
    return [r["DeviceID"] + "\\" for r in rows if r.get("Size") and int(r["Size"]) < SMALL]


def find_macos():
    cands = []
    for v in Path("/Volumes").iterdir():
        info = run("diskutil", "info", "-plist", str(v)).stdout
        try:
            p = plistlib.loads(info.encode())
        except Exception:
            continue
        if not p.get("Internal", True) and int(p.get("TotalSize", 1 << 40)) < SMALL:
            cands.append(str(v))
    return cands


def find_linux():
    """返回 [(设备路径, 已挂载的目录或 None)]，只要 USB 口上的小容量盘。"""
    out = run("lsblk", "-J", "-b", "-o", "NAME,PATH,TRAN,SIZE,MOUNTPOINT,TYPE").stdout
    cands = []
    for disk in json.loads(out or "{}").get("blockdevices", []):
        if disk.get("tran") != "usb" or int(disk.get("size") or 0) >= SMALL:
            continue
        parts = [c for c in disk.get("children", []) if c.get("type") == "part"] or [disk]
        part = parts[0]
        cands.append((part["path"], part.get("mountpoint")))
    return cands


def mount_linux(dev):
    """树莓派桌面版 / 带 udisks2 的系统不用 root；否则回退到 mount（需要 sudo）。返回 (目录, 卸载函数)。"""
    if shutil.which("udisksctl"):
        r = run("udisksctl", "mount", "-b", dev)
        if r.returncode == 0 and " at " in r.stdout:
            mnt = r.stdout.strip().split(" at ", 1)[1].rstrip(".")
            return mnt, lambda: run("udisksctl", "unmount", "-b", dev)
    mnt = tempfile.mkdtemp()
    r = run("mount", dev, mnt)
    if r.returncode != 0:
        os.rmdir(mnt)
        sys.exit(f"挂载 {dev} 失败: {r.stderr.strip()}\n请用 sudo 运行: sudo python3 flash.py")

    def undo():
        run("umount", mnt)
        os.rmdir(mnt)
    return mnt, undo


def locate(arg):
    """返回 (目标目录, 卸载函数或 None)。"""
    if arg:
        if os.path.isdir(arg):
            return arg, None
        if sys.platform.startswith("linux") and os.path.exists(arg):
            return mount_linux(arg)
        sys.exit(f"{arg} 不是目录，也不是块设备")
    if sys.platform == "win32":
        c = find_windows()
        if len(c) != 1:
            sys.exit(f"没能唯一确定 Alarmo 的盘（找到 {len(c)} 个小容量可移动盘 {c}）。\n请手动指定，比如: python flash.py E:\\")
        return c[0], None
    if sys.platform == "darwin":
        c = find_macos()
        if len(c) != 1:
            sys.exit(f"没能唯一确定 Alarmo 的盘（找到 {len(c)} 个）。请手动指定: python3 flash.py /Volumes/XXX")
        return c[0], None
    c = find_linux()
    if len(c) != 1:
        sys.exit(f"没能唯一确定 Alarmo 的盘（找到 {len(c)} 个候选）。请手动指定: sudo python3 flash.py /dev/sdX1")
    dev, mnt = c[0]
    print("目标磁盘:", dev)
    return (mnt, None) if mnt else mount_linux(dev)


# ---------------------------------------------------------------- 写入
def write_synced(src, dst):
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo)
        fo.flush()
        os.fsync(fo.fileno())


def main():
    a, mark = HERE / "a.bin", HERE / "MarkFile"
    if not (a.exists() and mark.exists()):
        sys.exit("找不到 a.bin / MarkFile：在 Debian 或树莓派上先运行 ./build.sh，再把这两个文件拷到这个目录")
    target, undo = locate(sys.argv[1] if len(sys.argv) > 1 else "")
    print("写入到:", target)
    try:
        write_synced(a, os.path.join(target, "a.bin"))
        time.sleep(0.3)
        write_synced(mark, os.path.join(target, "MarkFile"))   # MarkFile 必须最后写，2ndloader 看到它就开始加载
    finally:
        if undo:
            undo()
    where = {"win32": "设备管理器的「端口」里会多一个 USB 串行设备（COMx）",
             "darwin": "/dev/cu.usbmodem*"}.get(sys.platform, "/dev/ttyACM*")
    print(f"已写入。屏幕中间出现黄色方块说明固件已启动，几秒后会出现串口：{where}")
    print("检查：python hub.py --list-ports（带 * 的就是 Alarmo）")


if __name__ == "__main__":
    main()
