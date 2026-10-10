// 牛马办公室 - M5Stack Core2 屏幕端
//
// Core2 的屏幕正好是 320x240，和 hub 渲染的画面一样大。这里不做任何绘制，只做三件事：
//   1. 连 WiFi，从 hub 轮询 /frame.jpg 并显示；
//   2. 把按键、滑动、摇一摇变成 hub 的 /api/button 请求；
//   3. 用振动马达和扬声器给「抽鞭子」加点反馈。
//
// 操作：
//   屏幕下方三个触摸键   左 = 一键鼓励    中 = 抽鞭子（大按钮）    右 = 换肤
//   在屏幕上上下滑动      转滚轮：进入聊天记录并翻页（按「中」回到工位）
//   点一下屏幕上的某只牛马  点名：老板跑过去盯着它，并私聊它一句（hub 的 [buttons] poke_text）
//   按住某只牛马 / 按住「我」 说话私聊它 / 发到群里：用 Core2 自带的麦克风录，松手把录音传给 hub，
//                            由 Grok Bot 听写成文字发出去（跑 Grok Bot 的电脑上要装虚拟麦克风，见 README）
//   摇一摇设备            抽鞭子
#include <M5Unified.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include "config.h"

static constexpr size_t JPG_MAX = 120 * 1024;

static M5Canvas canvas(&M5.Display);          // 先画到内存里再整屏推出去，避免撕裂
static uint8_t *jpgBuf = nullptr;
static WiFiClient frameClient;
static HTTPClient frameHttp;                  // 一直复用同一个连接，省掉每帧握手
static int failCount = 0;
static uint32_t noticeUntil = 0;
static String notice;
static uint32_t vibeOffAt = 0;
static uint32_t lastShake = 0;

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

static void drawStatus(const String &line1, const String &line2 = "") {
    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setTextSize(2);
    centerText(line1, 100);
    M5.Display.setTextSize(1);
    centerText(line2, 135, TFT_LIGHTGREY);
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
static bool connectWifi(uint32_t timeoutMs = 15000) {
    if (WiFi.status() == WL_CONNECTED) return true;
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);                     // 关省电，帧率稳很多
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t t0 = millis();
    int dots = 0;
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) {
        drawStatus("Connecting WiFi" + String("....").substring(0, dots++ % 4), WIFI_SSID);
        delay(400);
    }
    return WiFi.status() == WL_CONNECTED;
}

static bool fetchFrame() {
    frameHttp.setReuse(true);
    frameHttp.setConnectTimeout(1500);
    frameHttp.setTimeout(2500);
    String url = String(HUB_URL) + "/frame.jpg?q=" + String(JPEG_QUALITY);
    if (!frameHttp.begin(frameClient, url)) return false;
    int code = frameHttp.GET();
    if (code != 200) {
        frameHttp.end();
        return false;
    }
    int len = frameHttp.getSize();
    if (len <= 0 || (size_t)len > JPG_MAX) {
        frameHttp.end();
        return false;
    }
    WiFiClient *s = frameHttp.getStreamPtr();
    int got = 0;
    uint32_t t0 = millis();
    while (got < len && millis() - t0 < 2500) {
        int n = s->available();
        if (n > 0) {
            got += s->readBytes(jpgBuf + got, min(n, len - got));
        } else if (!s->connected()) {
            break;
        } else {
            delay(1);
        }
    }
    frameHttp.end();
    if (got != len) return false;
    canvas.drawJpg(jpgBuf, len, 0, 0);
    return true;
}

// 往 hub 的某个接口 POST 一段 JSON。reply 不为空时带回响应内容。
static bool postHub(const char *path, const String &body, String *reply = nullptr) {
    HTTPClient http;
    http.setConnectTimeout(1500);
    http.setTimeout(2500);
    if (!http.begin(String(HUB_URL) + path)) return false;
    http.addHeader("Content-Type", "application/json");
    if (strlen(HUB_TOKEN)) http.addHeader("X-Token", HUB_TOKEN);
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
    memcpy(buf, "RIFF", 4); put32(buf + 4, 36 + n * 2); memcpy(buf + 8, "WAVEfmt ", 8);
    put32(buf + 16, 16); put16(buf + 20, 1); put16(buf + 22, 1); put32(buf + 24, MIC_RATE);
    put32(buf + 28, MIC_RATE * 2); put16(buf + 32, 2); put16(buf + 34, 16); memcpy(buf + 36, "data", 4); put32(buf + 40, n * 2);
    HTTPClient http;
    http.setConnectTimeout(1500);
    http.setTimeout(15000);
    if (http.begin(String(HUB_URL) + "/api/voice/clip")) {
        http.addHeader("Content-Type", "audio/wav");
        if (strlen(HUB_TOKEN)) http.addHeader("X-Token", HUB_TOKEN);
        int code = http.POST(buf, 44 + n * 2);
        http.end();
        showNotice(code == 200 ? "Sent to Grok" : "Upload error " + String(code));
    }
    free(buf);
}

