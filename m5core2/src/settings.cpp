// 屏幕上的设置界面：选 WiFi、输密码、改 hub 地址和口令。
//
//   设置首页   点一行进去改；底部「取消」「保存并连接」（实体键：左 = 取消，右 = 保存）
//   选 WiFi    扫描附近的网络，点一下选中；第一行可以手动输入（隐藏网络）
//   屏幕键盘   字母 / 大写 / 符号三套键位，OK 确认；左上角「< 取消」放弃这次修改
#include "settings.h"

#include <Preferences.h>
#include <WiFi.h>

#include <algorithm>
#include <vector>

#if __has_include("niuma_config.h")                // 可选的本地配置（不进 git）；没有就用示例里的默认值
#include "niuma_config.h"
#else
#include "config.example.h"
#endif

Settings S;

// ---------------------------------------------------------------- 存取
static bool placeholder(const char *v) {
    String s(v);
    return s.isEmpty() || s.indexOf("你的") >= 0 || s.startsWith("YOUR");
}

void settingsLoad() {
    Preferences p;
    p.begin("niuma", false);
    S.ssid = p.getString("ssid", placeholder(WIFI_SSID) ? "" : WIFI_SSID);
    S.pass = p.getString("pass", placeholder(WIFI_SSID) ? "" : WIFI_PASS);
    S.hub = p.getString("hub", HUB_URL);
    S.token = p.getString("token", HUB_TOKEN);
    p.end();
}

void settingsSave() {
    Preferences p;
    p.begin("niuma", false);
    p.putString("ssid", S.ssid);
    p.putString("pass", S.pass);
    p.putString("hub", S.hub);
    p.putString("token", S.token);
    p.end();
}

// ---------------------------------------------------------------- 画图和输入的小工具
static const lgfx::IFont *CN = &fonts::efontCN_14;
static constexpr uint16_t BG = 0x10A2, PANEL = 0x2124, LINE = 0x39C7, KEY = 0x31A6, KEY2 = 0x4A49;
static constexpr uint16_t ACC = 0xFD20, DIM = 0x9CD3, OK_GREEN = 0x2E8B;

struct Box {
    int x, y, w, h;
    bool hit(int px, int py) const { return px >= x && px < x + w && py >= y && py < y + h; }
};

enum Tap { TAP_SCREEN, TAP_A, TAP_B, TAP_C };

static void beep() { M5.Speaker.tone(1500, 18, 0); }

// 等用户点一下屏幕或按一个实体键
static Tap waitTap(int &x, int &y) {
    while (true) {
        M5.update();
        if (M5.BtnA.wasClicked()) return TAP_A;
        if (M5.BtnB.wasClicked()) return TAP_B;
        if (M5.BtnC.wasClicked()) return TAP_C;
        auto t = M5.Touch.getDetail();
        if (t.wasClicked() && t.y < 240) {
            x = t.x;
            y = t.y;
            return TAP_SCREEN;
        }
        delay(8);
    }
}

// 太长的字只留能放下的部分；tail = true 时留结尾（输入框里看最新打的字）
static String fitText(M5Canvas &cv, const String &s, int w, bool tail = false) {
    if (cv.textWidth(s) <= w) return s;
    String t = s;
    while (t.length() && cv.textWidth((tail ? "…" : "") + t + (tail ? "" : "…")) > w) {
        if (tail) {                               // 按 UTF-8 字符去掉开头一个字
            int n = 1;
            while (n < (int)t.length() && (t[n] & 0xC0) == 0x80) n++;
            t = t.substring(n);
        } else {
            int n = t.length() - 1;
            while (n > 0 && (t[n] & 0xC0) == 0x80) n--;
            t = t.substring(0, n);
        }
    }
    return tail ? "…" + t : t + "…";
}

static void titleBar(M5Canvas &cv, const char *title, const char *left = nullptr) {
    cv.fillRect(0, 0, 320, 26, PANEL);
    cv.drawFastHLine(0, 26, 320, LINE);
    cv.setTextDatum(textdatum_t::middle_center);
    cv.setTextColor(TFT_WHITE, PANEL);
    cv.drawString(title, 160, 13);
    if (left) {
        cv.setTextDatum(textdatum_t::middle_left);
        cv.setTextColor(ACC, PANEL);
        cv.drawString(left, 8, 13);
    }
}

