// 牛马办公室 - M5Stack Core2 屏幕端
//
// Core2 的屏幕正好是 320x240，和 hub 渲染的画面一样大。这里不做任何绘制，只做三件事：
//   1. 连 WiFi，连上 hub 的 /frame.stream 一直收画面：hub 有新帧就推一条增量帧——只含和上一帧相比变了的
//      16x16 小块（RGB565 + 行程压缩，整条再 deflate 一遍），不用解 JPEG、没变的地方不重画；收数据在 core 0 的网络任务里，画图在 core 1；
//   2. 把按键、滑动、摇一摇变成 hub 的 /api/button 请求（在后台任务里发，不卡画面）；
//   3. 用振动马达和扬声器给「抽鞭子」加点反馈。
//
// 操作：
//   屏幕下方三个触摸键   左 = 一键鼓励    中 = 显示 / 隐藏聊天记录（在聊天记录页里 = 回到工位）    右 = 换肤
//   按住中键              按住期间看历史聊天记录（可以同时在屏幕上滑动翻页），松手回到工位
//   在屏幕上上下滑动     转滚轮：进入聊天记录并翻页（按「中」回到工位）
//   点一下屏幕上的某只牛马  点名：老板跑过去盯着它，并私聊它一句（hub 的 [buttons] poke_text）
//   按住某只牛马 / 按住「我」 说话私聊它 / 发到群里：用 Core2 自带的麦克风录，松手把录音传给 hub，
//                            由 Grok Bot 听写成文字发出去（跑 Grok Bot 的电脑上要装虚拟麦克风，见 README）
//   摇一摇设备            抽鞭子（要来回摇几下才算，碰一下、放桌上不算）
//   按住右键 2 秒          设置：选 WiFi、输密码、改 hub 地址和口令（存在 Core2 里，不用重新编译）
#include <M5Unified.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp32/rom/miniz.h>                    // ROM 里自带的 tinfl：解 hub 推过来的 deflate 帧

#include <algorithm>
#include <atomic>
#include <utility>
#include <vector>

#if __has_include("niuma_config.h")                // 可选的本地配置（不进 git）；没有就用示例里的默认值
#include "niuma_config.h"                           // 只当出厂默认值；真正用的设置在屏幕上改，存在 NVS 里
#else
#include "config.example.h"
#endif
#include "settings.h"

#ifndef SHAKE_HITS
#define SHAKE_HITS 3                                // 旧的 niuma_config.h 里没有这项
#endif

static constexpr size_t FRAME_MAX = 256 * 1024;   // 一帧最坏情况（每个像素都和旁边不一样）约 230KB
static constexpr const char *FW_VERSION = "2026.10.10-slownet";   // 报给 hub，排查时确认烧的是哪一版

static M5Canvas canvas(&M5.Display);          // 屏幕上这一帧的完整副本：叠提示、恢复画面都靠它

// 三个缓冲区：网络任务往 back 里下载，下完和 mid 交换；主循环把 mid 换到 front 再画。
// 增量帧必须按顺序一帧不漏地画，所以主循环取走上一帧之前，网络任务不交下一帧（但可以先下载）。
static uint8_t *fbuf[3];
static uint8_t *zbuf;                         // 收到的 deflate 数据，网络任务解到 fbuf[back] 里
static tinfl_decompressor *inflator;          // 解压状态约 11KB，放内部 RAM（不能放网络任务的栈上）
static int fbufLen[3];
static int backBuf = 0, midBuf = 1, frontBuf = 2;
static volatile bool midFresh = false;
static portMUX_TYPE frameMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool netPause = true;         // 连 WiFi、进设置时停下网络任务
static volatile bool needKeyframe = true;     // 下一帧要整帧（刚开机、从设置回来）
static bool awaitKey = true;                  // 主循环在等整帧：之前的增量帧都不画
static bool lcdStale = false;                 // 屏幕上画了别的（状态页、REC），下一帧用 canvas 整屏恢复
static bool noticeOnScreen = false;
static bool noticeDirty = false;              // 提示换了内容，或者新的一帧可能盖掉了它：要重画
static volatile uint32_t netFails = 0;
static uint32_t lastFrameAt = 0;
// 性能统计（每 3 秒报给 hub）
static volatile uint32_t stWaitMs = 0, stRecvMs = 0, stFetchN = 0, stInflateMs = 0, stZBytes = 0;
static uint32_t stApplyMs = 0, stFrames = 0, stBytes = 0, stFull = 0, stAt = 0;
static uint32_t noticeUntil = 0;
static String notice;
static uint32_t vibeOffAt = 0;
static uint32_t lastShake = 0;
static volatile bool hubErr = false;          // 后台发请求出了错：错误码交给主循环显示（showNotice 只在主循环里调）
static volatile int hubErrCode = 0;
static bool wantSettings = false;             // 要进设置界面（按住右键，或在连不上的画面上点了屏幕）
static void pollInput();                      // 取画面时也要读输入，定义在后面

