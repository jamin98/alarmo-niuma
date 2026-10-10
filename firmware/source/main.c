/* 牛马办公室 - Alarmo 端固件。
 * 只做两件事：把 USB 串口收到的矩形画到屏幕上；把按键和滚轮状态报回去。
 * 所有画面都由 Debian 上的 hub 渲染。 */
#include "main.h"
#include "board.h"
#include "lcd_ext.h"
#include "libalarmo/input.h"
#include "proto.h"
#include "usb_serial.h"

static volatile uint8_t want_report;
static float backlight = 0.8f;

static void op_begin(uint16_t r0, uint16_t r1, uint16_t c0, uint16_t c1) { LCD_BeginWindow(r0, r1, c0, c1); }
static void op_write(const uint8_t *p, uint32_t n) { LCD_WritePixels(p, n); }
static void op_end(void) { LCD_EndWindow(); }
static void op_bright(uint16_t v) { backlight = (float)v / 1000.0f; LCD_SetBrightness(backlight); }
static void op_hello(void) { want_report = 1; }

static const proto_ops_t ops = { op_begin, op_write, op_end, op_bright, op_hello };

/* 开机状态色块：没有字库，用颜色表示。hub 发来第一帧后会被盖掉 */
static void status_block(uint8_t r, uint8_t g, uint8_t b)
{
    LCD_DrawRect(140, 100, 40, 40, r, g, b);
}

static void send_report(uint8_t buttons, uint16_t dial, uint8_t seq)
{
    uint8_t p[8] = { 0xA5, 0x5A, 0x81, buttons, (uint8_t)dial, (uint8_t)(dial >> 8), seq, 0 };
    for (int i = 0; i < 7; i++) p[7] ^= p[i];
    USBSER_Write(p, sizeof(p));
}

int main(void)
{
    static uint8_t buf[4096];

    SCB_EnableICache();
    SCB_EnableDCache();
    __enable_irq();

    HAL_Init();

    /* 时钟沿用 2ndloader 的配置 */
    DMA_Init();
    FMC_Init();
    MDMA_Init();
    TIM3_Init();
    ADC_Init();
    INPUT_Init();
    LCD_Init();

    LCD_SetBrightness(backlight);
    LCD_DrawRect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, 18, 20, 28);
    status_block(250, 200, 60);           /* 黄：等主机 */
    LCD_DISPON();

    proto_init(&ops);
    if (USBSER_Init() != 0) {
        status_block(235, 64, 52);        /* 红：USB 初始化失败 */
    }

    uint32_t last_sample = 0, last_report = 0, last_rx = 0;
    uint8_t stable = 0, prev_raw = 0, sent_buttons = 0xFF, seq = 0;
    uint16_t sent_dial = 0xFFFF;
    int dimmed = 0, ever_rx = 0;

    while (1) {
        uint32_t now = HAL_GetTick();

        proto_tick(now);
        uint32_t n = USBSER_Read(buf, sizeof(buf));
        if (n) {
            proto_feed(buf, n);
            last_rx = now;
            ever_rx = 1;
            if (dimmed) {
                dimmed = 0;
                LCD_SetBrightness(backlight);
            }
        } else if (ever_rx && !dimmed && now - last_rx > 10000U) {
            /* hub 10 秒没动静：把背光调暗，提示画面已经不是实时的了 */
            dimmed = 1;
            LCD_SetBrightness(0.15f);
        }

        if (now - last_sample >= 10U) {
            last_sample = now;
            uint8_t raw = (uint8_t)INPUT_GetButtons();
            if (raw == prev_raw) stable = raw;      /* 连续两次一致才算数，去抖 */
            prev_raw = raw;

            float deg = INPUT_GetDial();
            if (deg < 0.0f) deg = 0.0f;
            uint16_t dial = (uint16_t)(deg * 10.0f);
            if (dial >= 3600U) dial = 3599U;
            int moved = (int)dial - (int)sent_dial;
            if (moved < 0) moved = -moved;
            if (moved > 1800) moved = 3600 - moved;

            if (stable != sent_buttons || moved >= 10 || want_report || now - last_report >= 250U) {
                send_report(stable, dial, seq++);
                sent_buttons = stable;
                sent_dial = dial;
                last_report = now;
                want_report = 0;
            }
        }
    }
}
