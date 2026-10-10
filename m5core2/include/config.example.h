// 出厂默认值。WiFi、hub 地址、口令平时在 Core2 屏幕上设置（按住右键 2 秒），存在 Core2 里，不用改这里；
// 只有 Core2 里还没存过设置时才用下面的值。想改默认值或调参：复制成 niuma_config.h 再改（niuma_config.h 不进 git）
#pragma once

#define WIFI_SSID   "你的WiFi名"
#define WIFI_PASS   "你的WiFi密码"

// hub 的地址和口令（hub/config.toml 里的 [server] token，没设就留空）
#define HUB_URL     "http://192.168.1.100:8765"
#define HUB_TOKEN   ""

#define JPEG_QUALITY   80     // 越低越小越快，画面越糊
#define FRAME_MS       30     // 两次取图的最小间隔（毫秒）；hub 有新帧才会返回，所以实际帧率跟着 hub 的 fps 走
#define BRIGHTNESS     160    // 背光 0~255
#define SHAKE_G        2.3f   // 摇一摇抽鞭子的灵敏度（加速度，单位 g），不想要就改成 99
