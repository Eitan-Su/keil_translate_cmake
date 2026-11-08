# Keil 工程转换验证清单

本文档记录经过实际转换和 GCC 构建验证的 Keil 工程。统计日期为 2026 年 11 月 9 日。
AT32F415 V2.1.8 SDK 首次全量测试通过 130 个工程、131 个目标。
随后补充 FreeRTOS GCC 移植层选择和 SYMDEFS 输入解析，重新验证全部 132 个工程、134 个目标，
转换、配置、编译、链接和产物检查全部通过。独立工程总数增加至 156 项，器件规格仍为 21 项。
同日进一步修复 SLIB 配套布局，L0、L1 的 Debug 和 Release 构建及 ARM 指令模拟调用均通过，
工程总数保持原值。物理开发板及芯片保护寄存器尚未测试。
同日从兆易创新官方网站下载 GD32F30x V3.0.3 和 GD32E23x V2.5.0，
测试 27 个原始 Keil 工程、31 个目标，全部在最终链接阶段失败，详情见文末。
11 月 9 日完成 GD32 启动与链接文件配套修复，全部 27 个工程、31 个目标通过重新构建，
通过清单增加至 183 个独立工程、27 种器件规格，详情见文末修复记录。
同日使用新增 SDK 批量工具重新验证其中 31 个工程、36 个目标，全部完成构建和
ELF 结构检查。重复构建按原工程去重，独立工程及器件总数保持 183 项、27 项。

## 验证标准

工程只有同时满足以下条件才计入清单：

1. 原始 `.uvprojx` 或 `.uvproj` 文件由 `keil_to_cmake.py` 读取。
2. 原始 Keil 工程和生成后的工程源码均未经过手工修改。
3. 生成 CMake 工程后完成 CMake 配置。
4. 使用 `arm-none-eabi-gcc` 完成源文件编译和最终链接。
5. 输出目录中存在对应的 ELF 固件。

多数工程还生成 BIN、HEX、MAP 和构建诊断报告。验证环境为 Windows、CMake 4.3.1、Ninja 1.13.2、MinGW Makefiles 和 GNU Arm Embedded Toolchain 13.3.1。

本清单证明所列工程在当前样本和工具链环境下通过构建。相同芯片的其他 SDK 版本、启动文件、链接脚本和中间件组合仍需单独验证。

## 汇总

| 项目 | 数量 |
| --- | ---: |
| 通过验证的独立 Keil 工程 | 183 |
| 通过验证的器件规格 | 27 |
| AT32F415 官方 examples | 118 |
| GD32 官方例程和模板 | 27 |
| 其他代表工程、utility 工程和模板 | 38 |

## 器件规格

| 厂商 | 器件规格 | 通过工程数 | 验证范围 |
| --- | --- | ---: | --- |
| ArteryTek | `AT32F415RCT7` | 132 | 官方 examples 118 项、utility 12 项、模板 2 项 |
| ArteryTek | `AT32F415CBT7` | 1 | EEPROM/FOC ESC 代表工程 |
| ArteryTek | `AT32F421K8T7` | 1 | FOC 代表工程 |
| ArteryTek | `AT32F435RGT7` | 2 | SDIO 与 E-Track 代表工程 |
| GigaDevice | `GD32F303RE` | 2 | MDK4、MDK5 模板的 HD 目标 |
| GigaDevice | `GD32F303RK` | 2 | MDK4、MDK5 模板的 XD 目标 |
| GigaDevice | `GD32F303ZE` | 11 | USBD 官方例程 |
| GigaDevice | `GD32F307VC` | 14 | USBFS Device 10 项、USBFS Host 2 项、MDK4 与 MDK5 模板 |
| GigaDevice | `GD32F307VG` | 1 | ENET Telnet 官方例程 |
| GigaDevice | `GD32E230C8` | 1 | 官方 MDK5 模板 |
| Texas Instruments | `MSP432P401R` | 1 | GPIO LED 代表工程 |
| Nationstech | `N32WB031` | 3 | HID、RF Test、WWDG |
| Nationstech | `N32WB031KEQ6-2` | 1 | Beacon/TOTP 工程 |
| Nordic Semiconductor | `nRF52810_xxAA` | 1 | ESB PTX |
| Nordic Semiconductor | `nRF52811_xxAA` | 1 | ESB PTX |
| Nordic Semiconductor | `nRF52820_xxAA` | 1 | ESB PTX |
| Nordic Semiconductor | `nRF52832_xxAA` | 1 | Gazell 动态配对 |
| Nordic Semiconductor | `nRF52833_xxAA` | 1 | ESB PTX |
| Nordic Semiconductor | `nRF52840_xxAA` | 1 | Gazell 动态配对 |
| Puya | `PY32F002Bx5` | 1 | USART Idle 中断 |
| Puya | `PY32MD320x8` | 2 | LL 模板和六步 BLDC |
| STMicroelectronics | `STM32F100ZE` | 1 | 标准外设库模板 |
| STMicroelectronics | `STM32F103C8` | 1 | PWM LED 工程 |
| STMicroelectronics | `STM32F103ZE` | 1 | USART 工程 |
| STMicroelectronics | `STM32F401VCTx` | 1 | SPI Receiver |
| STMicroelectronics | `STM32F401VE` | 1 | RTE 环境检测工程 |
| STMicroelectronics | `STM32F407VGTx` | 1 | SPI Transmitter |

GD32F30x 的两个模板工程分别包含三种器件目标，因此器件表中的工程数量合计大于
独立工程总数。独立工程按原始 Keil 文件去重，每种器件行内按该器件对应的工程文件去重。

## AT32F415 官方例程

验证根目录：

```text
AT32F415_Firmware_Library_V2.1.8/project/at_start_f415/examples
```

该目录下全部 118 个 `mdk_v5/*.uvprojx` 工程完成转换、编译和链接。
2026-11-08 重复验证覆盖其中全部 119 个目标，USART IRDA 工程包含两个目标。
分类统计按独立工程文件计算：

