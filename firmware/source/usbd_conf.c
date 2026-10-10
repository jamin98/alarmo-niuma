/* USB 设备库 <-> STM32H730 的 OTG_HS 外设（片内全速 PHY，PA11/PA12）。
 * 参数取自 ST 官方 NUCLEO-H723ZG 的 USB_Device 例程（同一芯片系列）。
 * 注意：Alarmo 的 USB 接线没有公开资料，这一层还没有在真机上验证过。 */
#include "usbd_conf.h"
#include "usbd_core.h"
#include "usbd_cdc.h"

PCD_HandleTypeDef hpcd;

/* 2ndloader 跳转前可能把 USB 的 48MHz 时钟关了，这里确认一下，不行就切到 HSI48 */
static void usb_clock_ensure(void)
{
    uint32_t sel = (RCC->D2CCIP2R & RCC_D2CCIP2R_USBSEL) >> RCC_D2CCIP2R_USBSEL_Pos;
    int ok = 0;
    if (sel == 1U) {            /* PLL1Q */
        ok = (RCC->CR & RCC_CR_PLL1RDY) && (RCC->PLLCFGR & RCC_PLLCFGR_DIVQ1EN);
    } else if (sel == 2U) {     /* PLL3Q */
        ok = (RCC->CR & RCC_CR_PLL3RDY) && (RCC->PLLCFGR & RCC_PLLCFGR_DIVQ3EN);
    } else if (sel == 3U) {     /* HSI48 */
        ok = (RCC->CR & RCC_CR_HSI48RDY) != 0;
    }
    if (!ok) {
        uint32_t t0 = HAL_GetTick();
        RCC->CR |= RCC_CR_HSI48ON;
        while (!(RCC->CR & RCC_CR_HSI48RDY) && HAL_GetTick() - t0 < 100U) {
        }
        MODIFY_REG(RCC->D2CCIP2R, RCC_D2CCIP2R_USBSEL, 3U << RCC_D2CCIP2R_USBSEL_Pos);
    }
}

void HAL_PCD_MspInit(PCD_HandleTypeDef *h)
{
    GPIO_InitTypeDef gpio = { 0 };
    (void)h;

    HAL_PWREx_EnableUSBVoltageDetector();
    usb_clock_ensure();

    __HAL_RCC_GPIOA_CLK_ENABLE();
    gpio.Pin = GPIO_PIN_11 | GPIO_PIN_12;
    gpio.Mode = GPIO_MODE_AF_PP;
    gpio.Pull = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.Alternate = GPIO_AF10_OTG1_FS;
    HAL_GPIO_Init(GPIOA, &gpio);

    __HAL_RCC_USB1_OTG_HS_CLK_ENABLE();
    /* 用片内 PHY 时必须保证 ULPI 时钟在睡眠模式下不被当成必需 */
    __HAL_RCC_USB1_OTG_HS_ULPI_CLK_SLEEP_DISABLE();

    HAL_NVIC_SetPriority(OTG_HS_IRQn, 6, 0);
    HAL_NVIC_EnableIRQ(OTG_HS_IRQn);
}

void OTG_HS_IRQHandler(void)
{
    HAL_PCD_IRQHandler(&hpcd);
}

/* ---- PCD -> 设备库 ---- */
void HAL_PCD_SetupStageCallback(PCD_HandleTypeDef *h) { USBD_LL_SetupStage(h->pData, (uint8_t *)h->Setup); }
void HAL_PCD_DataOutStageCallback(PCD_HandleTypeDef *h, uint8_t ep) { USBD_LL_DataOutStage(h->pData, ep, h->OUT_ep[ep].xfer_buff); }
void HAL_PCD_DataInStageCallback(PCD_HandleTypeDef *h, uint8_t ep) { USBD_LL_DataInStage(h->pData, ep, h->IN_ep[ep].xfer_buff); }
void HAL_PCD_SOFCallback(PCD_HandleTypeDef *h) { USBD_LL_SOF(h->pData); }
void HAL_PCD_ResetCallback(PCD_HandleTypeDef *h)
{
    USBD_LL_SetSpeed(h->pData, USBD_SPEED_FULL);
    USBD_LL_Reset(h->pData);
}
void HAL_PCD_SuspendCallback(PCD_HandleTypeDef *h) { USBD_LL_Suspend(h->pData); }
void HAL_PCD_ResumeCallback(PCD_HandleTypeDef *h) { USBD_LL_Resume(h->pData); }
void HAL_PCD_ISOOUTIncompleteCallback(PCD_HandleTypeDef *h, uint8_t ep) { USBD_LL_IsoOUTIncomplete(h->pData, ep); }
void HAL_PCD_ISOINIncompleteCallback(PCD_HandleTypeDef *h, uint8_t ep) { USBD_LL_IsoINIncomplete(h->pData, ep); }
void HAL_PCD_ConnectCallback(PCD_HandleTypeDef *h) { USBD_LL_DevConnected(h->pData); }
void HAL_PCD_DisconnectCallback(PCD_HandleTypeDef *h) { USBD_LL_DevDisconnected(h->pData); }

