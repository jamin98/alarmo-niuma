/* 主机 <-> Alarmo 的帧协议解析器。不依赖任何硬件，可以在 PC 上编译测试。
 * 协议格式见 hub/device.py 顶部注释。 */
#pragma once
#include <stdint.h>

#define PROTO_CMD_RECT   1
#define PROTO_CMD_BRIGHT 2
#define PROTO_CMD_HELLO  3

#define PROTO_ROWS 320u   /* 面板原生行数 = 屏幕宽 */
#define PROTO_COLS 240u   /* 面板原生列数 = 屏幕高 */

typedef struct {
    void (*begin)(uint16_t r0, uint16_t r1, uint16_t c0, uint16_t c1);
    void (*write)(const uint8_t *bgr, uint32_t len);   /* len 总是偶数 */
    void (*end)(void);
    void (*brightness)(uint16_t permille);
    void (*hello)(void);
} proto_ops_t;

void proto_init(const proto_ops_t *ops);
void proto_feed(const uint8_t *data, uint32_t len);
/* 包收到一半主机就没声音了：超时后丢弃，回到找包头的状态 */
void proto_tick(uint32_t now_ms);
uint32_t proto_error_count(void);