// ---------------------------------------------------------------- 小工具
static void showNotice(const String &s, uint32_t ms = 2000) {
    notice = s;
    noticeUntil = millis() + ms;
    noticeDirty = true;
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
// 连 hub 的 /frame.stream?z=1（HTTP/1.0，响应一直不结束），然后一条一条读：<长度 u32> + deflate 过的 NRLE 增量帧，
// 在这个任务里（core 0）解压，主循环（core 1）同时在画上一帧。
// hub 每推一条都等这边在 TCP 层确认收到才推下一条，所以路上最多一帧。要整帧时断开重连（新连接第一条是整帧）。
// 以前是每帧发一次 HTTP 请求：实测 ESP32 的 HTTPClient 收完一帧到发出下一个请求要 ~100ms，帧率卡在 9~10。
static uint16_t rd16(const uint8_t *p) { return p[0] | (p[1] << 8); }
static uint32_t rd32(const uint8_t *p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

static bool parseHub(String &host, uint16_t &port) {
    String u = S.hub;
    if (u.startsWith("http://")) u = u.substring(7);
    int slash = u.indexOf('/');
    if (slash >= 0) u = u.substring(0, slash);
    int colon = u.lastIndexOf(':');
    host = colon > 0 ? u.substring(0, colon) : u;
    port = colon > 0 ? u.substring(colon + 1).toInt() : 80;
    return host.length() && port;
}

// 读满 n 个字节；连接断了或 timeoutMs 内一个字节都没来就返回 false
static bool readFull(WiFiClient &c, uint8_t *buf, size_t n, uint32_t timeoutMs) {
    size_t got = 0;
    uint32_t t0 = millis();
    while (got < n) {
        int a = c.available();
        if (a > 0) {
            int r = c.read(buf + got, min((size_t)a, n - got));
            if (r > 0) {
                got += r;
                t0 = millis();
            }
        } else if (!c.connected() || millis() - t0 > timeoutMs) {
            return false;
        } else {
            delay(1);
        }
    }
    return true;
}

static bool openStream(WiFiClient &c) {
    String host;
    uint16_t port;
    c.stop();
    if (!parseHub(host, port) || !c.connect(host.c_str(), port, 2000)) return false;
    c.setNoDelay(true);
    c.print("GET /frame.stream?z=1 HTTP/1.0\r\nHost: " + host + "\r\n\r\n");
    String line;                              // 读响应头，读到空行为止；第一行要是 200
    bool ok = false;
    int lines = 0;
    uint32_t t0 = millis();
    while (millis() - t0 < 3000) {
        if (!c.available()) {
            if (!c.connected()) return false;
            delay(1);
            continue;
        }
        char ch = c.read();
        if (ch == '\r') continue;
        if (ch != '\n') {
            if (line.length() < 200) line += ch;
            continue;
        }
        if (lines++ == 0) ok = line.indexOf(" 200") > 0;
        else if (line.isEmpty()) return ok;
        line = "";
    }
    return false;
}

static void netTask(void *) {
    WiFiClient c;
    bool open = false;
    for (;;) {
        if (netPause || WiFi.status() != WL_CONNECTED) {
            if (open) c.stop();
            open = false;
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        if (needKeyframe) {                   // 要整帧：断开重连，新连接的第一条一定是整帧
            needKeyframe = false;
            if (open) c.stop();
            open = false;
        }
        if (!open) {
            open = openStream(c);
            if (!open) {
                netFails = netFails + 1;
                vTaskDelay(pdMS_TO_TICKS(netFails > 3 ? 500 : 200));
                continue;
            }
        }
        if (midFresh) {                       // 主循环还没取走上一帧：等它（增量帧不能跳）
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }
        uint8_t hdr[4];
        uint32_t t0 = millis();
        bool ok = readFull(c, hdr, 4, 3000);  // hub 每帧都推（画面没变时只有十几个字节），3 秒没动静就是断了
        uint32_t t1 = millis(), n = ok ? rd32(hdr) : 0;
        ok = ok && n >= 2 && n <= FRAME_MAX && readFull(c, zbuf, n, 3000);
        uint32_t t2 = millis();
        size_t inLen = n, outLen = FRAME_MAX;
        if (ok) {
            tinfl_init(inflator);
            tinfl_status st = tinfl_decompress(inflator, zbuf, &inLen, fbuf[backBuf], fbuf[backBuf], &outLen,
                                               TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
            ok = st == TINFL_STATUS_DONE && outLen >= 11;
        }
        if (!ok) {
            c.stop();
            open = false;
            netFails = netFails + 1;
            continue;
        }
        stWaitMs = stWaitMs + (t1 - t0);
        stRecvMs = stRecvMs + (t2 - t1);
        stInflateMs = stInflateMs + (millis() - t2);
        stZBytes = stZBytes + n;
        stFetchN = stFetchN + 1;
        netFails = 0;
        portENTER_CRITICAL(&frameMux);
        fbufLen[backBuf] = outLen;
        std::swap(backBuf, midBuf);
        midFresh = true;
        portEXIT_CRITICAL(&frameMux);
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
// 解出来的像素按屏幕 / canvas 自己的字节序（高字节在前）存，写进去是直接拷贝，不用逐点转换。
static uint16_t tileBuf[320 * 40];           // hub 现在发 16 行高的矩形；旧版 hub 是 40 行

static bool applyRects(const uint8_t *b, int len, bool toLcd) {
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
            uint16_t c = q[2] | (q[1] << 8);     // 存成 swap565
            if (o + cnt > w * h) return false;
            for (int k = 0; k < cnt; k++) tileBuf[o++] = c;
        }
        if (o != w * h) return false;
        p += nb;
        canvas.pushImage(x, y, w, h, (const lgfx::swap565_t *)tileBuf);
        if (toLcd) M5.Display.pushImage(x, y, w, h, (const lgfx::swap565_t *)tileBuf);
    }
    return true;
}

static bool applyFrame(const uint8_t *b, int len, bool toLcd) {
    if (toLcd) M5.Display.startWrite();       // 所有矩形在一次 SPI 事务里写完，不用每块都重新占总线
    bool ok = applyRects(b, len, toLcd);
    if (toLcd) M5.Display.endWrite();
    return ok;
}

static void drawNotice() {
    M5.Display.fillRoundRect(60, 205, 200, 24, 6, TFT_BLACK);
    M5.Display.setFont(&fonts::efontCN_14);
    M5.Display.setTextSize(1);
    M5.Display.setTextDatum(textdatum_t::middle_center);
    M5.Display.setTextColor(TFT_YELLOW, TFT_BLACK);
    M5.Display.drawString(notice, 160, 217);
}

static int postOnce(HTTPClient &http, const char *path, const String &body, String *reply) {
    http.setConnectTimeout(1500);
    http.setTimeout(2500);
    if (!http.begin(S.hub + path)) return HTTPC_ERROR_CONNECTION_REFUSED;
    http.addHeader("Content-Type", "application/json");
    if (S.token.length()) http.addHeader("X-Token", S.token);
    int code = http.POST(body);
    if (reply && code == 200) *reply = http.getString();
    http.end();                               // keep-alive 时连接留着，下次直接用
    return code;
}

// 往 hub 的某个接口 POST 一段 JSON。reply 不为空时带回响应内容。
// 传了 conn 就复用它的连接（keep-alive）：网络差时每次重新建 TCP 连接是最慢的一步，往往比请求本身还慢。
static bool postHub(const char *path, const String &body, String *reply = nullptr, HTTPClient *conn = nullptr) {
    HTTPClient local;
    HTTPClient &http = conn ? *conn : local;
    int code = postOnce(http, path, body, reply);
    if (conn && code <= HTTPC_ERROR_SEND_HEADER_FAILED && code >= HTTPC_ERROR_CONNECTION_LOST)
        code = postOnce(http, path, body, reply);   // 留着的连接已经被 hub 关了（空闲太久）：重新连一次
    if (code != 200) {                        // 可能在后台任务里：只记下来，主循环去显示
        hubErrCode = code;
        hubErr = true;
    }
    return code == 200;
}

// 按键、点名、滑动、性能统计都放到后台任务（core 0）里发：一次 POST 要 100ms 以上，
// 以前在主循环里发，按一下键画面就停一下（换肤时最明显）。按顺序一条条发，队列满了就丢。
// 网络差时一次 POST 要好几秒，按了几下就会排队、过很久才一下下生效（比如连着换好几次肤），所以：
//   - 同一种操作上一下还没发完，再按就不发了（kind，见 processInput）；
//   - 在队列里等太久（REQ_STALE_MS）的直接丢掉，不在用户已经不想要的时候才生效；
//   - 一直用同一个连接发。
static constexpr uint32_t REQ_STALE_MS = 2000;
struct HubReq {
    char path[24];
    char body[232];
    uint32_t at;                              // 什么时候按的
    int8_t kind;                              // 哪种操作（EvType），-1 = 不算（性能统计）
    bool keep;                                // 再旧也要发（比如「松开中键」，不发 hub 就一直停在聊天记录页）
};
static QueueHandle_t reqQ = nullptr;
static volatile bool sending = false;         // 后台任务正在发：进设置前要等它发完（设置会改 hub 地址）
static std::atomic<uint32_t> pendingKinds{0}; // 每种操作一位：还在排队或正在发
static volatile uint32_t staleDropped = 0;    // 因为等太久丢掉了几个，主循环去提示
static volatile bool connReset = false;       // 改了 hub 地址：留着的连接作废

static bool postAsync(const char *path, const String &body, int kind = -1, bool keep = false) {
    HubReq r;
    strlcpy(r.path, path, sizeof r.path);
    strlcpy(r.body, body.c_str(), sizeof r.body);
    r.at = millis();
    r.kind = kind;
    r.keep = keep;
    if (kind >= 0) pendingKinds |= 1u << kind;      // 先标上再入队：后台任务可能马上发完并清掉它
    if (reqQ && xQueueSend(reqQ, &r, 0) == pdTRUE) return true;
    if (kind >= 0) pendingKinds &= ~(1u << kind);
    return false;
}

static void sendTask(void *) {
    HubReq r;
    HTTPClient http;
    http.setReuse(true);
    for (;;) {
        if (xQueueReceive(reqQ, &r, portMAX_DELAY) != pdTRUE) continue;
        sending = true;
        if (connReset) {
            connReset = false;
            http.setReuse(false);
            http.end();                       // 不复用时 end() 会真的关掉连接
            http.setReuse(true);
        }
        if (!r.keep && millis() - r.at > REQ_STALE_MS) {
            if (r.kind >= 0) staleDropped = staleDropped + 1;
        } else if (!netPause && WiFi.status() == WL_CONNECTED) {
            postHub(r.path, r.body, nullptr, &http);
        }
        if (r.kind >= 0) pendingKinds &= ~(1u << r.kind);
        sending = false;
    }
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

// 往 hub 发按键。button = "dial" | "back" | "notify" | "chat" | "chat_hold" | "chat_release"；dial != 0 时发滚轮转动（度）。
static void sendButton(int kind, const char *button, int dial = 0, bool keep = false) {
    String body = dial ? String("{\"dial\":") + dial + "}" : String("{\"button\":\"") + button + "\"}";
    postAsync("/api/button", body, kind, keep);
}

// ---------------------------------------------------------------- 输入
// 读输入（pollInput）和发请求（processInput）分开：pollInput 每几毫秒调一次（取画面、等下一帧时也调），
// 读到的操作先放进队列并马上响一声；processInput 每帧开头把队列里的请求交给后台任务发给 hub。
// 这样按得再快也不会漏，按下去立刻有反馈，不用等这一帧画面下载完。
enum EvType : uint8_t { EV_ENCOURAGE, EV_CHAT, EV_HISTORY, EV_HISTORY_END, EV_SKIN, EV_SETTINGS, EV_HOLD, EV_TAP, EV_FLICK, EV_WHIP };
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

// 摇一摇：加速度超过 SHAKE_G 算「一下」（相隔至少 80ms，过了峰值回落才算下一下），
// SHAKE_WINDOW_MS 内有 SHAKE_HITS 下才算在摇。碰一下、放到桌上、掉一下只有一两下，不会误触发。
static constexpr uint32_t SHAKE_WINDOW_MS = 1200;
static uint32_t shakeHits[8];
static uint8_t shakeN = 0;                    // shakeHits 里记了几下（环形，最多 8 下）
static bool shakeHigh = false;
static uint32_t lastImuAt = 0;

static void pollShake() {
    if (!M5.Imu.isEnabled() || millis() - lastImuAt < 10) return;
    lastImuAt = millis();
    if (!M5.Imu.update()) return;
    auto d = M5.Imu.getImuData();
    float g = sqrtf(d.accel.x * d.accel.x + d.accel.y * d.accel.y + d.accel.z * d.accel.z);
    uint32_t now = millis();
    if (g < SHAKE_G - 0.5f) {                 // 回落了：可以算下一下
        shakeHigh = false;
        return;
    }
    if (g <= SHAKE_G || shakeHigh) return;
    shakeHigh = true;
    constexpr int need = SHAKE_HITS < 1 ? 1 : SHAKE_HITS > 8 ? 8 : SHAKE_HITS;
    if (shakeN && now - shakeHits[(shakeN - 1) % 8] < 80) return;
    shakeHits[shakeN % 8] = now;
    shakeN++;
    if (shakeN >= need && now - shakeHits[(shakeN - need) % 8] <= SHAKE_WINDOW_MS && now - lastShake > 3000) {
        lastShake = now;
        shakeN = 0;
        whipFeedback();
        pushEv(EV_WHIP);
    }
    if (shakeN >= 16) shakeN = 8 + shakeN % 8;   // 别溢出，保持下标对得上
}

static void pollInput() {
    M5.update();
    if (M5.BtnA.wasPressed()) { click(); pushEv(EV_ENCOURAGE); }     // 左：一按下就算
    // 中：点一下切换聊天内容（要分「点一下」和「按住」，所以松手时才触发）；按住 = 看历史聊天记录，松手回到工位
    static bool historyHeld = false;
    if (M5.BtnB.wasClicked()) { click(); pushEv(EV_CHAT); }
    if (M5.BtnB.wasHold()) { click(); historyHeld = true; pushEv(EV_HISTORY); }
    if (M5.BtnB.wasReleased() && historyHeld) { historyHeld = false; pushEv(EV_HISTORY_END); }
    if (M5.BtnC.wasClicked()) { click(); pushEv(EV_SKIN); }          // 右：点一下换肤（松手时才知道不是按住）
    if (M5.BtnC.wasHold()) { click(); pushEv(EV_SETTINGS); }         // 按住右键 2 秒：设置
    pollShake();

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

// 网络慢的时候：同一种操作上一下还没发到 hub，这一下就不发了，只提示一下（不然网络好了会一口气连换好几次肤）
static bool stillSending(EvType t) {
    if (!(pendingKinds & (1u << t))) return false;
    showNotice("网络慢，上一下还在发…");
    return true;
}

static void processInput() {
    while (evHead != evTail) {
        Ev e = evq[evHead];
        evHead = (evHead + 1) % 16;
        if (stillSending(e.type)) continue;
        switch (e.type) {
            case EV_ENCOURAGE: sendButton(e.type, "back"); break;
            case EV_CHAT: sendButton(e.type, "chat"); break;             // hub：显示 / 隐藏聊天记录
            case EV_HISTORY: sendButton(-1, "chat_hold"); break;         // hub：打开聊天记录页（和松开成对，不拦）
            case EV_HISTORY_END: sendButton(-1, "chat_release", 0, true); break;   // hub：回到工位（再晚也要发）
            case EV_SKIN: sendButton(e.type, "notify"); break;
            case EV_SETTINGS: wantSettings = true; return;
            case EV_TAP: postAsync("/api/poke", String("{\"x\":") + e.x + ",\"y\":" + e.y + "}", e.type); break;
            case EV_FLICK: sendButton(e.type, "", e.y > 0 ? 30 : -30); break;   // 往下滑看更早的消息，往上滑回到最新
            case EV_WHIP: sendButton(e.type, "dial"); break;             // 摇一摇 = 抽鞭子（hub 自己也有冷却）
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

    if (hubErr) {
        hubErr = false;
        showNotice(hubErrCode == 401 ? String("Bad token") : "Hub error " + String(hubErrCode));
    }
    if (staleDropped) {
        staleDropped = 0;
        showNotice("网络太慢，刚才的操作没发出去");
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
    if (reqQ) xQueueReset(reqQ);              // 没发的按键作废
    for (uint32_t t0 = millis(); sending && millis() - t0 < 4000;) delay(10);   // 正在发的等它发完（设置会改 hub 地址）
    delay(100);
    if (settingsUI(canvas, reason)) {         // 保存了：用新设置重连
        WiFi.disconnect(true);
        delay(300);
        netFails = 0;
        connReset = true;                     // hub 地址可能改了：发按键的连接重新建
        showNotice("已保存设置");
    }
    needKeyframe = true;                      // 设置界面用掉了 canvas：要一整帧重新开始
    awaitKey = true;
    lastFrameAt = millis();
}

// ---------------------------------------------------------------- 性能统计
static void reportStats() {
    if (millis() - stAt < 3000) return;
    if (sending || uxQueueMessagesWaiting(reqQ)) return;   // 按键还没发完（网络慢）：这次不报，别排在按键前面
    float sec = (millis() - stAt) / 1000.0f;
    String body = String("{\"ver\":\"") + FW_VERSION + "\",\"fps\":" + String(stFrames / sec, 1) +
                  ",\"wait_ms\":" + (stFetchN ? stWaitMs / stFetchN : 0) +
                  ",\"recv_ms\":" + (stFetchN ? stRecvMs / stFetchN : 0) +
                  ",\"inflate_ms\":" + (stFetchN ? stInflateMs / stFetchN : 0) +
                  ",\"draw_ms\":" + (stFrames ? stApplyMs / stFrames : 0) +
                  ",\"zkb_per_frame\":" + (stFetchN ? stZBytes / stFetchN / 1024 : 0) +
                  ",\"kb_per_frame\":" + (stFrames ? stBytes / stFrames / 1024 : 0) +
                  ",\"full_frames\":" + stFull + ",\"rssi\":" + WiFi.RSSI() +
                  ",\"free_heap\":" + ESP.getFreeHeap() + "}";
    stAt = millis();
    stWaitMs = stRecvMs = stFetchN = stInflateMs = stZBytes = 0;
    stApplyMs = stFrames = stBytes = stFull = 0;
    postAsync("/api/device_stats", body);
}

// ---------------------------------------------------------------- 主流程
void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setBrightness(BRIGHTNESS);
    M5.Speaker.setVolume(140);
    Serial.begin(115200);

    for (auto &b : fbuf) b = (uint8_t *)ps_malloc(FRAME_MAX);  // Core2 有 8MB PSRAM
    zbuf = (uint8_t *)ps_malloc(FRAME_MAX);
    inflator = (tinfl_decompressor *)heap_caps_malloc(sizeof(tinfl_decompressor), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    canvas.setColorDepth(16);
    canvas.setPsram(true);
    canvas.createSprite(320, 240);
    canvas.setFont(&fonts::efontCN_14);        // 提示文字有中文
    M5.BtnB.setHoldThresh(400);               // 中键：点一下切换聊天内容，按住 0.4 秒看历史聊天记录
    M5.BtnC.setHoldThresh(2000);              // 右键：点一下换肤，按住 2 秒进设置

    settingsLoad();
    xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);   // 下载画面放在 core 0
    reqQ = xQueueCreate(8, sizeof(HubReq));
    xTaskCreatePinnedToCore(sendTask, "send", 6144, nullptr, 1, nullptr, 0);  // 发按键也在 core 0
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
            noticeDirty = true;               // 这一帧可能把提示盖掉了：重画
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
    // 小提示（口令错误、网络慢之类）直接叠在屏幕上：按了键马上就能看到，不用等下一帧画面（网络差时要等很久）
    if (!awaitKey && !lcdStale) {
        if (millis() < noticeUntil) {
            if (noticeDirty || !noticeOnScreen) drawNotice();
            noticeOnScreen = true;
        } else if (noticeOnScreen) {          // 提示到时间了：用 canvas 把那块恢复
            canvas.pushSprite(0, 0);
            noticeOnScreen = false;
        }
        noticeDirty = false;
    }
    pollFor(3);                              // 等下一帧的时候一直读输入
}