static void button(M5Canvas &cv, const Box &b, const char *label, uint16_t bg, uint16_t fg = TFT_WHITE) {
    cv.fillRoundRect(b.x, b.y, b.w, b.h, 6, bg);
    cv.setTextDatum(textdatum_t::middle_center);
    cv.setTextColor(fg, bg);
    cv.drawString(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
}

// ---------------------------------------------------------------- 屏幕键盘
struct Key {
    String label;      // 显示的字
    String out;        // 打出来的字；空 = 功能键
    float units;       // 宽度（一格 = 32 像素）
    int action;        // 0 字符 1 Shift 2 退格 3 切换符号 4 确定
};

static std::vector<Key> row(const char *chars) {
    std::vector<Key> r;
    for (const char *p = chars; *p; p++) r.push_back({String(*p), String(*p), 1, 0});
    return r;
}

static std::vector<std::vector<Key>> layout(int mode) {      // 0 小写 1 大写 2 符号
    std::vector<std::vector<Key>> rows;
    if (mode == 2) {
        rows.push_back(row("1234567890"));
        rows.push_back(row("!@#$%^&*()"));
        rows.push_back(row("~`+=[]{}|\\"));
        auto r3 = row("_;:'\",?");
        r3.insert(r3.begin(), {"ABC", "", 1.5, 3});
        r3.push_back({"删除", "", 1.5, 2});
        rows.push_back(r3);
        rows.push_back({{"ABC", "", 1.5, 3}, {"<", "<", 1, 0}, {">", ">", 1, 0}, {"空格", " ", 3, 0},
                        {".", ".", 1, 0}, {"-", "-", 1, 0}, {"OK", "", 1.5, 4}});
    } else {
        const bool up = mode == 1;
        rows.push_back(row("1234567890"));
        rows.push_back(row(up ? "QWERTYUIOP" : "qwertyuiop"));
        rows.push_back(row(up ? "ASDFGHJKL-" : "asdfghjkl-"));
        auto r3 = row(up ? "ZXCVBNM" : "zxcvbnm");
        r3.insert(r3.begin(), {up ? "小写" : "大写", "", 1.5, 1});
        r3.push_back({"删除", "", 1.5, 2});
        rows.push_back(r3);
        rows.push_back({{"#+=", "", 1.5, 3}, {".", ".", 1, 0}, {"/", "/", 1, 0}, {"空格", " ", 3, 0},
                        {":", ":", 1, 0}, {"@", "@", 1, 0}, {"OK", "", 1.5, 4}});
    }
    return rows;
}

// 返回 true = 确定（text 已改），false = 取消（text 不变）
static bool keyboard(M5Canvas &cv, const char *title, String &text) {
    String buf = text;
    int mode = 0;
    const Box cancel{0, 0, 70, 26};
    const int top = 64, rh = 35;
    while (true) {
        auto rows = layout(mode);
        cv.fillScreen(BG);
        titleBar(cv, title, "< 取消");
        cv.fillRoundRect(6, 31, 308, 28, 5, PANEL);
        cv.drawRoundRect(6, 31, 308, 28, 5, ACC);
        cv.setTextDatum(textdatum_t::middle_left);
        cv.setTextColor(TFT_WHITE, PANEL);
        String shown = fitText(cv, buf, 292, true);
        cv.drawString(shown, 12, 46);
        int cx = 12 + cv.textWidth(shown) + 1;
        cv.drawFastVLine(cx, 37, 17, ACC);                           // 光标
        std::vector<std::pair<Box, Key>> hits;
        for (size_t r = 0; r < rows.size(); r++) {
            float x = 0;
            for (auto &k : rows[r]) {
                Box b{(int)(x * 32) + 1, top + (int)r * rh + 1, (int)(k.units * 32) - 2, rh - 3};
                uint16_t bg = k.action == 4 ? OK_GREEN : k.action ? KEY2 : KEY;
                button(cv, b, k.label.c_str(), bg);
                hits.push_back({b, k});
                x += k.units;
            }
        }
        cv.pushSprite(0, 0);

        int x, y;
        Tap t = waitTap(x, y);
        if (t == TAP_A || (t == TAP_SCREEN && cancel.hit(x, y))) { beep(); return false; }
        if (t == TAP_C) { beep(); text = buf; return true; }
        if (t == TAP_B) { beep(); if (buf.length()) buf.remove(buf.length() - 1); continue; }
        for (auto &h : hits) {
            if (!h.first.hit(x, y)) continue;
            beep();
            const Key &k = h.second;
            if (k.action == 0) {
                buf += k.out;
                if (mode == 1) mode = 0;                             // 大写只管一个字母
            } else if (k.action == 1) {
                mode = mode == 1 ? 0 : 1;
            } else if (k.action == 2) {
                if (buf.length()) buf.remove(buf.length() - 1);
            } else if (k.action == 3) {
                mode = mode == 2 ? 0 : 2;
            } else {
                text = buf;
                return true;
            }
            break;
        }
    }
}

// ---------------------------------------------------------------- 选 WiFi
struct Net {
    String ssid;
    int rssi;
    bool open;
};

static std::vector<Net> scan(M5Canvas &cv) {
    cv.fillScreen(BG);
    titleBar(cv, "选择 WiFi");
    cv.setTextDatum(textdatum_t::middle_center);
    cv.setTextColor(DIM, BG);
    cv.drawString("正在扫描附近的 WiFi…", 160, 120);
    cv.pushSprite(0, 0);
    if (WiFi.getMode() == WIFI_OFF) WiFi.mode(WIFI_STA);
    int n = WiFi.scanNetworks();
    std::vector<Net> nets;
    for (int i = 0; i < n; i++) {
        String s = WiFi.SSID(i);
        if (s.isEmpty()) continue;
        auto it = std::find_if(nets.begin(), nets.end(), [&](const Net &x) { return x.ssid == s; });
        if (it == nets.end()) nets.push_back({s, WiFi.RSSI(i), WiFi.encryptionType(i) == WIFI_AUTH_OPEN});
        else if (WiFi.RSSI(i) > it->rssi) it->rssi = WiFi.RSSI(i);
    }
    WiFi.scanDelete();
    std::sort(nets.begin(), nets.end(), [](const Net &a, const Net &b) { return a.rssi > b.rssi; });
    return nets;
}

static void bars(M5Canvas &cv, int x, int y, int rssi) {
    int lv = rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : 1;
    for (int k = 0; k < 4; k++) {
        int h = 4 + k * 3;
        cv.fillRect(x + k * 5, y + 14 - h, 3, h, k < lv ? TFT_WHITE : LINE);
    }
}

static void lockIcon(M5Canvas &cv, int x, int y) {
    cv.drawRoundRect(x + 2, y, 7, 7, 3, DIM);
    cv.fillRect(x, y + 5, 11, 8, DIM);
}

// 返回 true = 选好了（ssid / open 已填），false = 取消
static bool pickWifi(M5Canvas &cv, String &ssid, bool &open) {
    std::vector<Net> nets = scan(cv);
    int page = 0;
    const int per = 5, rh = 38, top = 28;
    const Box cancel{0, 0, 70, 26}, prev{4, 218, 100, 20}, again{110, 218, 100, 20}, next{216, 218, 100, 20};
    while (true) {
        int total = nets.size() + 1;                                 // 第 0 行是"手动输入"
        int pages = (total + per - 1) / per;
        page = std::max(0, std::min(page, pages - 1));
        cv.fillScreen(BG);
        titleBar(cv, nets.empty() ? "选择 WiFi（没扫到）" : "选择 WiFi", "< 取消");
        for (int r = 0; r < per; r++) {
            int i = page * per + r;
            if (i >= total) break;
            int y = top + r * rh;
            cv.drawFastHLine(0, y + rh - 1, 320, PANEL);
            cv.setTextDatum(textdatum_t::middle_left);
            if (i == 0) {
                cv.setTextColor(ACC, BG);
                cv.drawString("+ 手动输入 WiFi 名（隐藏网络）", 12, y + rh / 2);
                continue;
            }
            const Net &n = nets[i - 1];
            bool cur = n.ssid == S.ssid;
            cv.setTextColor(cur ? ACC : TFT_WHITE, BG);
            cv.drawString(fitText(cv, (cur ? "* " : "") + n.ssid, 250), 12, y + rh / 2);
            if (!n.open) lockIcon(cv, 272, y + 12);
            bars(cv, 290, y + 11, n.rssi);
        }
        button(cv, prev, "< 上一页", page > 0 ? KEY2 : PANEL, page > 0 ? TFT_WHITE : LINE);
        button(cv, again, "重新扫描", KEY2);
        button(cv, next, "下一页 >", page < pages - 1 ? KEY2 : PANEL, page < pages - 1 ? TFT_WHITE : LINE);
        cv.pushSprite(0, 0);

        int x, y;
        Tap t = waitTap(x, y);
        if (t == TAP_SCREEN && cancel.hit(x, y)) { beep(); return false; }
        if (t == TAP_A || (t == TAP_SCREEN && prev.hit(x, y))) { beep(); page--; continue; }
        if (t == TAP_C || (t == TAP_SCREEN && next.hit(x, y))) { beep(); page++; continue; }
        if (t == TAP_B || (t == TAP_SCREEN && again.hit(x, y))) { beep(); nets = scan(cv); page = 0; continue; }
        if (t != TAP_SCREEN || y < top || y >= top + per * rh) continue;
        int i = page * per + (y - top) / rh;
        if (i >= total) continue;
        beep();
        if (i == 0) {
            String s = S.ssid;
            if (!keyboard(cv, "WiFi 名", s) || s.isEmpty()) continue;
            ssid = s;
            open = false;                                            // 不知道有没有密码，接着让他输（可以留空）
            return true;
        }
        ssid = nets[i - 1].ssid;
        open = nets[i - 1].open;
        return true;
    }
}

// ---------------------------------------------------------------- 设置首页
static String masked(const String &s, const char *empty) {
    if (s.isEmpty()) return empty;
    return "已设置（" + String(s.length()) + " 位）";
}

bool settingsUI(M5Canvas &cv, const char *reason) {
    Settings T = S;                                                  // 改的是副本，保存时才生效
    const int top = 30, rh = 42;
    const Box cancelBtn{8, 204, 144, 32}, saveBtn{168, 204, 144, 32};
    cv.setFont(CN);
    cv.setTextSize(1);
    while (true) {
        cv.fillScreen(BG);
        titleBar(cv, "设置");
        if (WiFi.status() == WL_CONNECTED) {
            cv.setTextDatum(textdatum_t::middle_right);
            cv.setTextColor(OK_GREEN, PANEL);
            cv.drawString(WiFi.localIP().toString(), 314, 13);
        }
        if (reason) {
            cv.setTextDatum(textdatum_t::middle_left);
            cv.setTextColor(ACC, PANEL);
            cv.drawString(fitText(cv, reason, 100), 6, 13);
        }
        const char *labels[4] = {"WiFi", "WiFi 密码", "hub 地址", "hub 口令"};
        String values[4] = {T.ssid.isEmpty() ? "（未设置，点这里选）" : T.ssid, masked(T.pass, "（无密码）"),
                            T.hub.isEmpty() ? "（未设置）" : T.hub, masked(T.token, "（没设口令）")};
        for (int r = 0; r < 4; r++) {
            int y = top + r * rh;
            cv.fillRoundRect(6, y + 2, 308, rh - 4, 6, PANEL);
            cv.setTextDatum(textdatum_t::middle_left);
            cv.setTextColor(DIM, PANEL);
            cv.drawString(labels[r], 14, y + rh / 2);
            cv.setTextColor(TFT_WHITE, PANEL);
            cv.drawString(fitText(cv, values[r], 200), 96, y + rh / 2);
            cv.setTextColor(LINE, PANEL);
            cv.drawString(">", 302, y + rh / 2);
        }
        button(cv, cancelBtn, "取消", KEY2);
        button(cv, saveBtn, "保存并连接", OK_GREEN);
        cv.pushSprite(0, 0);

        int x, y;
        Tap t = waitTap(x, y);
        if (t == TAP_A || (t == TAP_SCREEN && cancelBtn.hit(x, y))) { beep(); return false; }
        if (t == TAP_C || (t == TAP_SCREEN && saveBtn.hit(x, y))) {
            beep();
            if (!T.hub.startsWith("http")) T.hub = "http://" + T.hub;
            S = T;
            settingsSave();
            return true;
        }
        if (t != TAP_SCREEN || y < top || y >= top + 4 * rh) continue;
        beep();
        switch ((y - top) / rh) {
            case 0: {
                String ssid;
                bool open = false;
                if (!pickWifi(cv, ssid, open)) break;
                bool same = ssid == T.ssid;
                T.ssid = ssid;
                if (open) {
                    T.pass = "";
                } else {
                    String p = same ? T.pass : "";
                    if (keyboard(cv, ("「" + fitText(cv, ssid, 150) + "」的密码").c_str(), p)) T.pass = p;
                }
                break;
            }
            case 1: keyboard(cv, "WiFi 密码", T.pass); break;
            case 2: {
                String h = T.hub.isEmpty() ? "http://192.168." : T.hub;
                if (keyboard(cv, "hub 地址，如 http://192.168.1.100:8765", h)) T.hub = h;
                break;
            }
            case 3: keyboard(cv, "hub 口令（hub/config.toml 的 token）", T.token); break;
        }
    }
}
