// 复制成 config.h 再改（config.h 在 .gitignore 里，不会被提交）
#pragma once

#define WIFI_SSID   "你的WiFi名"
#define WIFI_PASS   "你的WiFi密码"

// hub 的地址和口令（hub/config.toml 里的 [server] token，没设就留空）
#define HUB_URL     "http://192.168.1.100:8765"
#define HUB_TOKEN   ""

#define JPEG_QUALITY   80     // 越低越小越快，画面越糊
#define FRAME_MS       100    // 两次取图的最小间隔，100 = 最多 10 帧/秒
#define BRIGHTNESS     160    // 背光 0~255
#define SHAKE_G        2.3f   // 摇一摇抽鞭子的灵敏度（加速度，单位 g），不想要就改成 99
