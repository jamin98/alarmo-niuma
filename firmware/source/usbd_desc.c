/* USB 描述符。VID:PID 用的是 pid.codes 的测试 ID 1209:0001，仅供自用。
 * Linux 下设备节点: /dev/serial/by-id/usb-NiumaOffice_Alarmo_0001-if00 */
#include "usbd_core.h"
#include "usbd_desc.h"
#include "usbd_conf.h"

#define USBD_VID 0x1209
#define USBD_PID 0x0001

static uint8_t dev_desc[USB_LEN_DEV_DESC] = {
    0x12, USB_DESC_TYPE_DEVICE, 0x00, 0x02,
    0x02, 0x02, 0x00,                 /* CDC */
    USB_MAX_EP0_SIZE,
    LOBYTE(USBD_VID), HIBYTE(USBD_VID), LOBYTE(USBD_PID), HIBYTE(USBD_PID),
    0x00, 0x01,
    USBD_IDX_MFC_STR, USBD_IDX_PRODUCT_STR, USBD_IDX_SERIAL_STR,
    USBD_MAX_NUM_CONFIGURATION
};

static uint8_t lang_desc[USB_LEN_LANGID_STR_DESC] = { USB_LEN_LANGID_STR_DESC, USB_DESC_TYPE_STRING, 0x09, 0x04 };
static uint8_t str_buf[USBD_MAX_STR_DESC_SIZ];

static uint8_t *d_device(USBD_SpeedTypeDef s, uint16_t *len) { (void)s; *len = sizeof(dev_desc); return dev_desc; }
static uint8_t *d_lang(USBD_SpeedTypeDef s, uint16_t *len) { (void)s; *len = sizeof(lang_desc); return lang_desc; }
static uint8_t *d_str(const char *text, uint16_t *len) { USBD_GetString((uint8_t *)text, str_buf, len); return str_buf; }
static uint8_t *d_mfc(USBD_SpeedTypeDef s, uint16_t *len) { (void)s; return d_str("NiumaOffice", len); }
static uint8_t *d_product(USBD_SpeedTypeDef s, uint16_t *len) { (void)s; return d_str("Alarmo", len); }
static uint8_t *d_serial(USBD_SpeedTypeDef s, uint16_t *len) { (void)s; return d_str("0001", len); }
static uint8_t *d_config(USBD_SpeedTypeDef s, uint16_t *len) { (void)s; return d_str("CDC Config", len); }
static uint8_t *d_iface(USBD_SpeedTypeDef s, uint16_t *len) { (void)s; return d_str("CDC Interface", len); }

USBD_DescriptorsTypeDef niuma_desc = { d_device, d_lang, d_mfc, d_product, d_serial, d_config, d_iface };
