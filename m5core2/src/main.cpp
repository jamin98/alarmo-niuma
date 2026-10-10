// 牛马办公室 - M5Stack Core2 屏幕端
//
// Core2 的屏幕正好是 320x240，和 hub 渲染的画面一样大。这里不做任何绘制，只做三件事：
//   1. 连 WiFi，从 hub 取 /frame.rle 并显示：只传和上一帧相比变了的 40x40 小块（RGB565 + 行程压缩），
//      不用解 JPEG、没变的地方不重画；下载在 core 0 的网络任务里，画图在 core 1，两边同时干；
//   2. 把按键、滑动、摇一摇变成 hub 的 /api/button 请求；
//   3. 用振动马达和扬声器给「抽鞭子」加点反馈。
//
// 操作：
//   屏幕下方三个触摸键   左 = 一键鼓励    中 = 显示 / 隐藏聊天记录（在聊天记录页里 = 回到工位）    右 = 换肤
//   在屏幕上上下滑动      转滚轮：进入聊天记录并翻页（按「中」回到工位）
//   点一下屏幕上的某只牛马  点名：老板跑过去盯着它，并私聊它一句（hub 的 [buttons] poke_text）
//   按住某只牛马 / 按住「我」 说话私聊它 / 发到群里：用 Core2 自带的麦克风录，松手把录音传给 hub，
//                            由 Grok Bot 听写成文字发出去（跑 Grok Bot 的电脑上要装虚拟麦克风，见 README）
//   摇一摇设备            抽鞭子
//   按住右键 2 秒          设置：选 WiFi、输密码、改 hub 地址和口令（存在 Core2 里，不用重新编译）
#include <M5Unified.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include <algorithm>
#include <utility>
#include <vector>

#if __has_include("niuma_config.h")                // 可选的本地配置（不进 git）；没有就用示例里的默认值
#include "niuma_config.h"                           // 只当出厂默认值；真正用的设置在屏幕上改，存在 NVS 里
#else
#include "config.example.h"
#endif
#include "settings.h"

static constexpr size_t FRAME_MAX = 256 * 1024;   // 一帧最坏情况（每个像素都和旁边不一样）约 230KB

static M5Canvas canvas(&M5.Display);          // 屏幕上这一帧的完整副本：叠提示、恢复画面都靠它

// 三个缓冲区：网络任务往 back 里下载，下完和 mid 交换；主循环把 mid 换到 front 再画。
// 增量帧必须按顺序一帧不漏地画，所以主循环取走上一帧之前，网络任务不交下一帧（但可以先下载）。
static uint8_t *fbuf[3];
static int fbufLen[3];
static int backBuf = 0, midBuf = 1, frontBuf = 2;
static volatile bool midFresh = false;
static portMUX_TYPE frameMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool netPause = true;         // 连 WiFi、进设置时停下网络任务
static volatile bool needKeyframe = true;     // 下一帧要整帧（刚开机、从设置回来）
static bool awaitKey = true;                  // 主循环在等整帧：之前的增量帧都不画
static bool lcdStale = false;                 // 屏幕上画了别的（状态页、REC），下一帧用 canvas 整屏恢复
static bool noticeOnScreen = false;
static volatile uint32_t netFails = 0;
static uint32_t lastFrameAt = 0;
// 性能统计（每 3 秒报给 hub）
static volatile uint32_t stFetchMs = 0, stFetchN = 0;
static uint32_t stApplyMs = 0, stFrames = 0, stBytes = 0, stFull = 0, stAt = 0;
static uint32_t noticeUntil = 0;
static String notice;
static uint32_t vibeOffAt = 0;
static uint32_t lastShake = 0;
static bool wantSettings = false;             // 要进设置界面（按住右键，或在连不上的画面上点了屏幕）
static void pollInput();                      // 取画面时也要读输入，定义在后面

// ---------------------------------------------------------------- 小工具
static void showNotice(const String &s, uint32_t ms = 2000) {
    notice = s;
    noticeUntil = millis() + ms;
}

static void centerText(const String &s, int y, uint16_t color = TFT_WHITE) {
    M5.Display.setTextDatum(textdatum_t::middle_center);
    M5.Display.setTextColor(color, TFT_BLACK);
    M5.Display.drawString(s, M5.Display.width() / 2, y);
}

