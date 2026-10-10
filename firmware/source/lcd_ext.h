#pragma once
#include <stdint.h>
#include "libalarmo/lcd.h"

/* r/c 是面板原生坐标：row = 319 - 屏幕x，col = 屏幕y */
void LCD_BeginWindow(uint16_t r0, uint16_t r1, uint16_t c0, uint16_t c1);
/* 像素流，每像素 B,G,R 三字节；len 必须是偶数 */
void LCD_WritePixels(const uint8_t *bgr, uint32_t len);
void LCD_EndWindow(void);