| 分类 | 工程数 | 分类 | 工程数 |
| --- | ---: | --- | ---: |
| ADC | 6 | CAN | 3 |
| CMP | 3 | Cortex-M4 | 3 |
| CRC | 1 | CRM | 3 |
| Debug | 1 | DMA | 2 |
| ERTC | 6 | EXINT | 2 |
| Flash | 2 | GPIO | 3 |
| I2C | 5 | I2S | 3 |
| PWC | 7 | SDIO | 3 |
| SPI | 8 | TMR | 25 |
| USART | 13 | USB Device | 13 |
| USB Host | 3 | WDT | 2 |
| WWDT | 1 | 合计 | 118 |

同一 SDK 另有 12 个 utility 工程和 2 个模板通过完整构建，来源及首次失败和修复记录见文末。

## 其他通过工程

以下路径省略本机盘符和 SDK 上级目录，仅保留用于定位样本的关键部分。

| 器件 | 工程路径 |
| --- | --- |
| `AT32F421K8T7` | `AT32/at32_foc/project/MDK_V5/project_file.uvprojx` |
| `AT32F415CBT7` | `AT32_ESC/mdk_v5/eeprom.uvprojx` |
| `AT32F435RGT7` | `AT32F435RGT7_SDIO/MDK-ARM_F435/proj.uvprojx` |
| `AT32F435RGT7` | `E-Track/MDK-ARM_F435/proj.uvprojx` |
| `MSP432P401R` | `MSP432_Quick_entry-master/02_RNA_LED/keil/empty.uvprojx` |
| `N32WB031` | `N32WB_HID/MDK-ARM/touch_screen.uvprojx` |
| `N32WB031` | `N32WB03x_SDK_V2.0.0/projects/n32wb03x_EVAL/prod_test/RF_Test/MDK-ARM/RF_Test.uvprojx` |
| `N32WB031` | `N32WB03x_SDK_V2.0.0/projects/n32wb03x_EVAL/peripheral/WWDG/WWDG_Reset/MDK-ARM/WWDG_Reset.uvprojx` |
| `N32WB031KEQ6-2` | `N32_TOTP/MDK-ARM/beacon.uvprojx` |
| `nRF52810_xxAA` | `nRF5_SDK_17.1.0/examples/proprietary_rf/esb_ptx/pca10040e/blank/arm5_no_packs/esb_ptx_pca10040e.uvprojx` |
| `nRF52811_xxAA` | `nRF5_SDK_17.1.0/examples/proprietary_rf/esb_ptx/pca10056e/blank/arm5_no_packs/esb_ptx_pca10056e.uvprojx` |
| `nRF52820_xxAA` | `nRF5_SDK_17.1.0/examples/proprietary_rf/esb_ptx/pca10100e/blank/arm5_no_packs/esb_ptx_pca10100e.uvprojx` |
| `nRF52832_xxAA` | `nRF5_SDK_17.1.0/examples/proprietary_rf/gzll/gzp_dynamic_pairing/host/pca10040/blank/arm5_no_packs/gzll_dynamic_pairig_host_pca10040.uvprojx` |
| `nRF52833_xxAA` | `nRF5_SDK_17.1.0/examples/proprietary_rf/esb_ptx/pca10100/blank/arm5_no_packs/esb_ptx_pca10100.uvprojx` |
| `nRF52840_xxAA` | `nRF5_SDK_17.1.0/examples/proprietary_rf/gzll/gzp_dynamic_pairing/host/pca10056/blank/arm5_no_packs/gzll_dynamic_pairig_host_pca10056.uvprojx` |
| `PY32F002Bx5` | `ebf_py32f002b/2-配套程序/USART/USART_IDLE_IT/MDK-Project/Project.uvprojx` |
| `PY32MD320x8` | `PY32MD320_Firmware_V0.0.5/Templates/PY32MD320xx_Templates_LL/MDK-ARM/Project.uvprojx` |
| `PY32MD320x8` | `PY32MD320_MC_BLC_SDK_V1.0/MDK-ARM/SixStepBLDC.uvprojx` |
| `STM32F100ZE` | `STM32F10x_StdPeriph_Lib_V3.6.0/Project/STM32F10x_StdPeriph_Template/MDK-ARM/Project.uvproj` |
| `STM32F103C8` | `STM32Project/6-3 PWM驱动LED呼吸灯/Project.uvprojx` |
| `STM32F103ZE` | `13-USART串口通信实验/Template.uvprojx` |
| `STM32F401VCTx` | `SPI_test_stm32f407/spi_receiver/MDK-ARM/spi_receiver.uvprojx` |
| `STM32F401VE` | `Home_Environment_Detection/工程/Keil工程/project.uvprojx` |
| `STM32F407VGTx` | `SPI_test_stm32f407/spi_transmitter/MDK-ARM/spi_transmitter.uvprojx` |

## 2026-11-08 算法优化后的重复验证

以下两个既有工程使用优化后的 `keil_to_cmake.py` 从原始 Keil 工程重新生成 CMake，
完成配置、全部源文件编译、最终链接以及 ELF、HEX、BIN 导出。
原始工程与生成后的工程源码均未经过手工修改。

验证环境：Windows、Python 3.13.12、CMake 4.3.1、MinGW Makefiles、
GNU Arm Embedded Toolchain 13.3.1。

| 器件 | 工程来源 | TargetName | ELF 文件 |
| --- | --- | --- | --- |
| `N32WB031` | `N32WB_HID/MDK-ARM/touch_screen.uvprojx` | `N32WB03x` | `beacon.elf` |
| `AT32F435RGT7` | `AT32F435RGT7_SDIO/MDK-ARM_F435/proj.uvprojx` | `X-Track` | `X-Track.elf` |

