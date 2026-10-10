#!/usr/bin/env python3
"""端到端验证：hub 渲染 -> device.py 编码 -> 固件 proto.c 解码 -> 逐像素比对。
运行: python3 test_proto.py   （需要 gcc）"""
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "hub"))
import device  # noqa: E402
from render import Renderer  # noqa: E402
from state import Office  # noqa: E402


def panel_to_screen(raw):
    """面板原生内存 (320 行 x 240 列, BGR) -> 屏幕图像 (240,320,3) RGB。"""
    p = np.frombuffer(raw, np.uint8).reshape(320, 240, 3)
    return p[::-1].transpose(1, 0, 2)[:, :, ::-1]


def run(exe, stream, chunk):
    r = subprocess.run([exe, str(chunk)], input=stream, capture_output=True, check=True)
    return r.stdout, r.stderr.decode().strip()


def main():
    tmp = Path(tempfile.mkdtemp())
    exe = str(tmp / "host_test")
    subprocess.run(["gcc", "-O1", "-Wall", "-Wextra", "-fsanitize=address,undefined", "-o", exe,
                    str(HERE / "host_test.c"), str(HERE.parent / "source" / "proto.c")], check=True)

    R = Renderer()
    T = 1e6
    o = Office(group="协议测试")
    o.set_bots(["架构牛", "后端马", "前端小牛", "测试老马", "运维牛", "文档马"], T)
    o.bridge_seen = T + 1e5
    o.set_status("架构牛", "typing", T)
    o.add_message("架构牛", "方案好了 @后端马 接一下", T)
    stream = bytearray(device.encode_brightness(0.8) + device.encode_hello())
    prev = last = None
    for i in range(80):
        if i == 30:
            o.whip(T + 3.0)
        if i == 60:
            o.view = "chat"
        last = np.asarray(R.render(o, T + i * 0.1))
        for pkt in device.encode_diff(prev, last):
            stream += pkt
        prev = last

    ok = True
    for chunk in (1, 7, 64, 512, 4096):
        out, info = run(exe, bytes(stream), chunk)
        same = np.array_equal(panel_to_screen(out), last)
        good = same and "errors=0 bad=0 brightness=800 hellos=1" in info
        ok &= good
        print(f"chunk={chunk:5d}  像素一致={same}  {info}  {'OK' if good else 'FAIL'}")

    # 噪点图：RLE 会比原始数据大，走 raw 分支
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 256, (240, 320, 3), dtype=np.uint8)
    out, info = run(exe, b"".join(device.encode_full(noise)), 64)
    good = np.array_equal(panel_to_screen(out), noise) and "errors=0 bad=0" in info
    ok &= good
    print(f"raw 分支        {info}  {'OK' if good else 'FAIL'}")

    # 抗干扰：数据流前面和包之间塞垃圾，后面的包仍要能正确解出
    random.seed(2)
    junk = bytes(random.randrange(256) for _ in range(300)).replace(b"\xA5", b"\x00")
    s = junk + b"".join(device.encode_full(noise)) + junk + b"\xA5" + b"".join(device.encode_full(last))
    out, info = run(exe, s, 64)
    good = np.array_equal(panel_to_screen(out), last) and "bad=0" in info
    ok &= good
    print(f"夹杂垃圾字节    {info}  {'OK' if good else 'FAIL'}")

    # 截断的包后面跟完整包：靠超时恢复不了（数据是连续的），但不能越界或崩溃
    full = b"".join(device.encode_full(last))
    out, info = run(exe, full[: len(full) // 2] + full, 64)
    print(f"截断包不崩溃    {info}  OK")

    print("总体带宽:", len(stream) // 80, "字节/帧")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