/* ---- 设备库 -> PCD ---- */
static USBD_StatusTypeDef st(HAL_StatusTypeDef s)
{
    switch (s) {
    case HAL_OK: return USBD_OK;
    case HAL_BUSY: return USBD_BUSY;
    default: return USBD_FAIL;
    }
}

USBD_StatusTypeDef USBD_LL_Init(USBD_HandleTypeDef *pdev)
{
    hpcd.Instance = USB1_OTG_HS;
    hpcd.Init.dev_endpoints = 8;
    hpcd.Init.use_dedicated_ep1 = DISABLE;
    hpcd.Init.dma_enable = DISABLE;
    hpcd.Init.speed = USB_OTG_SPEED_FULL;
    hpcd.Init.phy_itface = PCD_PHY_EMBEDDED;
    hpcd.Init.Sof_enable = DISABLE;
    hpcd.Init.low_power_enable = DISABLE;
    hpcd.Init.lpm_enable = DISABLE;
    hpcd.Init.battery_charging_enable = DISABLE;
    hpcd.Init.vbus_sensing_enable = DISABLE;
    hpcd.Init.use_external_vbus = DISABLE;
    hpcd.pData = pdev;
    pdev->pData = &hpcd;
    if (HAL_PCD_Init(&hpcd) != HAL_OK) {
        return USBD_FAIL;
    }
    /* FIFO 以 32 位字为单位：接收 0x200，EP0 发送 0x40，EP1(数据) 0x100，EP2(通知) 0x20 */
    HAL_PCDEx_SetRxFiFo(&hpcd, 0x200);
    HAL_PCDEx_SetTxFiFo(&hpcd, 0, 0x40);
    HAL_PCDEx_SetTxFiFo(&hpcd, 1, 0x100);
    HAL_PCDEx_SetTxFiFo(&hpcd, 2, 0x20);
    return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_DeInit(USBD_HandleTypeDef *pdev) { return st(HAL_PCD_DeInit(pdev->pData)); }
USBD_StatusTypeDef USBD_LL_Start(USBD_HandleTypeDef *pdev) { return st(HAL_PCD_Start(pdev->pData)); }
USBD_StatusTypeDef USBD_LL_Stop(USBD_HandleTypeDef *pdev) { return st(HAL_PCD_Stop(pdev->pData)); }
USBD_StatusTypeDef USBD_LL_OpenEP(USBD_HandleTypeDef *pdev, uint8_t ep, uint8_t type, uint16_t mps) { return st(HAL_PCD_EP_Open(pdev->pData, ep, mps, type)); }
USBD_StatusTypeDef USBD_LL_CloseEP(USBD_HandleTypeDef *pdev, uint8_t ep) { return st(HAL_PCD_EP_Close(pdev->pData, ep)); }
USBD_StatusTypeDef USBD_LL_FlushEP(USBD_HandleTypeDef *pdev, uint8_t ep) { return st(HAL_PCD_EP_Flush(pdev->pData, ep)); }
USBD_StatusTypeDef USBD_LL_StallEP(USBD_HandleTypeDef *pdev, uint8_t ep) { return st(HAL_PCD_EP_SetStall(pdev->pData, ep)); }
USBD_StatusTypeDef USBD_LL_ClearStallEP(USBD_HandleTypeDef *pdev, uint8_t ep) { return st(HAL_PCD_EP_ClrStall(pdev->pData, ep)); }
uint8_t USBD_LL_IsStallEP(USBD_HandleTypeDef *pdev, uint8_t ep)
{
    PCD_HandleTypeDef *h = pdev->pData;
    return (ep & 0x80U) ? h->IN_ep[ep & 0x7FU].is_stall : h->OUT_ep[ep & 0x7FU].is_stall;
}
USBD_StatusTypeDef USBD_LL_SetUSBAddress(USBD_HandleTypeDef *pdev, uint8_t addr) { return st(HAL_PCD_SetAddress(pdev->pData, addr)); }
USBD_StatusTypeDef USBD_LL_Transmit(USBD_HandleTypeDef *pdev, uint8_t ep, uint8_t *buf, uint32_t size) { return st(HAL_PCD_EP_Transmit(pdev->pData, ep, buf, size)); }
USBD_StatusTypeDef USBD_LL_PrepareReceive(USBD_HandleTypeDef *pdev, uint8_t ep, uint8_t *buf, uint32_t size) { return st(HAL_PCD_EP_Receive(pdev->pData, ep, buf, size)); }
uint32_t USBD_LL_GetRxDataSize(USBD_HandleTypeDef *pdev, uint8_t ep) { return HAL_PCD_EP_GetRxCount(pdev->pData, ep); }
void USBD_LL_Delay(uint32_t ms) { HAL_Delay(ms); }

void *USBD_static_malloc(uint32_t size)
{
    static uint32_t mem[(sizeof(USBD_CDC_HandleTypeDef) / 4U) + 1U];
    (void)size;
    return mem;
}

void USBD_static_free(void *p) { (void)p; }
