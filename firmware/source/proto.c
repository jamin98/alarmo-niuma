#include "proto.h"
#include <string.h>

#define HDR_LEN 16
#define OUT_CAP 6144u            /* 6 的倍数：永远在像素对的边界上刷出 */
#define TIMEOUT_MS 1000u

enum { ST_HEADER, ST_RAW, ST_RLE, ST_SKIP };

static const proto_ops_t *g_ops;
static uint8_t g_hdr[HDR_LEN];
static uint32_t g_hdr_n;
static int g_state;
static uint32_t g_payload_left;   /* 还没读的 payload 字节 */
static uint32_t g_px_left;        /* 这个矩形还差多少像素 */
static uint8_t g_rle[4];
static uint32_t g_rle_n;
static uint8_t g_out[OUT_CAP];
static uint32_t g_out_n;
static uint32_t g_errors;
static uint32_t g_last_ms, g_now_ms;

static void flush(void)
{
    if (g_out_n) {
        g_ops->write(g_out, g_out_n);
        g_out_n = 0;
    }
}

static void finish_rect(void)
{
    /* payload 结束了像素却没给够：补黑，保证窗口被完整写满 */
    if (g_px_left) {
        g_errors++;
        while (g_px_left) {
            g_out[g_out_n++] = 0; g_out[g_out_n++] = 0; g_out[g_out_n++] = 0;
            g_px_left--;
            if (g_out_n == OUT_CAP) flush();
        }
    }
    flush();
    g_ops->end();
}

static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }

static void on_header(void)
{
    uint8_t cmd = g_hdr[2], flags = g_hdr[3];
    uint16_t r0 = rd16(g_hdr + 4), r1 = rd16(g_hdr + 6), c0 = rd16(g_hdr + 8), c1 = rd16(g_hdr + 10);
    uint32_t len = rd32(g_hdr + 12);

    g_state = ST_HEADER;
    if (cmd == PROTO_CMD_BRIGHT && len == 0) {
        g_ops->brightness(r0 > 1000 ? 1000 : r0);
        return;
    }
    if (cmd == PROTO_CMD_HELLO && len == 0) {
        g_ops->hello();
        return;
    }
    if (cmd == PROTO_CMD_RECT && r0 <= r1 && r1 < PROTO_ROWS && c0 <= c1 && c1 < PROTO_COLS) {
        uint32_t px = (uint32_t)(r1 - r0 + 1) * (uint32_t)(c1 - c0 + 1);
        int rle = flags & 1;
        if ((px & 1u) == 0 && len > 0 && (rle ? (len % 4u == 0 && len <= px * 4u) : (len == px * 3u))) {
            g_px_left = px;
            g_payload_left = len;
            g_rle_n = 0;
            g_out_n = 0;
            g_state = rle ? ST_RLE : ST_RAW;
            g_ops->begin(r0, r1, c0, c1);
            return;
        }
    }
    /* 头不合法。长度可信就跳过 payload，否则直接重新找包头 */
    g_errors++;
    if (len > 0 && len <= PROTO_ROWS * PROTO_COLS * 4u) {
        g_payload_left = len;
        g_state = ST_SKIP;
    }
}

void proto_init(const proto_ops_t *ops)
{
    g_ops = ops;
    g_state = ST_HEADER;
    g_hdr_n = 0;
    g_out_n = 0;
    g_errors = 0;
}

void proto_feed(const uint8_t *data, uint32_t len)
{
    g_last_ms = g_now_ms;
    while (len) {
        switch (g_state) {
        case ST_HEADER: {
            uint8_t b = *data++;
            len--;
            if (g_hdr_n == 0 && b != 0xA5) break;
            if (g_hdr_n == 1 && b != 0x5A) { g_hdr_n = (b == 0xA5) ? 1 : 0; break; }
            g_hdr[g_hdr_n++] = b;
            if (g_hdr_n == HDR_LEN) {
                g_hdr_n = 0;
                on_header();
            }
            break;
        }
        case ST_RAW: {
            uint32_t n = len < g_payload_left ? len : g_payload_left;
            uint32_t room = OUT_CAP - g_out_n;
            if (n > room) n = room;
            memcpy(g_out + g_out_n, data, n);
            g_out_n += n; data += n; len -= n; g_payload_left -= n;
            if (g_out_n == OUT_CAP) flush();
            if (g_payload_left == 0) {
                g_px_left = 0;
                finish_rect();
                g_state = ST_HEADER;
            }
            break;
        }
        case ST_RLE: {
            g_rle[g_rle_n++] = *data++;
            len--; g_payload_left--;
            if (g_rle_n == 4) {
                uint32_t run = (uint32_t)g_rle[0] + 1u;
                g_rle_n = 0;
                if (run > g_px_left) { run = g_px_left; g_errors++; }
                g_px_left -= run;
                while (run--) {
                    g_out[g_out_n++] = g_rle[1]; g_out[g_out_n++] = g_rle[2]; g_out[g_out_n++] = g_rle[3];
                    if (g_out_n == OUT_CAP) flush();
                }
            }
            if (g_payload_left == 0) {
                finish_rect();
                g_state = ST_HEADER;
            }
            break;
        }
        default: { /* ST_SKIP */
            uint32_t n = len < g_payload_left ? len : g_payload_left;
            data += n; len -= n; g_payload_left -= n;
            if (g_payload_left == 0) g_state = ST_HEADER;
            break;
        }
        }
    }
}

void proto_tick(uint32_t now_ms)
{
    g_now_ms = now_ms;
    if ((g_state != ST_HEADER || g_hdr_n) && now_ms - g_last_ms > TIMEOUT_MS) {
        if (g_state == ST_RAW || g_state == ST_RLE) finish_rect();
        g_state = ST_HEADER;
        g_hdr_n = 0;
        g_errors++;
    }
}

uint32_t proto_error_count(void) { return g_errors; }
