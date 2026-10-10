# 牛马办公室 Alarmo 固件。依赖官方 payload 仓库（build.sh 会帮你拉好）:
#   ALARMO = GaryOderNichts/alarmo 的 usb_payload 目录
ALARMO ?= $(CURDIR)/upstream/alarmo/usb_payload
CUBE   := $(ALARMO)/STM32CubeH7
HAL    := $(CUBE)/Drivers/STM32H7xx_HAL_Driver
USBLIB := $(CUBE)/Middlewares/ST/STM32_USB_Device_Library
PREFIX ?= arm-none-eabi-
BUILD  := build

HAL_SRC := adc_ex adc cortex dma_ex dma gpio mdma pwr_ex pwr rcc_ex rcc sram tim_ex tim pcd pcd_ex
SRCS := $(wildcard source/*.c) \
        $(ALARMO)/source/libalarmo/input.c \
        $(ALARMO)/source/stm32h7xx_hal_msp.c \
        $(ALARMO)/source/stm32h7xx_it.c \
        $(ALARMO)/source/system_stm32h7xx.c \
        $(ALARMO)/source/syscalls.c \
        $(ALARMO)/source/string.c \
        $(HAL)/Src/stm32h7xx_hal.c \
        $(foreach m,$(HAL_SRC),$(HAL)/Src/stm32h7xx_hal_$(m).c) \
        $(HAL)/Src/stm32h7xx_ll_fmc.c $(HAL)/Src/stm32h7xx_ll_usb.c \
        $(USBLIB)/Core/Src/usbd_core.c $(USBLIB)/Core/Src/usbd_ctlreq.c $(USBLIB)/Core/Src/usbd_ioreq.c \
        $(USBLIB)/Class/CDC/Src/usbd_cdc.c
ASRC := $(ALARMO)/source/startup_stm32h730xx.s

INC := -Isource -I$(ALARMO)/source \
       -I$(CUBE)/Drivers/CMSIS/Include -I$(CUBE)/Drivers/CMSIS/Device/ST/STM32H7xx/Include \
       -I$(HAL)/Inc -I$(USBLIB)/Core/Inc -I$(USBLIB)/Class/CDC/Inc

MACH   := -mthumb -march=armv7e-m -mtune=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard
OPT    := -O2
CFLAGS  = $(MACH) -DSTM32H730xx -DHAL_PCD_MODULE_ENABLED $(OPT) -ffunction-sections -fdata-sections $(INC)
LDFLAGS := $(MACH) -specs=nano.specs -Wl,-T,$(ALARMO)/link.ld -Wl,--gc-sections \
           -Wl,--no-warn-rwx-segments -Wl,-Map,$(BUILD)/niuma.map

OBJS := $(addprefix $(BUILD)/,$(notdir $(SRCS:.c=.o))) $(BUILD)/startup.o
vpath %.c source $(filter-out source/,$(sort $(dir $(SRCS))))

all: a.bin

# 官方的 LCD/输入/外设初始化代码一直是用 -O0 编译并验证的，保持一致，避免时序变化
$(BUILD)/lcd_ext.o $(BUILD)/board.o $(BUILD)/input.o $(BUILD)/stm32h7xx_hal_msp.o: OPT := -O0

$(BUILD):
	@mkdir -p $@

$(BUILD)/%.o: %.c | $(BUILD)
	@echo CC $(notdir $<)
	@$(PREFIX)gcc $(CFLAGS) -c $< -o $@

$(BUILD)/startup.o: $(ASRC) | $(BUILD)
	@$(PREFIX)gcc $(MACH) -x assembler-with-cpp -c $< -o $@

$(BUILD)/niuma.elf: $(OBJS)
	@echo LD $(notdir $@)
	@$(PREFIX)gcc $(LDFLAGS) $(OBJS) -lc -lm -lnosys -o $@
	@$(PREFIX)size $@

$(BUILD)/niuma.bin: $(BUILD)/niuma.elf
	@$(PREFIX)objcopy -O binary $< $@

# 加密成 2ndloader 认的格式。需要先在 $(ALARMO)/key.py 里填好 AES key 和 IV
a.bin: $(BUILD)/niuma.bin
	@echo ENCRYPT a.bin
	@cd $(ALARMO) && python3 ciphtool.py encrypt $(abspath $<) $(abspath $@)

elf: $(BUILD)/niuma.bin

clean:
	rm -rf $(BUILD) a.bin

.PHONY: all elf clean