该轮重复验证计入原有记录，当时独立工程为 143 项，器件规格为 21 项，
AT32F415 官方 examples 仍为 118 项，其他代表工程和 utility 工程仍为 25 项。
器件表中的工程数量和例程分类统计保持原值。

本机证据保存在 `.cache/perf-20261108/`。两个工程分别使用
`n32-optimized/output/build-mingw` 和 `f435-optimized/output/build-mingw`。
AT32F435 首次完整构建生成全部产物，但验证器记录了超时；随后再次构建返回 0，
并通过 `arm-none-eabi-size` 检查。首次日志与再次检查日志均保留。

同日第二轮源码摘要与文件解析缓存优化后，两个工程再次从原始 Keil 输入生成 CMake，
并使用全新目录完成全部编译、链接和 ELF、HEX、BIN 导出，配置及构建命令均返回 0。
第二轮目录分别为 `n32-round2-1/output/build-mingw` 和
`f435-round2-1/output/build-mingw`，详细结果为 `round2-builds.json`。
构建前后的 82 个和 386 个输入及生成文件 SHA-256 校验值分别保持一致。
本轮仍属于上述两个工程的重复验证，工程总数、器件总数及分类数量保持原值。

转换耗时和生成内容一致性结果见 [转换性能记录](conversion_performance.md)。

## 2026-11-08 AT32F415 SDK 首次全量测试

SDK 为官方 `AT32F415_Firmware_Library_V2.1.8`，发布说明日期为 2025-11-24。
本机输入目录为 `D:\singlechip\AT32\f415\AT32F415_Firmware_Library_V2.1.8`。
全部工程的器件字段为 `-AT32F415RCT7`，统计时规范化为 `AT32F415RCT7`。

验证环境：Windows、Python 3.13.12、CMake 4.3.1、MinGW Makefiles、GNU Make 4.4.1、
Arm GNU Toolchain 13.3.Rel1，GCC 版本为 13.3.1 20240614。
生成工程引用本机 CMSIS 5.6.0，部分目标同时引用 ArteryTek AT32F415 DFP 2.1.8。
每个目标从原始 Keil 文件生成 CMake，并在全新目录执行 Debug 配置和完整构建。
测试关闭编译缓存，同时运行两个工程，每个工程使用两个编译进程。

| 分类 | 工程数 | 通过工程数 | 目标数 | 通过目标数 | 失败目标数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| examples | 118 | 118 | 119 | 119 | 0 |
| templates | 2 | 2 | 2 | 2 | 0 |
| utilities | 12 | 10 | 13 | 10 | 3 |
| 合计 | 132 | 130 | 134 | 131 | 3 |

全部 134 个目标完成转换和 CMake 配置。131 个通过目标均完成全部源文件编译、最终链接，
生成非空 ELF、HEX、BIN；ELF 的 ARM 类型检查和 `arm-none-eabi-size` 检查均通过。
本次 SDK 通过工程由原有 119 项增加至 130 项，新增 11 项；重复构建和同一工程中的多个目标分别去重。
全清单由 143 项增加至 154 项，器件规格保持 21 项，examples 分类统计保持 118 项。

通过的模板和 utility 工程如下，文件名均相对于本节 SDK 输入目录：

| 分类 | 工程文件 |
| --- | --- |
| MDK4 模板 | `project/at_start_f415/templates/mdk_v4/template.uvproj` |
| MDK5 模板 | `project/at_start_f415/templates/mdk_v5/template.uvprojx` |
| Boot Memory 读写 | `utilities/at32f415_boot_memory_ap_demo/read_write_boot_memory/mdk_v5/read_write_boot_memory.uvprojx` |
| Boot Memory 执行 | `utilities/at32f415_boot_memory_ap_demo/run_in_boot_memory/mdk_v5/run_in_boot_memory.uvprojx` |
| 随机数 | `utilities/at32f415_gen_random_number_demo/mdk_v5/gen_ramdom_number.uvprojx` |
| SLIB L0 | `utilities/at32f415_slib_demo/project_l0/mdk_v5/project_l0.uvprojx` |
| USART IAP LED3 | `utilities/at32f415_usart_iap_demo/source_code/app_led3_toggle/mdk_v5/app_led3_toggle.uvprojx` |
| USART IAP LED4 | `utilities/at32f415_usart_iap_demo/source_code/app_led4_toggle/mdk_v5/app_led4_toggle.uvprojx` |
| USART IAP Bootloader | `utilities/at32f415_usart_iap_demo/source_code/bootloader/mdk_v5/bootloader.uvprojx` |
| USB IAP LED3 | `utilities/at32f415_usb_iap_demo/source_code/app_led3_toggle/mdk_v5/app_led3_toggle.uvprojx` |
| USB IAP LED4 | `utilities/at32f415_usb_iap_demo/source_code/app_led4_toggle/mdk_v5/app_led4_toggle.uvprojx` |
| USB IAP Bootloader | `utilities/at32f415_usb_iap_demo/source_code/bootloader/mdk_v5/bootloader.uvprojx` |

### 本次未通过的工程

以下两个工程在首次测试时未计入通过清单，失败日志和构建目录保留在 `.cache/f415-20261108/`。
随后完成转换器修复并通过重新构建，结果见下一节；本节保留首次测试的诊断记录。

| 工程文件 | 失败目标 | 阶段 | 原因 | 构建日志 |
| --- | --- | --- | --- | --- |
| `utilities/at32f415_freertos_demo/mdk_v5/freertos_demo.uvprojx` | `freertos_demo_ac5`、`freertos_demo_ac6` | 编译 | RVDS 专用汇编进入 GCC 编译 | `p123/build.log`、`p124/build.log` |
| `utilities/at32f415_slib_demo/project_l1/mdk_v5/project_l1.uvprojx` | `project_l1` | 链接 | `FIR_lowpass_filter` 未定义 | `p127/build.log` |

