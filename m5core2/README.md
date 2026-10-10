# M5Stack Core2 屏幕端

Core2（含 v1.1 / v1.3，BMI270 或 MPU6886 都行）的屏幕正好是 320×240，和 hub 渲染的画面一样大。固件只做三件事：连 WiFi 从 hub 取 `/frame.jpg` 显示；把按键、滑动、摇一摇变成 `/api/button` 请求；用振动和扬声器给抽鞭子加反馈。**不需要 USB 串口协议，也不需要 Alarmo 的 AES key。**

| 操作 | 作用 |
|---|---|
| 屏幕下方左键 | 一键鼓励（老板发大饼 + 群里发「大家辛苦了」） |
| 屏幕下方中键 | 抽鞭子（大按钮）；也用来从聊天记录回到工位 |
| 屏幕下方右键 | 换肤 |
| 屏幕上上下滑动 | 转滚轮：进聊天记录、翻页 |
| 点一下屏幕上的某只牛马 | 点名：老板跑过去盯着它，并私聊它一句（`/api/poke`） |
| 按住某只牛马 / 按住「我」 | 说话私聊它 / 发到群里：用 Core2 自带的麦克风录（屏幕右上角显示 REC），松手把录音传给 hub，由 Grok Bot 听写成文字发出去；最长 60 秒。跑 Grok Bot 的电脑要装虚拟麦克风，见主 README |
| 摇一摇设备 | 抽鞭子，同时振动 + 啪的一声 |

## 编译和烧录

```bash
pip install platformio           # 或用 VS Code 的 PlatformIO 插件
cd m5core2
cp include/config.example.h include/config.h     # Windows: copy include\config.example.h include\config.h
# 编辑 include/config.h：WIFI_SSID、WIFI_PASS、HUB_URL、HUB_TOKEN
pio run -t upload                # Core2 用 USB-C 接电脑
pio device monitor               # 看日志（可选）
```

- 第一次编译会下载 ESP32 工具链（几百 MB），需要一点时间。
- `include/config.h` 里有 WiFi 密码和 hub 口令，已经在 `.gitignore` 里，不要提交。
- `HUB_URL` 要写 Core2 能访问到的 hub 地址（和 Core2 在同一个局域网，hub 所在电脑的防火墙要放行 8765，见主 README）。
- `HUB_TOKEN` 就是 `hub/config.toml` 里的 `[server] token`；没设口令就留空。

## 调参

都在 `include/config.h`：`JPEG_QUALITY`（越低越快越糊）、`FRAME_MS`（取图间隔，100 = 最多 10 帧/秒）、`BRIGHTNESS`（背光）、`SHAKE_G`（摇一摇灵敏度，不想要就写 99）。

## 排错

| 屏幕显示 | 原因 |
|---|---|
| `Connecting WiFi...` 一直转 | WiFi 名或密码不对；Core2 只支持 2.4 GHz |
| `Hub offline` | hub 没运行 / `HUB_URL` 不对 / 防火墙拦了 8765 / 不在同一网段 |
| 画面正常，但按键后提示 `Bad token` | `HUB_TOKEN` 和 hub 的不一致 |
| 帧率低 | 降低 `JPEG_QUALITY`，或换掉 `neon` 主题（辉光让画面最复杂） |