static void drawStatus(const String &line1, const String &line2 = "", const String &hint = "") {
    lcdStale = true;
    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setFont(&fonts::efontCN_14);
    M5.Display.setTextSize(2);
    centerText(line1, 96);
    M5.Display.setTextSize(1);
    centerText(line2, 136, TFT_LIGHTGREY);
    centerText(hint.length() ? hint : String("按住右键 2 秒 = 设置"), 212, 0x7BEF);
}

// 等一会儿；期间点了屏幕或按住右键就记下"要进设置"，提前返回
static void waitOrSettings(uint32_t ms) {
    uint32_t t0 = millis();
    while (millis() - t0 < ms) {
        M5.update();
        auto t = M5.Touch.getDetail();
        if ((t.wasClicked() && t.y < M5.Display.height()) || M5.BtnC.wasHold()) {
            wantSettings = true;
            return;
        }
        delay(20);
    }
}

static void click() {
    M5.Speaker.tone(1400, 25, 0);
}

static void whipFeedback() {                  // 啪！：一声脆响 + 振动
    M5.Speaker.tone(2200, 50, 0);
    M5.Speaker.tone(260, 160, 1);
    M5.Power.setVibration(255);
    vibeOffAt = millis() + 260;
}

// ---------------------------------------------------------------- 网络
static bool tryWifi(const String &ssid, const String &pass, uint32_t timeoutMs) {
    WiFi.disconnect();
    delay(50);
    WiFi.begin(ssid.c_str(), pass.c_str());
    uint32_t t0 = millis();
    int dots = 0;
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs && !wantSettings) {
        drawStatus("连接 WiFi" + String("....").substring(0, dots++ % 4), ssid, "点屏幕 = 修改设置");
        waitOrSettings(400);
    }
    return WiFi.status() == WL_CONNECTED;
}

// 先试当前选用的 WiFi；连不上就扫描附近，按信号强弱依次试其他连过的 WiFi。连上哪个就把它记到列表最前面。
static bool connectWifi(uint32_t timeoutMs = 15000) {
    if (WiFi.status() == WL_CONNECTED) return true;
    if (S.ssid.isEmpty() && savedNets.empty()) {
        wantSettings = true;
        return false;
    }
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);                     // 关省电，帧率稳很多
    if (S.ssid.length() && tryWifi(S.ssid, S.pass, timeoutMs)) {
        rememberNet(S.ssid, S.pass);
        return true;
    }
    if (wantSettings) return false;
    // 当前这个连不上：看看附近有没有别的连过的 WiFi
    std::vector<std::pair<int, int>> near;    // (信号, 列表下标)
    if (savedNets.size() > (S.ssid.length() ? 1u : 0u)) {
        drawStatus("找附近连过的 WiFi…", "", "点屏幕 = 修改设置");
        WiFi.disconnect();
        delay(100);
        int n = WiFi.scanNetworks();
        for (int k = 0; k < n; k++) {
            int i = findSavedNet(WiFi.SSID(k));
            if (i < 0 || savedNets[i].ssid == S.ssid) continue;
            auto it = std::find_if(near.begin(), near.end(), [&](const std::pair<int, int> &e) { return e.second == i; });
            if (it == near.end()) near.push_back({WiFi.RSSI(k), i});
            else it->first = std::max(it->first, (int)WiFi.RSSI(k));
        }
        WiFi.scanDelete();
        std::sort(near.begin(), near.end(), [](const std::pair<int, int> &x, const std::pair<int, int> &y) { return x.first > y.first; });
    }
    for (auto &c : near) {
        if (wantSettings) return false;
        SavedNet net = savedNets[c.second];
        if (tryWifi(net.ssid, net.pass, 10000)) {
            rememberNet(net.ssid, net.pass);  // 设为当前，下次开机先试它
            return true;
        }
    }
    return false;
}

