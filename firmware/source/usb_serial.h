#pragma once
#include <stdint.h>

/* 初始化并以 CDC 串口身份重新枚举。返回 0 表示成功 */
int USBSER_Init(void);
/* 取出已收到的数据，返回字节数 */
uint32_t USBSER_Read(uint8_t *dst, uint32_t max);
/* 发送一小包（<=64 字节）。上一包还没发完时返回 0 */
int USBSER_Write(const uint8_t *data, uint32_t len);
int USBSER_Configured(void);