// 往 hub 发按键。button = "dial" | "back" | "notify"；dial != 0 时发滚轮转动（度）。
static bool sendButton(const char *button, int dial = 0) {
    String body = dial ? String("{\"dial\":") + dial + "}" : String("{\"button\":\"") + button + "\"}";
    return postHub("/api/button", body);
}

// ---------------------------------------------------------------- 输入
static void handleInput() {
    M5.update();

    if (M5.BtnA.wasClicked()) {               // 左：一键鼓励
        click();
        sendButton("back");
    }
    if (M5.BtnB.wasClicked()) {               // 中：抽鞭子
        whipFeedback();
        sendButton("dial");
    }
    if (M5.BtnC.wasClicked()) {               // 右：换肤
        click();
        sendButton("notify");
    }

    // 屏幕上滑动 = 转滚轮。往下滑看更早的消息，往上滑回到最新
    auto t = M5.Touch.getDetail();
    if (t.wasHold() && t.y < M5.Display.height()) {        // 按住：录音（hub 按位置决定私聊谁 / 发群里），松手发送
        click();
        String reply;
        if (postHub("/api/voice", String("{\"action\":\"start\",\"x\":") + t.x + ",\"y\":" + t.y + "}", &reply)
            && reply.indexOf("\"ok\": true") >= 0) recordVoice();      // 没按中牛马或「我」时 ok 是 false
        return;
    }
    if (t.wasClicked() && t.y < M5.Display.height()) {     // 点一下：hub 按当前主题找点中的牛马，点名盯它
        click();
        postHub("/api/poke", String("{\"x\":") + t.x + ",\"y\":" + t.y + "}");
    }
    if (t.wasFlicked() && t.y < M5.Display.height()) {
        int dy = t.distanceY();
        if (abs(dy) > 25 && abs(dy) > abs(t.distanceX())) {
            click();
            sendButton("", dy > 0 ? 30 : -30);
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

// ---------------------------------------------------------------- 主流程
void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setBrightness(BRIGHTNESS);
    M5.Speaker.setVolume(140);
    Serial.begin(115200);

    jpgBuf = (uint8_t *)ps_malloc(JPG_MAX);   // Core2 有 8MB PSRAM
    if (!jpgBuf) jpgBuf = (uint8_t *)malloc(JPG_MAX);
    canvas.setColorDepth(16);
    canvas.setPsram(true);
    canvas.createSprite(320, 240);

    drawStatus("Niuma Office", "starting...");
    if (!connectWifi()) drawStatus("WiFi failed", String("check ") + WIFI_SSID);
}

void loop() {
    uint32_t t0 = millis();
    handleInput();

    if (WiFi.status() != WL_CONNECTED) {
        if (!connectWifi(8000)) {
            drawStatus("WiFi lost", "retrying...");
            delay(1000);
        }
        return;
    }

    if (fetchFrame()) {
        failCount = 0;
        if (millis() < noticeUntil) {         // 叠一条小提示（口令错误之类）
            canvas.fillRoundRect(60, 205, 200, 24, 6, TFT_BLACK);
            canvas.setTextDatum(textdatum_t::middle_center);
            canvas.setTextColor(TFT_YELLOW, TFT_BLACK);
            canvas.drawString(notice, 160, 217);
        }
        canvas.pushSprite(0, 0);
    } else if (++failCount >= 3) {
        drawStatus("Hub offline", String(HUB_URL));
        delay(1000);
    }

    uint32_t used = millis() - t0;
    if (used < FRAME_MS) delay(FRAME_MS - used);
}