// ---------------------------------------------------------------- 取画面（core 0 上的网络任务）
// /frame.rle?after=<帧号>&base=<屏幕上的帧号>：hub 有了新的一帧才返回（最多等 1 秒），只发和 base 比变了的部分。
static bool fetchInto(HTTPClient &http, WiFiClient &client, uint8_t *buf, int &len, uint32_t base, uint32_t &id) {
    static const char *hdrs[] = {"X-Frame-Id"};
    http.setReuse(true);                      // 一直复用同一个连接，省掉每帧握手
    http.setConnectTimeout(1500);
    http.setTimeout(3000);
    String url = S.hub + "/frame.rle?after=" + String(id) + "&base=" + String(base);
    if (!http.begin(client, url)) return false;
    http.collectHeaders(hdrs, 1);
    int code = http.GET();
    int size = http.getSize();
    if (code != 200 || size <= 11 || (size_t)size > FRAME_MAX) {
        http.end();
        return false;
    }
    uint32_t newId = http.header("X-Frame-Id").toInt();
    WiFiClient *s = http.getStreamPtr();
    int got = 0;
    uint32_t t0 = millis();
    while (got < size && millis() - t0 < 3000) {
        int n = s->available();
        if (n > 0) {                          // 批量读：readBytes 是一个字节一个字节地读，10KB 要一万次调用
            int r = s->read(buf + got, min(n, size - got));
            if (r > 0) got += r;
        }
        else if (!s->connected()) break;
        else delay(1);
    }
    http.end();
    if (got != size) return false;
    len = size;
    id = newId;
    return true;
}

static void netTask(void *) {
    WiFiClient client;
    HTTPClient http;
    uint32_t id = 0;                          // 最后交给主循环的帧号 = 主循环画完后屏幕上的帧
    for (;;) {
        if (netPause || WiFi.status() != WL_CONNECTED) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        if (midFresh) {                       // 主循环还没取走上一帧：等它（增量帧不能跳）
            vTaskDelay(pdMS_TO_TICKS(2));
            continue;
        }
        uint32_t base = id;
        if (needKeyframe) {
            needKeyframe = false;
            base = 0;
        }
        uint32_t t0 = millis();
        int len = 0;
        uint32_t newId = id;
        if (fetchInto(http, client, fbuf[backBuf], len, base, newId)) {
            stFetchMs = stFetchMs + (millis() - t0);
            stFetchN = stFetchN + 1;
            netFails = 0;
            id = newId;
            portENTER_CRITICAL(&frameMux);
            fbufLen[backBuf] = len;
            std::swap(backBuf, midBuf);
            midFresh = true;
            portEXIT_CRITICAL(&frameMux);
        } else {
            netFails = netFails + 1;
            vTaskDelay(pdMS_TO_TICKS(netFails > 3 ? 500 : 100));
        }
        uint32_t used = millis() - t0;
        if (used < FRAME_MS) vTaskDelay(pdMS_TO_TICKS(FRAME_MS - used));
    }
}

// 主循环拿下一帧（没有返回 false）
static bool takeFrame() {
    bool ok = false;
    portENTER_CRITICAL(&frameMux);
    if (midFresh) {
        std::swap(frontBuf, midBuf);
        midFresh = false;
        ok = true;
    }
    portEXIT_CRITICAL(&frameMux);
    return ok;
}

// 画一帧：b"NRLE" <帧号 u32><整帧 u8><矩形数 u16>，每个矩形 <x y w h u16><字节数 u32> + 行程数据
// （每段 3 字节：个数-1、RGB565 低字节、高字节；逐行）。每个矩形先解到内存里，再写进 canvas 和屏幕。
static uint16_t tileBuf[320 * 40];

