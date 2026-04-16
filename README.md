# Keil 转 CMake 工具

当前项目用于读取 Keil `uvprojx` 工程文件，提取目标芯片、源文件、头文件目录、宏定义、编译参数、链接参数与 RTE 组件信息，并生成可供 CMake 与 VS Code 使用的工程文件。

当前仓库同时提供图形界面与命令行入口，二者共用 `keil_to_cmake.py` 中的解析与生成核心。生成结果既支持 Keil MDK ArmClang，也支持 GNU Arm Embedded GCC，并可按配置输出 VS Code 的构建任务与调试配置。

## 主要能力

1. 解析 `uvprojx` 中的目标名称、输出文件名、设备型号、厂商信息与 CPU 描述。
2. 收集工程中的源文件、头文件目录、显式包含目录与预处理宏。
3. 读取 Keil RTE 信息，并从 PDSC 文件中补入相关源文件与目录。
4. 生成 `CMakeLists.txt`、工具链文件、构建脚本与 `.vscode` 配置。
5. 为 GCC 场景补充启动文件转换、链接脚本推断、产物导出与调试器配置。
6. 支持 OpenOCD、J-Link、PyOCD、Keil MDK 等调试后端枚举。

## 当前支持情况

当前代码对以下两类目标处理较完整：

| 目标系列 | 当前状态 | 说明 |
| --- | --- | --- |
| `STM32F103` | 支持较完整 | 包含 F1 宏推断、ARMASM 启动文件转 GNU 汇编、`core_cm3.c` 修补、回退链接脚本生成、OpenOCD 默认目标脚本 |
| `AT32F415` | 支持较完整 | 包含 MDK 启动文件切换到 GCC 版本、`.ld` 文件推断、ArteryTek OpenOCD 默认目标脚本、J-Link 与 ST-Link 配置生成 |

其他系列通常可以完成基础转换，但编译参数、启动文件、链接脚本、器件宏、OpenOCD 配置与 J-Link 设备名常需补充规则。

## 目录说明

| 文件 | 作用 |
| --- | --- |
| `main.py` | PySide6 图形界面入口 |
| `keil_to_cmake.py` | 工程解析与 CMake 生成核心 |
| `get_keil_mdk_info.py` | Keil MDK 安装目录与 `TOOLS.INI` 信息读取 |
| `config.ini` | 图形界面配置保存文件 |
| `keil_to_cmake_guide.html` | 当前仓库的详细介绍文档 |
| `AC7916.html` | 页面结构参考模版 |

## 运行方式

### 图形界面

执行：

```powershell
python main.py
```

界面中可选择以下内容：

1. `uvprojx` 工程文件
2. Keil MDK 安装目录
3. 输出目录
4. 编译器
5. 宿主系统
6. CMake Generator
7. 硬件探针
8. 调试后端
9. 构建目录

勾选“生成 VS Code 配置”后，输出目录中会新增 `.vscode` 相关文件。

### 命令行

示例：

```powershell
python keil_to_cmake.py project.uvprojx `
  --compiler gcc `
  --host-os windows `
  --generator Ninja `
  --debug-probe stlink `
  --debug-backend openocd `
  --build-dir build-gcc `
  --export-vsc-settings `
  --destination D:\output\demo
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--compiler` | 选择 `armclang` 或 `gcc` |
| `--host-os` | 选择 `windows`、`linux`、`macos` |
| `--generator` | 指定 CMake Generator |
| `--debug-probe` | 指定探针类型 |
| `--debug-backend` | 指定调试后端 |
| `--build-dir` | 指定构建目录名称 |
| `--export-vsc-settings` | 输出 `.vscode` 配置 |
| `--destination` | 指定输出目录 |
| `--uv4-path` | 指定 Keil MDK 安装目录 |

## 生成结果

典型输出目录结构如下：

```text
output/
├─ CMakeLists.txt
├─ do_build.bat 或 do_build.sh
├─ cmake/
│  ├─ armclang.cmake
│  ├─ arm-none-eabi-toolchain.cmake
│  ├─ generated_linker.ld
│  ├─ startup_xxx_gcc.S
│  └─ core_cm3_gcc.c
└─ .vscode/
   ├─ settings.json
   ├─ tasks.json
   ├─ launch.json
   ├─ c_cpp_properties.json
   ├─ jlink_flash_elf.jlink
   ├─ jlink_flash_hex.jlink
   └─ jlink_flash_bin.jlink
```

GCC 模式下，任务配置中通常会包含以下项目：

1. `Configure GCC Debug`
2. `Build GCC Debug`
3. `Rebuild`
4. `Flash ST-Link ELF`
5. `Flash ST-Link HEX`
6. `Flash ST-Link BIN`
7. `Start ST-Link OpenOCD GDB Server`
8. `Flash J-Link ELF`
9. `Flash J-Link HEX`
10. `Flash J-Link BIN`

## 详细文档

当前仓库已经补入 HTML 形式的详细介绍文档：

`keil_to_cmake_guide.html`

该文档包含以下内容：

1. 模块结构
2. 处理顺序
3. GUI 与 CLI 说明
4. VS Code 配置说明
5. `STM32F103` 与 `AT32F415` 的适配依据
6. 其他系列的主要修改位置
7. 关键函数索引

## 参考资料

当前项目参考了 `uvprojx2cmake` 的基础思路与工程方向：

[https://gitee.com/quincyzh/uvprojx2cmake](https://gitee.com/quincyzh/uvprojx2cmake)

参考关系主要体现在以下方面：

1. 目标一致，均围绕 Keil `uvprojx` 到 CMake 的工程转换。
2. 生成文件命名与整体思路存在明显关联，例如当前代码中的生成头注释仍保留了 `keil_uvprojx2cmake.py` 的历史命名痕迹。
3. 当前仓库在此基础上增加了图形界面、Keil MDK 自动发现、VS Code `tasks.json` 与 `launch.json` 输出、OpenOCD 与 J-Link 配置生成，以及面向 `STM32F103` 与 `AT32F415` 的 GCC 适配处理。

因此，`uvprojx2cmake` 更接近当前仓库的参考来源之一；当前仓库则面向本地使用场景加入了更多与 VS Code、调试器和芯片系列有关的扩展代码。

## 依赖环境

根据所选编译器与调试方式，需要具备以下工具中的部分或全部：

| 工具 | 用途 |
| --- | --- |
| Python 3 | 运行图形界面与命令行程序 |
| PySide6 | 图形界面 |
| CMake | 工程生成与编译 |
| Ninja 或其他 Generator 对应工具 | 构建后端 |
| Keil MDK | ArmClang 模式、RTE 组件信息、Keil 调试任务 |
| GNU Arm Embedded GCC | GCC 模式编译 |
| OpenOCD | ST-Link、DAPLink、Nu-Link、部分 J-Link 场景 |
| SEGGER J-Link Software | J-Link 烧录与调试 |
| VS Code 与 Cortex-Debug 扩展 | 调试配置使用 |

## 补充说明

当前仓库的主要目标，是把常见 Keil Cortex-M 工程转换为更便于在 VS Code 中维护和编译的 CMake 工程。对于新系列芯片，通常需要从以下函数开始补写规则：

1. `_inferred_device_defines()`
2. `_prepared_gcc_source_files()`
3. `_guess_gcc_linker_script()`
4. `_default_openocd_target_script()`
5. `_build_vscode_tasks()`
6. `_build_vscode_launch()`

相关细节已整理在 `keil_to_cmake_guide.html` 中。
