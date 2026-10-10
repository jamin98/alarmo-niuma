/* 在 PC 上跑固件的协议解析器：用一块内存假装面板，读入 hub 生成的数据流，
 * 输出最终的面板内容，供 test_proto.py 和渲染结果逐像素比对。
 * 用法: host_test <chunk大小> < stream.bin > panel.bin */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../source/proto.h"

static uint8_t panel[PROTO_ROWS][PROTO_COLS][3];
static uint16_t wr0, wr1, wc0, wc1, cur_r, cur_c;
static uint8_t carry[3];
static int carry_n, in_window, bad;
static unsigned brightness = 9999, hellos;

static void t_begin(uint16_t r0, uint16_t r1, uint16_t c0, uint16_t c1)
{
    if (in_window) bad++;
    wr0 = r0; wr1 = r1; wc0 = c0; wc1 = c1; cur_r = r0; cur_c = c0; carry_n = 0; in_window = 1;
}
static void t_write(const uint8_t *p, uint32_t len)
{
    if (!in_window || (len & 1)) bad++;      /* 真机上 DMA 按 16 位写，长度必须是偶数 */
    for (uint32_t i = 0; i < len; i++) {
        carry[carry_n++] = p[i];
        if (carry_n == 3) {
            carry_n = 0;
            if (cur_r > wr1) { bad++; continue; }
            memcpy(panel[cur_r][cur_c], carry, 3);
            if (++cur_c > wc1) { cur_c = wc0; cur_r++; }
        }
    }
}
static void t_end(void)
{
    if (!in_window || carry_n || cur_r != wr1 + 1) bad++;   /* 窗口必须恰好写满 */
    in_window = 0;
}
static void t_bright(uint16_t v) { brightness = v; }
static void t_hello(void) { hellos++; }

int main(int argc, char **argv)
{
    static const proto_ops_t ops = { t_begin, t_write, t_end, t_bright, t_hello };
    size_t chunk = argc > 1 ? (size_t)atoi(argv[1]) : 64;
    uint8_t *buf = malloc(chunk);
    size_t n;
    uint32_t ms = 0;
    proto_init(&ops);
    while ((n = fread(buf, 1, chunk, stdin)) > 0) {
        proto_tick(ms++);
        proto_feed(buf, (uint32_t)n);
    }
    free(buf);
    fwrite(panel, 1, sizeof(panel), stdout);
    fprintf(stderr, "errors=%u bad=%d brightness=%u hellos=%u\n", proto_error_count(), bad, brightness, hellos);
    return 0;
}