static uint16_t rd16(const uint8_t *p) { return p[0] | (p[1] << 8); }
static uint32_t rd32(const uint8_t *p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

static bool applyFrame(const uint8_t *b, int len, bool toLcd) {
    if (len < 11 || memcmp(b, "NRLE", 4) != 0) return false;
    int n = rd16(b + 9);
    const uint8_t *p = b + 11, *end = b + len;
    for (int r = 0; r < n; r++) {
        if (p + 12 > end) return false;
        int x = rd16(p), y = rd16(p + 2), w = rd16(p + 4), h = rd16(p + 6);
        uint32_t nb = rd32(p + 8);
        p += 12;
        if (x + w > 320 || y + h > 240 || w * h > 320 * 40 || p + nb > end || nb % 3) return false;
        int o = 0;
        for (const uint8_t *q = p; q < p + nb; q += 3) {
            int cnt = q[0] + 1;
            uint16_t c = q[1] | (q[2] << 8);
            if (o + cnt > w * h) return false;
            for (int k = 0; k < cnt; k++) tileBuf[o++] = c;
        }
        if (o != w * h) return false;
        p += nb;
        canvas.pushImage(x, y, w, h, (const lgfx::rgb565_t *)tileBuf);
        if (toLcd) M5.Display.pushImage(x, y, w, h, (const lgfx::rgb565_t *)tileBuf);
    }
    return true;
}

static void drawNotice() {
    M5.Display.fillRoundRect(60, 205, 200, 24, 6, TFT_BLACK);
    M5.Display.setFont(&fonts::efontCN_14);
    M5.Display.setTextSize(1);
    M5.Display.setTextDatum(textdatum_t::middle_center);
    M5.Display.setTextColor(TFT_YELLOW, TFT_BLACK);
    M5.Display.drawString(notice, 160, 217);
}

// 往 hub 的某个接口 POST 一段 JSON。reply 不为空时带回响应内容。
static bool postHub(const char *path, const String &body, String *reply = nullptr) {
    HTTPClient http;
    http.setConnectTimeout(1500);
    http.setTimeout(2500);
    if (!http.begin(S.hub + path)) return false;
    http.addHeader("Content-Type", "application/json");
    if (S.token.length()) http.addHeader("X-Token", S.token);
    int code = http.POST(body);
    if (reply && code == 200) *reply = http.getString();
    http.end();
    if (code == 401) showNotice("Bad token");
    else if (code != 200) showNotice("Hub error " + String(code));
    return code == 200;
}

// ---------------------------------------------------------------- 按住说话
static constexpr uint32_t MIC_RATE = 16000;
static constexpr size_t MIC_MAX = MIC_RATE * 60;   // 最长 60 秒（约 1.9MB，放 PSRAM）
static constexpr size_t MIC_CHUNK = MIC_RATE / 10;

static void put32(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
static void put16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }

// 一直录到手指离开屏幕，然后把 16kHz 单声道 WAV 传给 hub。录音期间不取画面，屏幕上叠一个 REC。
static void recordVoice() {
    uint8_t *buf = (uint8_t *)ps_malloc(44 + MIC_MAX * 2);
    if (!buf) {
        showNotice("No memory");
        postHub("/api/voice", "{\"action\":\"cancel\"}");
        return;
    }
    int16_t *pcm = (int16_t *)(buf + 44);
    M5.Speaker.end();                         // Core2 的喇叭和麦克风共用 I2S，录音时要先关喇叭
    M5.Mic.begin();
    M5.Display.fillRoundRect(214, 6, 100, 30, 8, TFT_BLACK);
    M5.Display.fillCircle(232, 21, 8, TFT_RED);
    M5.Display.setTextColor(TFT_WHITE);
    M5.Display.drawString("REC", 248, 13);
    size_t n = 0;
    while (n + MIC_CHUNK <= MIC_MAX) {
        M5.update();
        if (!M5.Touch.getDetail().isPressed()) break;
        if (M5.Mic.record(pcm + n, MIC_CHUNK, MIC_RATE)) n += MIC_CHUNK;
        else delay(2);
    }
    while (M5.Mic.isRecording()) delay(1);
    M5.Mic.end();
    M5.Speaker.begin();
    click();
    lcdStale = true;
    memcpy(buf, "RIFF", 4); put32(buf + 4, 36 + n * 2); memcpy(buf + 8, "WAVEfmt ", 8);
    put32(buf + 16, 16); put16(buf + 20, 1); put16(buf + 22, 1); put32(buf + 24, MIC_RATE);
    put32(buf + 28, MIC_RATE * 2); put16(buf + 32, 2); put16(buf + 34, 16); memcpy(buf + 36, "data", 4); put32(buf + 40, n * 2);
    HTTPClient http;
    http.setConnectTimeout(1500);
    http.setTimeout(15000);
    if (http.begin(S.hub + "/api/voice/clip")) {
        http.addHeader("Content-Type", "audio/wav");
        if (S.token.length()) http.addHeader("X-Token", S.token);
        int code = http.POST(buf, 44 + n * 2);
        http.end();
        showNotice(code == 200 ? "Sent to Grok" : "Upload error " + String(code));
    }
    free(buf);
}

// 往 hub 发按键。button = "dial" | "back" | "notify" | "chat"；dial != 0 时发滚轮转动（度）。
static bool sendButton(const char *button, int dial = 0) {
    String body = dial ? String("{\"dial\":") + dial + "}" : String("{\"button\":\"") + button + "\"}";
    return postHub("/api/button", body);
}

// ---------------------------------------------------------------- 输入
// 读输入（pollInput）和发请求（processInput）分开：pollInput 每几毫秒调一次（取画面、等下一帧时也调），
// 读到的操作先放进队列并马上响一声；processInput 每帧开头把队列里的请求发给 hub。
// 这样按得再快也不会漏，按下去立刻有反馈，不用等这一帧画面下载完。
enum EvType : uint8_t { EV_ENCOURAGE, EV_CHAT, EV_SKIN, EV_SETTINGS, EV_HOLD, EV_TAP, EV_FLICK };
struct Ev {
    EvType type;
    int16_t x, y;
};
static Ev evq[16];
static uint8_t evHead = 0, evTail = 0;

static void pushEv(EvType type, int x = 0, int y = 0) {
    uint8_t next = (evTail + 1) % 16;
    if (next == evHead) return;               // 满了就丢（不太可能）
    evq[evTail] = {type, (int16_t)x, (int16_t)y};
    evTail = next;
}

static void pollInput() {
    M5.update();
    if (M5.BtnA.wasPressed()) { click(); pushEv(EV_ENCOURAGE); }     // 左：一按下就算
    if (M5.BtnB.wasPressed()) { click(); pushEv(EV_CHAT); }          // 中：一按下就算
    if (M5.BtnC.wasClicked()) { click(); pushEv(EV_SKIN); }          // 右：点一下换肤（松手时才知道不是按住）
    if (M5.BtnC.wasHold()) { click(); pushEv(EV_SETTINGS); }         // 按住右键 2 秒：设置

    auto t = M5.Touch.getDetail();
    if (t.y >= M5.Display.height()) return;                          // 屏幕下方是三个触摸键，上面已经处理
    if (t.wasHold()) { click(); pushEv(EV_HOLD, t.x, t.y); }
    else if (t.wasClicked()) { click(); pushEv(EV_TAP, t.x, t.y); }
    else if (t.wasFlicked()) {
        int dy = t.distanceY();
        if (abs(dy) > 25 && abs(dy) > abs(t.distanceX())) { click(); pushEv(EV_FLICK, 0, dy); }
    }
}

// 等 ms 毫秒，期间一直读输入
static void pollFor(uint32_t ms) {
    uint32_t t0 = millis();
    do {
        pollInput();
        delay(5);
    } while (millis() - t0 < ms);
}

static void processInput() {
    while (evHead != evTail) {
        Ev e = evq[evHead];
        evHead = (evHead + 1) % 16;
        switch (e.type) {
            case EV_ENCOURAGE: sendButton("back"); break;
            case EV_CHAT: sendButton("chat"); break;                     // hub：显示 / 隐藏聊天记录
            case EV_SKIN: sendButton("notify"); break;
            case EV_SETTINGS: wantSettings = true; return;
            case EV_TAP: postHub("/api/poke", String("{\"x\":") + e.x + ",\"y\":" + e.y + "}"); break;
            case EV_FLICK: sendButton("", e.y > 0 ? 30 : -30); break;    // 往下滑看更早的消息，往上滑回到最新
            case EV_HOLD: {                                              // 按住：录音（hub 按位置决定私聊谁 / 发群里）
                if (!M5.Touch.getDetail().isPressed()) break;            // 已经松手了
                String reply;
                if (postHub("/api/voice", String("{\"action\":\"start\",\"x\":") + e.x + ",\"y\":" + e.y + "}", &reply)
                    && reply.indexOf("\"ok\": true") >= 0) recordVoice();  // 没按中牛马或「我」时 ok 是 false
                evHead = evTail;                                         // 录音期间的操作作废
                return;
            }
        }
    }

    // 摇一摇 = 抽鞭子（hub 自己有冷却，这里再限一下 3 秒）
    if (M5.Imu.isEnabled() && M5.Imu.update()) {
        auto d = M5.Imu.getImuData();
        float g = sqrtf(d.accel.x * d.accel.x + d.accel.y * d.accel.y + d.accel.z * d.accel.z);
        if (g > SHAKE_G && millis() - lastShake > 3000) {
            lastShake = millis();
            whipFeedback();
            sendButton("dial");
        }
    }

    if (vibeOffAt && millis() > vibeOffAt) {
        M5.Power.setVibration(0);
        vibeOffAt = 0;
    }
}

// ---------------------------------------------------------------- 设置
static void openSettings(const char *reason) {
    wantSettings = false;
    netPause = true;                          // 扫描 WiFi、改地址时别让网络任务在旁边连 hub
    delay(100);
    if (settingsUI(canvas, reason)) {         // 保存了：用新设置重连
        WiFi.disconnect(true);
        delay(300);
        netFails = 0;
        showNotice("已保存设置");
    }
    needKeyframe = true;                      // 设置界面用掉了 canvas：要一整帧重新开始
    awaitKey = true;
    lastFrameAt = millis();
}

// ---------------------------------------------------------------- 性能统计
static void reportStats() {
    if (millis() - stAt < 3000) return;
    float sec = (millis() - stAt) / 1000.0f;
    String body = String("{\"fps\":") + String(stFrames / sec, 1) +
                  ",\"fetch_ms\":" + (stFetchN ? stFetchMs / stFetchN : 0) +
                  ",\"draw_ms\":" + (stFrames ? stApplyMs / stFrames : 0) +
                  ",\"kb_per_frame\":" + (stFrames ? stBytes / stFrames / 1024 : 0) +
                  ",\"full_frames\":" + stFull + ",\"rssi\":" + WiFi.RSSI() +
                  ",\"free_heap\":" + ESP.getFreeHeap() + "}";
    stAt = millis();
    stFetchMs = stFetchN = 0;
    stApplyMs = stFrames = stBytes = stFull = 0;
    postHub("/api/device_stats", body);
}

// ---------------------------------------------------------------- 主流程
void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setBrightness(BRIGHTNESS);
    M5.Speaker.setVolume(140);
    Serial.begin(115200);

    for (auto &b : fbuf) b = (uint8_t *)ps_malloc(FRAME_MAX);  // Core2 有 8MB PSRAM
    canvas.setColorDepth(16);
    canvas.setPsram(true);
    canvas.createSprite(320, 240);
    canvas.setFont(&fonts::efontCN_14);        // 提示文字有中文
    M5.BtnC.setHoldThresh(2000);              // 右键：点一下换肤，按住 2 秒进设置

    settingsLoad();
    xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);   // 下载画面放在 core 0
    drawStatus("牛马办公室", "启动中…");
    if (S.ssid.isEmpty() && savedNets.empty()) openSettings("先设置 WiFi");
    if (!connectWifi()) {
        drawStatus("WiFi 连不上", S.ssid, "点屏幕 = 修改设置");
        waitOrSettings(3000);
    }
}

