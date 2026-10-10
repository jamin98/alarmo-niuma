/* USB CDC 串口：中断里只把数据拷进环形缓冲，主循环慢慢处理。
 * 缓冲快满时不再向主机要数据（NAK），这样画屏慢也不会丢字节。 */
#include "usb_serial.h"
#include "usbd_core.h"
#include "usbd_cdc.h"
#include "usbd_desc.h"

#define RING_SIZE 65536u
#define PKT_MAX CDC_DATA_HS_MAX_PACKET_SIZE

static USBD_HandleTypeDef usbd;
static uint8_t ring[RING_SIZE];
static volatile uint32_t r_head, r_tail;      /* head: 中断写；tail: 主循环读 */
static volatile uint8_t rx_paused;
static uint8_t rx_pkt[PKT_MAX] __attribute__((aligned(4)));
static uint8_t tx_pkt[64] __attribute__((aligned(4)));
static USBD_CDC_LineCodingTypeDef line = { 115200, 0, 0, 8 };

static uint32_t ring_free(void) { return RING_SIZE - 1u - ((r_head - r_tail) & (RING_SIZE - 1u)); }

static int8_t itf_init(void)
{
    USBD_CDC_SetRxBuffer(&usbd, rx_pkt);
    rx_paused = 0;
    return USBD_OK;
}

static int8_t itf_deinit(void) { return USBD_OK; }

static int8_t itf_control(uint8_t cmd, uint8_t *buf, uint16_t len)
{
    (void)len;
    if (cmd == CDC_SET_LINE_CODING) {
        line.bitrate = (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) | ((uint32_t)buf[2] << 16) | ((uint32_t)buf[3] << 24);
        line.format = buf[4]; line.paritytype = buf[5]; line.datatype = buf[6];
    } else if (cmd == CDC_GET_LINE_CODING) {
        buf[0] = (uint8_t)line.bitrate; buf[1] = (uint8_t)(line.bitrate >> 8);
        buf[2] = (uint8_t)(line.bitrate >> 16); buf[3] = (uint8_t)(line.bitrate >> 24);
        buf[4] = line.format; buf[5] = line.paritytype; buf[6] = line.datatype;
    }
    return USBD_OK;
}

static int8_t itf_receive(uint8_t *buf, uint32_t *len)
{
    uint32_t n = *len, h = r_head;
    for (uint32_t i = 0; i < n; i++) {
        ring[h] = buf[i];
        h = (h + 1u) & (RING_SIZE - 1u);
    }
    r_head = h;
    if (ring_free() >= PKT_MAX) {
        USBD_CDC_ReceivePacket(&usbd);
    } else {
        rx_paused = 1;      /* 主循环腾出空间后再继续收 */
    }
    return USBD_OK;
}

static int8_t itf_txdone(uint8_t *buf, uint32_t *len, uint8_t ep) { (void)buf; (void)len; (void)ep; return USBD_OK; }

static USBD_CDC_ItfTypeDef itf = { itf_init, itf_deinit, itf_control, itf_receive, itf_txdone };

int USBSER_Init(void)
{
    /* 2ndloader 留下的是 U 盘身份，先断开，让主机看到设备被拔掉 */
    __HAL_RCC_USB1_OTG_HS_CLK_ENABLE();
    ((USB_OTG_DeviceTypeDef *)((uint32_t)USB1_OTG_HS + USB_OTG_DEVICE_BASE))->DCTL |= USB_OTG_DCTL_SDIS;
    HAL_Delay(300);

    if (USBD_Init(&usbd, &niuma_desc, 0) != USBD_OK) return -1;
    if (USBD_RegisterClass(&usbd, USBD_CDC_CLASS) != USBD_OK) return -2;
    if (USBD_CDC_RegisterInterface(&usbd, &itf) != USBD_OK) return -3;
    if (USBD_Start(&usbd) != USBD_OK) return -4;
    return 0;
}

int USBSER_Configured(void) { return usbd.dev_state == USBD_STATE_CONFIGURED; }

uint32_t USBSER_Read(uint8_t *dst, uint32_t max)
{
    uint32_t n = 0, t = r_tail, h = r_head;
    while (t != h && n < max) {
        dst[n++] = ring[t];
        t = (t + 1u) & (RING_SIZE - 1u);
    }
    r_tail = t;
    if (rx_paused && ring_free() >= PKT_MAX) {
        __disable_irq();
        if (rx_paused) {
            rx_paused = 0;
            USBD_CDC_ReceivePacket(&usbd);
        }
        __enable_irq();
    }
    return n;
}

int USBSER_Write(const uint8_t *data, uint32_t len)
{
    USBD_CDC_HandleTypeDef *h = (USBD_CDC_HandleTypeDef *)usbd.pClassData;
    int ok = 0;
    if (!USBSER_Configured() || h == NULL || len > sizeof(tx_pkt)) return 0;
    __disable_irq();
    if (h->TxState == 0U) {
        memcpy(tx_pkt, data, len);
        USBD_CDC_SetTxBuffer(&usbd, tx_pkt, len);
        ok = USBD_CDC_TransmitPacket(&usbd) == USBD_OK;
    }
    __enable_irq();
    return ok;
}
