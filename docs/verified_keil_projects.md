# Keil 工程转换验证清单

本文档记录经过实际转换和 GCC 构建验证的 Keil 工程。统计日期为 2026 年 11 月 8 日。
本次对清单中的两个工程进行了算法优化后的重复验证，独立工程和器件数量保持原值。

## 验证标准

工程只有同时满足以下条件才计入清单：

1. 原始 `.uvprojx` 或 `.uvproj` 文件由 `keil_to_cmake.py` 读取。
2. 转换过程没有修改原始 Keil 工程。
3. 生成 CMake 工程后完成 CMake 配置。
4. 使用 `arm-none-eabi-gcc` 完成源文件编译和最终链接。
5. 输出目录中存在对应的 ELF 固件。

多数工程还生成 BIN、HEX、MAP 和构建诊断报告。验证环境为 Windows、CMake 4.3.1、Ninja 1.13.2、MinGW Makefiles 和 GNU Arm Embedded Toolchain 13.3.1。

本清单证明所列工程在当前样本和工具链环境下通过构建。相同芯片的其他 SDK 版本、启动文件、链接脚本和中间件组合仍需单独验证。

## 汇总

| 项目 | 数量 |
| --- | ---: |
| 通过验证的独立 Keil 工程 | 143 |
| 通过验证的器件规格 | 21 |
| AT32F415 官方 examples | 118 |
| 其他代表工程和 utility 工程 | 25 |

## 器件规格

| 厂商 | 器件规格 | 通过工程数 | 验证范围 |
| --- | --- | ---: | --- |
| ArteryTek | `AT32F415RCT7` | 119 | 官方 examples 全量验证 118 项，加 USB IAP utility 1 项 |
| ArteryTek | `AT32F415CBT7` | 1 | EEPROM/FOC ESC 代表工程 |
| ArteryTek | `AT32F421K8T7` | 1 | FOC 代表工程 |
| ArteryTek | `AT32F435RGT7` | 2 | SDIO 与 E-Track 代表工程 |
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

## AT32F415 官方例程

验证根目录：

```text
AT32F415_Firmware_Library_V2.1.8/project/at_start_f415/examples
```

该目录下全部 118 个 `mdk_v5/*.uvprojx` 工程完成转换、编译和链接。分类统计如下：

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

另外通过以下 utility 工程：

```text
utilities/at32f415_usb_iap_demo/source_code/app_led4_toggle/mdk_v5/app_led4_toggle.uvprojx
```

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

本次重复验证计入原有记录，独立工程仍为 143 项，器件规格仍为 21 项，
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

## 维护规则

新增记录必须来源于真实构建结果。仅完成 XML 解析、CMake 文件生成或单个源文件编译的工程不能加入本清单。新增芯片或工程时，应同步记录器件名称、工程路径、工具链版本和最终固件类型。