void loop() {
    if (wantSettings) {
        openSettings(WiFi.status() == WL_CONNECTED ? nullptr : "WiFi 连不上");
        return;
    }
    pollInput();
    processInput();
    if (wantSettings) return;

    if (WiFi.status() != WL_CONNECTED) {
        netPause = true;
        if (!connectWifi(8000) && !wantSettings) {
            drawStatus("WiFi 连不上", S.ssid + "  重试中…", "点屏幕 = 修改设置");
            waitOrSettings(2000);
        }
        lastFrameAt = millis();
        return;
    }
    netPause = false;

    if (takeFrame()) {                        // 有新的一帧：把变了的块画到 canvas 和屏幕上
        const uint8_t *b = fbuf[frontBuf];
        int len = fbufLen[frontBuf];
        bool full = len > 8 && b[8];
        uint32_t t0 = millis();
        if (awaitKey && !full) {
            needKeyframe = true;              // 还在等整帧，这帧不画
        } else if (!applyFrame(b, len, !lcdStale)) {
            awaitKey = true;                  // 数据不对：从整帧重新开始
            needKeyframe = true;
        } else {
            awaitKey = false;
            if (lcdStale) {                   // 屏幕上之前画了别的：整屏恢复
                canvas.pushSprite(0, 0);
                lcdStale = false;
            }
            if (millis() < noticeUntil) {     // 叠一条小提示（口令错误之类），直接画在屏幕上
                drawNotice();
                noticeOnScreen = true;
            } else if (noticeOnScreen) {      // 提示到时间了：用 canvas 把那块恢复
                canvas.pushSprite(0, 0);
                noticeOnScreen = false;
            }
            stApplyMs += millis() - t0;
            stFrames++;
            stBytes += len;
            stFull += full;
        }
        lastFrameAt = millis();
        reportStats();
    } else if (netFails >= 3 && millis() - lastFrameAt > 2000) {
        drawStatus("hub 连不上", S.hub, "点屏幕 = 修改设置");
        waitOrSettings(1000);
        if (wantSettings) openSettings("hub 连不上");
    }
    pollFor(3);                               // 等下一帧的时候一直读输入
}
