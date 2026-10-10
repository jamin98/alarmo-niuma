// 设置（WiFi、hub 地址、口令）：存在 Core2 的 NVS 里，在屏幕上改，不用重新编译。
// config.h 里的值只当出厂默认值。
#pragma once
#include <M5Unified.h>

#include <vector>

struct Settings {
    String ssid, pass, hub, token;            // ssid / pass = 当前选用的 WiFi（开机先试它）
};
extern Settings S;

// 连上过的 WiFi，最近连上的在最前面，最多 MAX_SAVED_NETS 个
struct SavedNet {
    String ssid, pass;
};
static constexpr int MAX_SAVED_NETS = 10;
extern std::vector<SavedNet> savedNets;

void settingsLoad();
void settingsSave();
void rememberNet(const String &ssid, const String &pass);   // 连上了：记进列表并设为当前
int findSavedNet(const String &ssid);                       // 列表里的下标，没有返回 -1

// 屏幕上的设置界面，一直停在这里直到保存或取消。reason 是顶上的一行提示（可以为空）。
// 返回 true = 保存了新设置。
bool settingsUI(M5Canvas &cv, const char *reason = nullptr);