FreeRTOS 的 `src/include_port.c` 通过 `__ARMCC_VERSION > 6000000` 选择 GCC 版 `port.c`，
其余情况选择 RVDS 版。GCC 编译时进入 RVDS 分支，触发 `__asm void` 等语法错误；
AC5 目标的头文件目录还包含 RVDS 版 `portmacro.h`。AC6 目标虽选择 GCC 版头文件目录，
仍受 `include_port.c` 中条件判断影响。两个目标均需要转换器处理编译器相关的移植层选择。

SLIB L1 的原始 Keil 工程通过 `FileType=3` 引用 `mdk_v5/fir_filter_symbol.txt`。
该文件采用 ARM Linker SYMDEFS 格式，包含 `0x0803e001 T FIR_lowpass_filter`。
生成后的 GCC 链接命令未包含该符号定义，因而在最终链接时失败。
这一工程依赖 SLIB L0 固件提供的函数地址，后续适配还需核对两个固件的地址关系。

### 耗时和完整性

全批次耗时 1474.511 秒，约 24 分 35 秒，包括转换、配置、完整编译链接、产物检查和完整性校验。
134 个目标的 CMake 生成耗时中位数为 1.989 秒，范围为 0.948 至 4.344 秒。
生成耗时在两个工程同时执行的条件下测得，仅作为本次测试记录。

测试前后 SDK 的 2,804 个文件 SHA-256 校验值保持一致，转换器文件校验值保持一致。
全部 134 个目标生成的 2,417 个输入文件逐项复核通过，校验值保持一致。
原始工程和生成后的工程源码均未经过手工修改。
构建过程中 CMake 在生成目录增加 `compile_commands.json`，该文件作为预期构建产物记录。
早期完整性检查曾把这项新增产物判为失败，调整检查规则后对既有构建结果重新校验，
原始检查记录保存在对应目录的 `initial-integrity-check.json`，转换器和工程源码保持原值。

部分构建日志含 SDK 的宏重复定义、格式化参数类型警告以及 newlib 系统调用占位提示。
本次验证覆盖转换与构建，开发板上的外设行为、FreeRTOS 调度、IAP 和 SLIB 功能仍需硬件测试。

完整目标清单见 `.cache/f415-20261108/manifest.json`，逐目标结果和汇总见 `summary.json`，
SDK 完整性结果见 `sdk-integrity.json`。每个 `pNNN` 目录保存转换、配置、构建、产物检查日志，
以及生成文件校验清单和 `result.json`。全部测试输出位于本项目 `.cache/f415-20261108/`。

## 2026-11-08 FreeRTOS 与 SYMDEFS 修复后全量验证

同一 AT32F415 V2.1.8 SDK 使用修复后的转换器，在全新输出目录重新生成和构建全部目标。
原始 SDK 和生成后的工程源码均未经手工修改。工具链、Debug 配置和并发设置与首次测试相同，
编译缓存保持关闭。

| 分类 | 独立工程数 | 构建目标数 | 通过目标数 | 失败目标数 |
| --- | ---: | ---: | ---: | ---: |
| examples | 118 | 119 | 119 | 0 |
| templates | 2 | 2 | 2 | 0 |
| utilities | 12 | 13 | 13 | 0 |
| 合计 | 132 | 134 | 134 | 0 |

全部目标通过转换、CMake 配置、全部源文件编译和最终链接，生成非空 ELF、HEX、BIN。
ELF 的 ARM 类型检查及 `arm-none-eabi-size` 检查均通过。批次耗时 1469.846 秒，约 24 分 30 秒。
相较首次测试新增两个通过工程，全清单从 154 项增加至 156 项，器件规格仍为 21 项。
AT32F415RCT7 工程数从 130 项增加至 132 项，examples 分类数量保持 118 项。

| 新增通过工程 | 目标 | ELF 文件 |
| --- | --- | --- |
| `utilities/at32f415_freertos_demo/mdk_v5/freertos_demo.uvprojx` | `freertos_demo_ac5`、`freertos_demo_ac6` | `freertos_demo_ac5.elf`、`freertos_demo_ac6.elf` |
| `utilities/at32f415_slib_demo/project_l1/mdk_v5/project_l1.uvprojx` | `project_l1` | `project_l1.elf` |

FreeRTOS 转换采用同一 SDK 内配套的 GCC `port.c` 和 `portmacro.h`，同时处理全局及文件级包含目录。
通过 `#include` 包装移植层源码的文件仅在生成副本中替换引用，保留包装文件中的原有代码。
两个 FreeRTOS ELF 均包含 SVC、PendSV、SysTick 和 `xPortStartScheduler` 的实际实现。

SYMDEFS 修复补充 Keil `FileType=3` 文本输入识别，复用已有的 GNU 链接符号片段生成功能。
`FIR_lowpass_filter` 在 L1 ELF 中解析为原符号表指定的 `0x0803e001`，Thumb 地址位保持原值。
ArmClang 输出继续保留原始符号文件作为链接输入。

本次新增 5 项单元测试，全部 143 项测试通过，覆盖移植层成套选择、包装源文件、
文件级编译设置、缺失配套文件、注释处理和双编译器 SYMDEFS 输出。
本机 Python 3.13 的临时目录权限导致首次标准测试启动失败，随后使用项目内测试启动脚本，
使临时目录继承项目权限，完成全部测试；两次日志均保留。

### 当时发现的 SLIB 配套问题

本次修复解决 SLIB L1 的符号导入与链接失败，符号地址遵循原始输入文件。
SDK 的说明要求符号表来自实际烧录的 L0 固件，同时匹配两个工程的工具链及内存布局。
本次 GCC L0 映射表中的 `FIR_lowpass_filter` 位于 `0x08001a4c`，与 L1 导入的
`0x0803e001` 指向的位置尚未匹配。L0、L1 的 SCT 分区、保留 RAM 和配套符号导出仍需适配。
因此，本次统计表示两个工程各自通过构建，不能将当前两个 GCC 固件视为通过联合运行验证的组合。
转换报告中增加固定地址符号需与配套固件地址及 ABI 一致的提示。
上述地址及分区问题在后续配套修复中解决，结果见下一节，首次记录保留用于比较。

