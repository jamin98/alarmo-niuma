// 设置（WiFi、hub 地址、口令）：存在 Core2 的 NVS 里，在屏幕上改，不用重新编译。
// config.h 里的值只当出厂默认值。
#pragma once
#include <M5Unified.h>

struct Settings {
    String ssid, pass, hub, token;
};
extern Settings S;

void settingsLoad();
void settingsSave();

// 屏幕上的设置界面，一直停在这里直到保存或取消。reason 是顶上的一行提示（可以为空）。
// 返回 true = 保存了新设置。
bool settingsUI(M5Canvas &cv, const char *reason = nullptr);
