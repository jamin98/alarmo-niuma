/* 在官方 libalarmo/lcd.c 的基础上加一个"开窗后连续写像素"的接口。
 * 直接把官方源文件包含进来，这样能用到里面的 static 函数，又不用改官方代码。 */
#include "libalarmo/lcd.c"
#include "lcd_ext.h"

void LCD_BeginWindow(uint16_t r0, uint16_t r1, uint16_t c0, uint16_t c1)
{
    LCD_RASET(r0, r1);
    LCD_CASET(c0, c1);
    LCD_Select();
    LCD_WriteCommand(0x2c);
}

void LCD_WritePixels(const uint8_t *bgr, uint32_t len)
{
    LCD_DrawData(bgr, len);
}

void LCD_EndWindow(void)
{
    LCD_Deselect();
}