### 验证证据

测试前后 SDK 的 2,804 个文件和本轮转换器内容保持一致。
全部目标的 2,420 个生成输入文件经 SHA-256 复核保持一致，构建新增文件仅为各目标的编译数据库。
首次测试通过的 131 个目标，其 BIN 固件与本轮输出逐项比较完全一致。
全部输出位于当前项目内，构建目录中未发现 reparse point，编译临时目录为空。

本机完整记录保存在 `.cache/f415-fix-20261108/`，其中 `manifest.json` 记录目标和工具链，
`summary.json` 记录全部构建结果，`sdk-integrity.json` 记录 SDK 完整性，`audit.json` 记录最终复核。
`p123`、`p124`、`p127` 分别保存此前三个失败目标的本轮完整日志和产物。
单元测试成功日志为 `unit-tests-inherited-acl.log`。
修复前的全部测试证据继续保存在 `.cache/f415-20261108/`。

## 2026-11-08 SLIB 配套布局与调用验证

验证输入仍为官方 AT32F415 V2.1.8 SDK 中的两个原始工程：

| 工程 | Keil 文件 | 构建配置 |
| --- | --- | --- |
| SLIB 算法提供方 L0 | `utilities/at32f415_slib_demo/project_l0/mdk_v5/project_l0.uvprojx` | Debug、Release |
| SLIB 调用方 L1 | `utilities/at32f415_slib_demo/project_l1/mdk_v5/project_l1.uvprojx` | Debug、Release |

本机输入根目录为 `D:\singlechip\AT32\f415\AT32F415_Firmware_Library_V2.1.8`。
验证环境为 Windows、Python 3.13.12、CMake 4.3.1、MinGW Makefiles、GNU Make 4.4.1、
Arm GNU Toolchain 13.3.Rel1，GCC 版本 13.3.1 20240614。
四次构建分别从原始 Keil 输入生成工程，在全新目录完成配置、全部编译和链接，
均生成 ELF、HEX、BIN；原始 SDK 和生成后的工程源码均未经手工修改。

### 修复内容

转换器从原始 SCT 提取并验证以下分区，取代普通工程的整片 Flash 与 RAM 布局：

| 区域 | 起始地址 | 大小 | 使用方 |
| --- | --- | --- | --- |
| 应用 Flash | `0x08000000` | 240 KiB | L0 应用或 L1 应用 |
| SLIB 运行库 | `0x0803C000` | 8 KiB | L0 算法依赖的运行库，L1 保留 |
| SLIB 指令 | `0x0803E000` | 4 KiB | L0 算法指令，L1 保留 |
| SLIB 常量 | `0x0803F000` | 4 KiB | L0 滤波系数，L1 保留 |
| SLIB RAM | `0x20000000` | 4 KiB | 算法状态，由两个工程的启动代码初始化 |
| 应用 RAM | `0x20001000` | 28 KiB | 普通数据、堆和栈 |

L0 的 `FIR_lowpass_filter` 固定在 `0x0803E000`，Thumb 函数指针为 `0x0803E001`，
与 L1 的原始符号约定一致。GNU 链接断言检查函数位置，构建后从实际 ELF 导出
`slib_exports.symdefs` 和 `slib_exports.ld`，并再次校验入口。
生成的链接脚本和符号片段参与增量构建依赖。

受保护的三个 C 文件使用 `-mpure-code -fno-jump-tables -fno-lto`，
指令区带有 ELF pure-code 标记，运行库与滤波常量留在独立的保留分区。
L0 与 L1 的真实复位流程均通过 `.preinit_array` 清零算法专用 RAM。
保护区中的非零初始化数据、未知 SCT 结构、分区重叠、符号地址越界和区域溢出会触发报错。
本次支持范围为该官方例程使用的静态 SCT 结构，ArmClang 仍使用原始 SCT。

### 执行验证

使用项目内安装的 Unicorn 2.1.4 加载实际构建产物：保留 L0 的 SLIB Flash 区域，
加载 L1 的应用区域，将 RAM 预填为非零值，从 L1 的真实 `Reset_Handler` 执行至 `main`。
模拟时仅替代硬件时钟初始化 `SystemInit`，C 运行库初始化和 SLIB RAM 初始化均执行实际固件代码。
随后执行 L1 `main` 中真实的调用指令，进入 L0 的滤波算法。

| 验证项 | Debug | Release |
| --- | --- | --- |
| L0、L1 入口一致 | 通过 | 通过 |
| L1 加载内容保留 SLIB Flash | 通过 | 通过 |
| 实际启动流程清零 SLIB RAM | 通过 | 通过 |
| L1 调用 L0 处理 320 个采样 | 通过 | 通过 |
| 信噪比达到官方 140 dB 标准 | 142.585 dB | 142.585 dB |
| 算法执行时读取受保护指令区 | 0 次 | 0 次 |
| 算法调用会被 L1 覆盖的 L0 应用代码 | 0 次 | 0 次 |

本次修复解决此前的配套地址、分区、依赖和初始化问题。
执行证据为 ARM 指令模拟；物理芯片的 SLIB 保护寄存器、实际烧录和外设行为尚未验收。

### 回归与记录

全部 151 项单元测试通过，其中 8 项覆盖 SLIB 分区解析、符号约定、源码完整性、
ArmClang 隔离，以及真实链接器对初始化数据和区域溢出的拒绝行为。
普通 SYMDEFS 工程保持原有转换行为，调用方 SCT 需包含 SLIB 标识才启用专用分区。
扫描 SDK 全部 134 个目标，仅 L0、L1 启用 SLIB 转换。
ADC、SDIO FatFs 和 FreeRTOS 三个代表目标重新完成完整构建，BIN 与上轮结果逐项一致。
SDK 的 2,804 个文件在配套构建前后校验一致。

