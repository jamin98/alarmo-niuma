#!/usr/bin/env bash
# 在 Debian 上一键编译固件，产物是当前目录下的 a.bin。
#   sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi make git python3-pycryptodome
#   ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

ALARMO_COMMIT=8767325bb65a0994a1deeef783c20fa724bdae2f   # GaryOderNichts/alarmo，已验证能编过的版本
UP=upstream/alarmo

if [ ! -d "$UP/.git" ]; then
    mkdir -p upstream
    git clone https://github.com/GaryOderNichts/alarmo.git "$UP"
    git -C "$UP" checkout -q "$ALARMO_COMMIT"
fi

CUBE="$UP/usb_payload/STM32CubeH7"
if [ ! -f "$CUBE/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal.c" ]; then
    # 官方 SDK 整个仓库有好几个 G，这里只取用得到的几个目录
    CUBE_COMMIT=$(git -C "$UP" ls-tree HEAD usb_payload/STM32CubeH7 | awk '{print $3}')
    rm -rf "$CUBE"
    git clone --filter=blob:none --no-checkout https://github.com/STMicroelectronics/STM32CubeH7.git "$CUBE"
    git -C "$CUBE" sparse-checkout init --cone
    git -C "$CUBE" sparse-checkout set Drivers/STM32H7xx_HAL_Driver Drivers/CMSIS/Include \
        Drivers/CMSIS/Device/ST/STM32H7xx Middlewares/ST/STM32_USB_Device_Library
    git -C "$CUBE" checkout -q "$CUBE_COMMIT"
    git -C "$CUBE" submodule update --init --depth 1 Drivers/CMSIS/Device/ST/STM32H7xx Drivers/STM32H7xx_HAL_Driver
fi

KEY="$UP/usb_payload/key.py"
if grep -q 'fromhex("1234")' "$KEY"; then
    echo
    echo "还没填 AES key：请编辑 $KEY，把 AES_KEY 和 AES_IV 换成你这台 Alarmo 的值（各 32 位十六进制）。"
    echo "获取方法见 $UP/key_bruteforcer/README.md 和 usb_payload/README.md。"
    echo "先只编译不加密：make elf  （产物 build/niuma.bin）"
    exit 1
fi

make -j"$(nproc)" ALARMO="$PWD/$UP/usb_payload"
cp -f "$UP/usb_payload/MarkFile" MarkFile
echo
echo "完成：a.bin 和 MarkFile 已生成。接下来运行 ./flash.sh 把它们装进 Alarmo。"
