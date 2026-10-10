#!/usr/bin/env bash
# 兼容旧用法，实际逻辑在 flash.py（Debian / 树莓派 / Windows / macOS 通用）
exec python3 "$(dirname "$0")/flash.py" "$@"