本次属于原有工程的重复验证：全清单仍为 156 个工程、21 种器件规格，
AT32F415RCT7 为 132 个工程，其中 examples 118 个、utility 12 个、模板 2 个。
例程分类统计和工程来源保持原值。

最终构建记录为 `.cache/slib-20261108/final-v3/summary.json`，四组产物及日志位于
`final-v3/Debug/p126`、`final-v3/Debug/p127`、`final-v3/Release/p126`、`final-v3/Release/p127`。
两个配置的调用验证分别记录在对应目录的 `emulation.json`。
单元测试日志为 `.cache/slib-20261108/unit-tests-final-v3.log`，普通工程回归记录为 `regression.json`。
模拟器和测试工具仅安装在本项目 `.cache/slib-20261108/`，转换器未新增必装依赖。

## 2026-11-08 GD32 官方 SDK 转换测试

### 来源与范围

SDK 下载自兆易创新官方网站，官方发布页面及压缩包地址如下：

| SDK | 官方发布日期 | 发布页面 | 下载文件 |
| --- | --- | --- | --- |
| GD32F30x V3.0.3 | 2026-04-15 | [GD32F30x Firmware Library](https://www.gd32mcu.com/en/download/7?kw=GD32F30) | [官方 7z 文件](https://www.gd32mcu.com/data/documents/toolSoftware/GD32F30x_Firmware_Library_V3.0.3.7z) |
| GD32E23x V2.5.0 | 2026-02-11 | [GD32E23x Firmware Library](https://www.gd32mcu.com/en/download/7?kw=GD32E23) | [官方 7z 文件](https://www.gd32mcu.com/data/documents/toolSoftware/GD32E23x_Firmware_Library_V2.5.0.7z) |

两个下载文件均包含内层压缩包及官方 `SHA256.txt`，解包前的校验全部一致。
发布页面、内外层压缩包校验值和解包日志保存在 `.cache/gd32-20261108/`。
本机 SDK 根目录分别为：

```text
.cache/gd32-20261108/sdk/f30/content/GD32F30x_Firmware_Library_V3.0.3
.cache/gd32-20261108/sdk/e23/content/GD32E23x_Firmware_Library_V2.5.0
```

F30x 包含 50 个 Keil 工程文件、54 个目标，其中 MDK4 和 MDK5 各 25 个工程。
E23x 包含一个 MDK5 模板工程。多数 GPIO、ADC、DMA、定时器及串口例程仅提供源码，
未附独立 Keil 工程。本轮采用全部原始 MDK5 工程和 F30x 的 MDK4 模板，
共 27 个工程、31 个目标；未把源码例程组装成新的测试工程。

| SDK | 内核 | 测试工程数 | 测试目标数 | 最终通过目标数 |
| --- | --- | ---: | ---: | ---: |
| GD32F30x V3.0.3 | Cortex-M4 | 26 | 30 | 0 |
| GD32E23x V2.5.0 | Cortex-M23 | 1 | 1 | 0 |
| 合计 | | 27 | 31 | 0 |

F30x 覆盖 `GD32F303ZE`、`GD32F303RE`、`GD32F303RK`、`GD32F307VC`、`GD32F307VG`；
E23x 覆盖 `GD32E230C8`。分类为 USBD 11 个目标、USBFS Device 10 个目标、
USBFS Host 2 个目标、ENET Telnet 1 个目标，以及模板 7 个目标。
两个 F30x 模板各含 CL、XD、HD 三个目标，独立工程统计分别计一次。

工程来源为 F30x 的 `Examples/USBD/*/MDK-ARM/*.uvprojx`、
`Examples/USBFS/USB_Device/*/MDK-ARM/*.uvprojx`、
`Examples/USBFS/USB_Host/*/MDK-ARM/*.uvprojx`、
`Examples/ENET/Telnet/MDK-ARM/Project.uvprojx`、
`Template/Keil4_project/Project.uvproj`、`Template/Keil5_project/Project.uvprojx`，
以及 E23x 的 `Template/Keil_project/Project.uvprojx`。
逐工程文件名、TargetName 和器件字段完整记录在本轮 `manifest.json`。

### 测试结果与原因

验证环境为 Windows、Python 3.13.12、CMake 4.3.1、MinGW Makefiles、GNU Make 4.4.1、
Arm GNU Toolchain 13.3.Rel1，GCC 版本为 13.3.1 20240614。
每个目标使用当前 `keil_to_cmake.py` 命令行从原始工程生成 CMake，在全新目录执行 Debug 构建，
关闭编译缓存，同时运行两个工程，每个工程使用两个编译进程。

全部 31 个目标通过转换和 CMake 配置，完成编译并执行最终链接，
均因 GNU 链接脚本引用未定义的 `__gVectors` 而失败，未生成有效 ELF 固件。
当前生成的启动文件使用 `.isr_vector` 段及 `__Vectors` 符号，
所选 GD32 链接脚本要求 `.vectors` 段及 `__gVectors` 符号，两者未成套匹配。

链接脚本选择还存在独立的兼容性问题：

| 目标 | 当前选中的脚本 | 问题 |
| --- | --- | --- |
| 全部 F30x 目标 | `Examples/USBD/msc_cdrom/src/gd32f303xE_flash.ld` | 引入光盘示例的 `0x08004000` 固定数据分区，并将多种器件套用为 512 KiB Flash、64 KiB RAM |
| E23x 模板 GD32E230C8 | `Firmware/CMSIS/GD/GD32E23x/Source/GCC/Ld/gd32e225x8_flash.ld` | 文件标识为 GD32E225，目标器件匹配依据不足；该脚本的 64 KiB Flash、8 KiB RAM 与当前目标容量相同 |

`_guess_gcc_linker_script()` 对目录内候选文件按名称评分选取，当前未确保芯片、
用途和启动符号同时匹配。单独补充 `__gVectors` 仍无法解决上述脚本选取问题，
后续修复应成套检查启动文件、链接脚本、内存规格和专用分区。
本轮仅执行当前转换器兼容性测试与原因分析，转换器代码保持测试前内容。

部分日志同时包含 SDK 宏重复定义、格式化参数及 `wchar_t` 宽度警告，
这些警告与本轮阻断链接的首个错误分别记录。
由于全部目标均未通过最终链接，现有通过清单保持 156 个工程、21 种器件规格，
本轮 GD32 器件和工程均未加入通过统计，物理开发板测试尚未执行。

### 文件完整性与证据

测试前后 F30x 的 3,460 个文件、E23x 的 626 个文件 SHA-256 校验一致。
转换器相关模块的校验值保持一致，31 个目标的生成输入文件保持原值，
构建新增的 `compile_commands.json` 作为预期产物记录。
原始 Keil 工程及生成后的工程源码均未经手工修改。

本轮全部下载、解包、构建、日志和临时文件均位于当前项目 `.cache/gd32-20261108/`。
解包工具来自 7-Zip 官方网站，保存在该目录的 `tools/`，仅用于本轮 SDK 解包。
最终检查未发现输出目录中的 reparse point，编译临时目录无残留文件。

`manifest.json` 记录工程与工具链，`summary.json` 记录 31 个目标的失败结果，
`audit.json` 记录文件完整性检查。`runs/p000` 至 `runs/p030` 保存各目标的
原始转换、配置和构建日志、生成文件及 `result.json`，供后续修复后比较。

## 2026-11-09 GD32 配套修复后验证

同一批原始工程使用修复后的转换器，在 `.cache/gd32-fix-20261108/final/` 的全新目录
完成 Debug 配置、全部编译、最终链接及 ELF、HEX、BIN 导出，31 个目标全部通过。
SDK 版本、下载来源、工程文件、工具链及并发参数与上一节一致，编译缓存继续关闭。

| 分类 | 独立工程数 | 构建目标数 | 通过目标数 |
| --- | ---: | ---: | ---: |
| GD32F30x USBD | 11 | 11 | 11 |
| GD32F30x USBFS Device | 10 | 10 | 10 |
| GD32F30x USBFS Host | 2 | 2 | 2 |
| GD32F30x ENET Telnet | 1 | 1 | 1 |
| GD32F30x MDK4、MDK5 模板 | 2 | 6 | 6 |
| GD32E23x MDK5 模板 | 1 | 1 | 1 |
| 合计 | 27 | 31 | 31 |

新增 `gd32_assets.py` 仅在 GD32 GCC 转换中加载。依据原始启动文件定位同一 SDK 的
GCC 启动文件，逐项核对中断向量；依据器件型号和容量标识选择同目录 `GCC/Ld` 中的
准确脚本文件，不再遍历相邻例程寻找最高评分文件。
原工程实际引用的源码目录含同型号专用脚本时优先采用该脚本。

生成副本保留 Keil 目标的 Flash、RAM、栈和堆设置；GD32F303RK 的 MDK4 模板为
192 KiB RAM，MDK5 模板为 96 KiB RAM，分别遵循原工程配置并记录差异。
USBD 光盘例程保留自身的专用脚本，USBFS 光盘例程依据同一变量的 Arm `at()` 与
GCC `section()` 声明生成固定分区，两个固件的 `Flash_Data` 均位于 `0x08004000`。
固定地址、向量表起点和栈边界由链接断言检查，堆分配沿用已有的有界 `_sbrk` 实现。
缺失配套文件、向量表差异、固定地址冲突、多内存区域和自定义 SCT 明确报错。
ArmClang 生成继续使用原有流程，本轮未修改 SDK 或手工修改生成后的源码。

### ELF 与启动检查

全部 31 个 ELF 的入口、中断向量数量、每项中断地址、初始栈指针、Flash 装载区域及
RAM 使用范围均通过检查；两个光盘例程的固定数据地址检查通过。
GNU `size` 与 `readelf` 检查均成功。

对 `p027` 的 GD32F307VC Cortex-M4 和 `p030` 的 GD32E230C8 Cortex-M23 使用已有的
项目内 Unicorn 模拟器，从真实 ELF 中的复位入口执行至 `main`。
RAM 预填非零值后，实际启动代码完成 `.data` 复制及 `.bss` 清零。
模拟仅替代硬件时钟配置 `SystemInit`，C 运行库初始化执行实际代码。
该结果验证启动代码的配套关系，物理开发板、USB、以太网及芯片外设仍需硬件验证。

### 证据

全部 159 项单元测试通过，新增 8 项覆盖 GD32 配套选择、内存与固定地址保留、
错误输入诊断、原始文件保护，以及 ArmClang 生成隔离。
AT32F415 ADC、FreeRTOS、SLIB L0、SLIB L1 及 N32WB_HID 共 5 个代表目标
重新完成完整构建，BIN 与此前通过的固件逐项一致，记录为 `regression/comparison.json`。

完整目标清单为 `.cache/gd32-fix-20261108/final/manifest.json`，构建汇总为
`final/summary.json`，逐目标日志和固件位于 `final/runs/p000` 至 `final/runs/p030`。
ELF 与启动模拟检查为 `firmware-checks.json`，单元测试日志为 `unit-tests.log`，
原始失败批次继续保留在
`.cache/gd32-20261108/`。
性能测量方法与数据见 [转换性能记录](conversion_performance.md)。

本次新增 27 个通过工程、6 种器件规格；全清单增加至 183 个独立工程、27 种器件规格。
AT32F415 原有工程数量、来源和分类统计保持原值。

## 2026-11-09 SDK 批量工具验证

新增 `tools/run_regression.py` 使用 JSON 清单集中管理 SDK 获取、目标发现、
转换、构建、失败分组和结果复用。操作方式见 [SDK 批量转换验证](batch_validation.md)，
完整工具验收见 [回归基线](regression_baseline.md)。

实际构建沿用 Windows、Python 3.13.12、CMake 4.3.1、MinGW Makefiles、GNU Make 4.4.1
及 GCC 13.3.1 20240614。全部采用 Debug，两个目标并发，每个目标两个编译进程，
关闭编译缓存。

| SDK | 版本 | 独立工程 | 目标 | 通过目标 |
| --- | --- | ---: | ---: | ---: |
| GD32F30x | 3.0.3 | 26 | 30 | 30 |
| GD32E23x | 2.5.0 | 1 | 1 | 1 |
| AT32F415 | 2.1.8 | 4 | 5 | 5 |
| 合计 | | 31 | 36 | 36 |

GD32 的工程及分类与前述配套修复验证相同：USBD 11 项、USBFS Device 10 项、
USBFS Host 2 项、ENET 1 项、模板 3 项，包含 MDK4 和 MDK5 两个 F30x 模板。
AT32 选中 ADC `current_vref_value_check` 一个工程、FreeRTOS 一个工程的 AC5 和 AC6
两个目标、SLIB L0 和 L1 两个工程。器件为 `AT32F415RCT7` 及既有六种 GD32 器件。
本轮没有新增独立工程或器件；本文件器件表及全量 SDK 分类统计保持原数量。

AT32 输入仍来自
`D:/singlechip/AT32/f415/AT32F415_Firmware_Library_V2.1.8`；F30x 输入仍为
`.cache/gd32-20261108/sdk/f30/content/GD32F30x_Firmware_Library_V3.0.3`。
E23x 由批量工具从清单中的同一官方地址下载，经过外层和内层 SHA-256 校验后解包到
`.cache/sdk-cache/sdks/`，准确输入目录记录于本轮 `plan.json`。
各工程相对位置和目标名称完整保存在结果清单。

全部工程由 `keil_to_cmake.py` 从原始 Keil 文件生成，原工程及生成后的源码均未经
手工修改。全部目标通过 CMake 配置、GCC 编译和最终链接，生成 ELF、HEX、BIN，
并通过 `size` 和 ELF 入口、向量、栈及内存范围检查。
AT32 的 2,804 个 SDK 文件、F30x 的 3,458 个 SDK 文件、E23x 的 624 个 SDK 文件
在构建前后保持一致；这里的 SDK 文件数不包含官方外层说明及内层压缩包文件。
外设和保护寄存器的硬件验证仍以之前各节注明的范围为准，本轮未新增开发板测试。

证据保存在 `.cache/batch-20261109/official/`，包括 `plan.json`、`context.json`、
`summary.json`、`integrity.json` 和各次构建目录。
首次尝试中的两个头文件查找失败来自 Windows GCC 展开较深输出目录后的长度限制，
原始失败产物保留；缩短批量缓存和构建目录名称后，36 个目标全部通过。

同一命令再次执行时全部 36 个结果复用，新增构建次数为零；完整构建耗时 712.532 秒，
复用运行耗时 38.547 秒。该比较覆盖输入扫描和文件校验，属于本机单次验收记录。
计时保存在 `.cache/batch-20261109/official-timing.json`，完整性审计为 `audit.json`。
随后补充中断目标自动重试处理，单元测试总数为 187 项，全部通过；
最终版本再次构建 GD32F30x MDK5 模板的三个目标、GD32E23x 模板及 AT32F415 ADC，
共五个目标全部通过，重复运行全部复用。记录在 `final-smoke/`、`smoke-timing.json`
及 `final-audit.json`，详细统计见回归基线。

## 2026-11-09 桌面交互与取消功能验证

通过桌面界面从原始 Keil 工程执行转换和完整构建，验证阶段显示、实时日志、
运行期间设置锁定、编译中取消和重新运行。

| 工程 | 器件 | 验证结果 |
| --- | --- | --- |
| AT32F415 V2.1.8 `project/at_start_f415/examples/gpio/led_toggle/mdk_v5/led_toggle.uvprojx` | AT32F415RCT7 | 编译中取消，重新运行后配置、编译、链接及 ELF 生成通过 |
| GD32E23x V2.5.0 `Template/Keil_project/Project.uvprojx` | GD32E230C8 | 配置、编译、链接及 ELF 生成通过 |

AT32 来源为 `D:\singlechip\AT32\f415\AT32F415_Firmware_Library_V2.1.8`，
GD32 来源为 `.cache/gd32-20261108/sdk/e23/content/GD32E23x_Firmware_Library_V2.5.0`。
原始 SDK 文件及构建期间生成源码的 SHA-256 校验通过，未手工修改工程或源码。
两项均为既有通过工程的重复验证，独立工程仍为 183 项，器件规格仍为 27 项；
器件表工程数量与批量例程分类统计保持原值。

验证环境为 Windows、CMake 4.3.1、MinGW Makefiles、GNU Make 4.4.1、
Arm GNU Toolchain 13.3.Rel1 的 GCC 13.3.1 20240614。
本轮将已有原生 `make.exe` 复制为项目内的 `mingw32-make.exe` 以供 CMake 自动发现。
取消后的 AT32 完整运行耗时 10.386 秒，GD32 完整运行耗时 21.540 秒；
时间包含 GUI 发起转换和构建，不代表单独的转换耗时。

首次 Ninja 运行在本机环境停滞，深层 GD32 SDK 缓存出现 Windows GCC 头文件
查找失败；相应测试产物保留，最终使用上述较短的既有 SDK 副本及 Make 完成验收。
证据位于 `.cache/ui-interaction-20261109/exercise.json`，成功构建分别位于
`at32f415-verified/` 和 `gd32e230-verified/`。未新增开发板硬件验证。

## 维护规则

新增记录必须来源于真实构建结果。仅完成 XML 解析、CMake 文件生成或单个源文件编译的工程不能加入本清单。新增芯片或工程时，应同步记录器件名称、工程路径、工具链版本和最终固件类型。
