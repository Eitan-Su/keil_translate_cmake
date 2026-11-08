# Keil 工程通用转换为 GCC 工程的实施方案（执行规格版）

## 一、文档说明

本文档是 `keil_to_cmake.py` 通用化改造的唯一方案来源，版本 2，更新日期 2026-07-11。内容由仓库维护方与协作方 codex2 经三轮技术讨论形成，本版在设计说明之外补入执行规格：中间模型的字段定义、构建验证器的判定规则、九个阶段各自的任务分解与验收标准、回归验证规范与阶段状态表。

改造目标：使更多 Keil `uvprojx` 工程在转换为 CMake 加 `arm-none-eabi-gcc` 工程后，无需人工修改即可编译通过。当前代码对 `STM32F103`、`STM32F407`、`AT32F415` 适配较完整，其余芯片系列转换后经常出现编译或链接错误。

执行者阅读顺序：第八、九、十节为执行核心，第十四节提供与现有代码的函数级映射，第十六节规定完成定义与禁止事项，第十七节跟踪进度。设计背景见第二至七节。

## 二、目标与现状差距

预期目标是把常见 Cortex-M 裸机 Keil 工程转换为可编译的 GCC 工程。现状是转换过程以少数芯片系列的经验规则为主，缺少对转换结果的编译验证，产出无法编译的工程时也不报错。差距集中在四个方面：芯片适配覆盖面、资产来源可靠性、库与汇编的跨编译器可迁移性、失败的可诊断性。

## 三、编译失败根因分类

以下每一类均已对照现有代码核验。

**启动文件覆盖面窄。** `_generate_gnu_startup_from_armasm()` 仅对 STM32 与 AT32 的 ARMASM 语法启动文件做正则提取与机械生成，生成的复位序列假设 `_estack`、`_sidata`、`_sdata`、`_edata`、`_sbss`、`_ebss` 等固定符号。当所选链接脚本采用不同符号命名时，链接阶段因符号缺失而失败。原始启动文件中较复杂的初始化逻辑在机械生成过程中丢失。

**回退链接脚本过于简化。** `_generate_fallback_gcc_linker_script()` 只生成 FLASH 与 RAM 两段内存。`_extract_memory_region()` 只取第一个 `IROM` 与 `IRAM`，忽略 `IROM2` 与 `IRAM2`。CCMRAM、TCM、SRAM2、外部 SDRAM 等多段内存无处安放。`_guess_gcc_linker_script()` 在工程目录递归匹配 `.ld` 时，可能选中其他芯片的脚本。

**器件宏不完整。** `_inferred_device_defines()` 与 `_infer_stm32f1_stdperiph_define()` 只覆盖 STM32F1 系列的标准外设库宏，其余系列依赖启动文件名推断。HAL 库所需的器件宏与 `USE_HAL_DRIVER` 等定义在多数非 STM32 系列缺少规则，GD32、HC32、MM32、Nordic、NXP 等厂商无对应处理。

**预编译库未被解析且难以迁移。** `self.libraries` 自初始化为空列表后，解析阶段从未向其写入，工程中以 `.lib` 形式引用的库被按后缀过滤后丢弃。即便补上解析，armlink 专用格式的库（部分 AC5 编译产物、加密库）也无法被 GNU ld 链接。

**非启动的 ARMASM 汇编文件无法编译。** 工程中除启动文件外的 `.s` 汇编若采用 armasm 语法，GNU as 无法编译，当前不做任何转换与检测。

**编译器方言差异。** armclang 基于 clang，其专有扩展、`#pragma`、内联汇编约束在 GCC 下报错。当前仅对 `core_cm3.c` 通过 `_generate_patched_core_cm3_source()` 做单点修补，缺少通用的方言检测。

**产出无验证。** `_generate_configured_output()` 完成文件写出即返回成功，不调用编译器验证转换结果，无法区分可用工程与无法编译的工程。

**文件级编译选项丢失。** `_is_file_included_in_build()` 只读取 `FileOption/CommonProperty/IncludeInBuild`。Keil 支持对单个文件设置独立的优化级别、宏定义与包含目录（`FileOption/FileArmAds`），这些设置当前未被解析，依赖单文件差异化编译的工程行为会改变。

**scatter 文件语义未被利用。** `_parse_linker_settings()` 读取的 `ScatterFile` 仅供 armclang 分支使用。GCC 分支放弃 scatter 语义，改用查找 `.ld` 加内存信息回退。真实工程的 `.sct` 中含加载域与执行域的准确布局，比从 `IROM` 与 `IRAM` 推测可靠得多。

## 四、设计原则

**资产优先级序列。** 启动文件、system 文件、链接脚本与 RTE 配置，依次优先采用工程现有资产、同版本厂商 SDK、匹配的 CMSIS-Pack，机械生成仅作末级备用。此序列使转换结果尽量与厂商官方版本一致，避免长期维护一份偏离上游的复制集合。

**器件数据表只存硬件事实。** 器件描述表保存 CPU、FPU、内存布局、器件宏、OpenOCD 目标等硬件事实与少量兼容规则，成套的启动与链接资产尽量来自工程、SDK 或 Pack，而非写死在表中。

**资产来源可追溯。** 每项被采用的资产记录来源、匹配依据、可信等级与支持等级。存在歧义或语义缺失时进入诊断报告，避免静默选择。

**失败可诊断而非静默。** 无法自动转换的部分明确标注为需人工处理并给出原因，而非产出无法编译的工程后交由使用者自行排查。

## 五、目标架构

在当前单文件实现之外，逐步引入轻量中间结果模型作为各模块之间的稳定接口，随后按职责划分六个模块。中间模型先行，可避免后续验证器与生成器依赖 `KeilProjectToCMake` 的大量成员变量。

**轻量中间模型**：规范化的工程事实，字段定义见第八节，各模块只依赖该模型。
**Project Parser**：解析 `uvprojx` 与 `uvproj`、文件级设置、库引用与 RTE 引用，产出中间模型；现有解析器保留为默认前端。
**Pack Resolver**：Pack 定位、版本选择、PDSC 继承与 condition 求值。
**Asset Resolver**：按资产优先级序列为启动文件、system 文件、链接脚本与设备头文件做成套选择并记录来源。
**Compatibility Analyzer**：检查汇编方言、库格式、ABI、专有 `pragma` 与链接符号，标记无法自动迁移的项。
**CMake Generator**：仅依据中间模型生成 CMake target，不内嵌芯片判断逻辑。
**Build Validator**：配置、编译、链接、产物检查与错误分类，是度量其余模块效果的基线。

## 六、可复用的开源底座

**CMSIS-Toolbox 的 uv2csolution。** 可把 `uvprojx` 转为 `csolution` 与 `cproject`，从中提取设备、组件、文件与构建类型。作为可选增强前端与内置解析器交叉校验：内置解析器始终执行，检测到工具时用其输出补充器件规范化、Pack 信息与工程结构。对无 RTE 的纯手写工程收益有限，对 GCC 兼容问题帮助不大。

**cpackget。** Pack 下载、安装与索引管理。复用标准 Pack 缓存比自定义下载机制更容易与 Keil、CMSIS-Toolbox 及本地测试环境共存。

**pyOCD。** 含 CMSIS-Pack 支持、目标描述与 flash algorithm 处理，可复用其 Pack 解析思路，或用于验证器件名称、内存区域与 Pack 支持状态；其设备数据库偏重调试与烧录，无法替代完整构建模型。

**CMSIS_5、CMSIS_6 与厂商公开 SDK。** 其中的 startup、system 文件与链接脚本适合作为回归材料与资产来源，应引用本地 Pack 或 SDK 文件。

**PlatformIO 与 Zephyr。** 前者的开发板与工具链描述可辅助设备识别与 OpenOCD 配置，但以开发板与框架为中心；后者覆盖面广但要求进入其构建体系。二者均只作设备信息参考。

**xPack 与 GNU MCU 工具。** 提供可重复安装的 Arm GNU 工具链与 OpenOCD，适合统一本地测试环境，不解决工程语义转换。

## 七、scatter 文件分级转换模型

解析 `.sct` 的加载域与执行域后，运行能力分析器判定可自动转换的程度。

```python
@dataclass
class ScatterCapabilities:
    static_layout: bool
    multiple_load_regions: bool
    multiple_copy_regions: bool
    multiple_zero_regions: bool
    ram_code: bool
    overlays: bool
    arm_library_regions: bool
    proprietary_symbols: bool
    dynamic_expressions: bool
```

四个判定等级：

**EXACT**：静态布局、结构简单，生成等价 GNU linker script 并自动启用。
**GENERATED_RUNTIME_REQUIRED**：需配套生成通用启动运行时代码完成多区数据拷贝与清零，方可与脚本一致。
**REVIEW_REQUIRED**：生成脚本，同时在报告中列出需人工确认的项，例如仅凭名称相似而语义未定的符号。
**UNSUPPORTED**：保留 SCT 分析结果，停止 GCC 链接脚本生成。

以下情况列入 UNSUPPORTED，不承诺等价转换：运行时由 overlay manager 动态装载的复杂 overlay；Arm linker 数据压缩或解压机制；依赖专有 veneer、区域表或运行库内部符号；加载区布局参与签名、校验、加密或升级协议；section 地址参与镜像头、校验和或烧录工具配置；bootloader 按私有格式重定位；TrustZone Secure 与 Non-Secure 镜像联动；多核镜像组合；scatter 文件经预处理或由构建命令动态生成；依赖 Arm linker 自动生成的特殊表；execution region 复杂重叠；`OVERLAY` 成员具有独立加载镜像与运行时切换逻辑。

生成的 `.ld` 须通过九项静态验证：所有 execution region 都有对应输出 section；所有 load region 都有对应 LMA；最大长度转换为 `MEMORY LENGTH` 或 `ASSERT()`；RW 区域具有加载地址；ZI 区域使用 `NOLOAD`；`+FIRST` 与 `+LAST` 顺序得到保留；SCT 中被引用的对象与 section 均进入生成脚本；startup 所需符号均由 `.ld` 提供；链接 map 中各 section 的 VMA 与 LMA 与 SCT 预期一致。

GNU ld 的 `OVERLAY` 只负责布局与装载符号生成，运行时选择、复制与生命周期管理仍由固件承担；原工程若依赖 Arm C Library 或厂商 overlay manager，语法转换无法保证行为一致。

## 八、中间模型数据结构定义

新建 `project_model.py`，定义以下数据类。字段一经进入 `main` 分支即视为契约，新增字段允许，改名或删除须在第十七节备注中登记原因。

```python
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class MemoryRegion:
    name: str            # FLASH、RAM、CCMRAM、SRAM2 等
    kind: str            # "rom" 或 "ram"
    origin: int
    length: int
    attributes: str      # 例如 "rx"、"xrw"
    source: str          # "uvprojx-cpu-desc" | "uvprojx-onchip" | "sct" | "pack" | "device-table"

@dataclass
class FileOptions:
    optimization: Optional[str]      # None 表示沿用目标级设置
    defines: List[str] = field(default_factory=list)
    undefines: List[str] = field(default_factory=list)
    include_paths: List[str] = field(default_factory=list)
    misc_flags: List[str] = field(default_factory=list)

@dataclass
class SourceFile:
    path: str
    language: str        # "c" | "cxx" | "asm-gnu" | "asm-armasm" | "object" | "library"
    include_in_build: bool
    group: str           # 所属 Keil Group 名称
    file_options: Optional[FileOptions]
    origin: str          # "project" | "rte" | "generated" | "staged"

@dataclass
class LibraryRef:
    path: str
    format: str          # "ar-elf" | "armlink-only" | "unknown"
    migratable: str      # "yes" | "trial" | "no"
    reason: str          # 判定依据说明

@dataclass
class AssetRecord:
    role: str            # "startup" | "system" | "linker-script" | "device-header"
    path: str
    origin: str          # "project" | "sdk" | "pack" | "generated"
    match_basis: str     # 匹配依据的文字说明
    confidence: str      # "exact" | "probable" | "guess"
    support_level: str   # "direct" | "review" | "manual"

@dataclass
class DeviceInfo:
    name: str
    vendor: str
    cpu: str             # 例如 "cortex-m4"
    fpu: str             # "none" | "fpv4-sp-d16" | "fpv5-sp-d16" | "fpv5-d16"
    float_abi: str       # "soft" | "hard"
    endian: str          # "little" | "big"
    memories: List[MemoryRegion] = field(default_factory=list)
    device_defines: List[str] = field(default_factory=list)

@dataclass
class Diagnostic:
    severity: str        # "info" | "warning" | "manual-required"
    category: str        # 资产角色或错误类别
    message: str

@dataclass
class ProjectModel:
    schema_version: int
    device: DeviceInfo
    target_name: str
    output_name: str
    sources: List[SourceFile]
    include_paths: List[str]
    global_defines: List[str]
    c_standard: str
    cpp_standard: str
    libraries: List[LibraryRef]
    assets: List[AssetRecord]
    linker_misc_flags: List[str]
    diagnostics: List[Diagnostic]
```

`KeilProjectToCMake` 增加 `build_project_model() -> ProjectModel` 方法，从既有成员导出模型。第一阶段仅导出，不改变生成行为；后续阶段令生成器逐步改为只消费模型。

## 九、Build Validator 规格与错误分类判定规则

### 9.1 行为规格

新建 `build_validator.py`，CLI 增加 `--validate` 参数。执行流程：

1. 工具探测：`shutil.which` 查找 `arm-none-eabi-gcc`、`cmake`、`ninja`。任一缺失时状态记为 `unverified`，报告列出缺失项，不抛异常。
2. 配置：`cmake -S <输出目录> -B <输出目录>/<build_dir> -G Ninja -DCMAKE_BUILD_TYPE=Debug`，日志存 `<输出目录>/validate/configure.log`。
3. 构建：`cmake --build <build_dir> --parallel`，日志存 `validate/build.log`。
4. 产物检查：`.elf` 存在且 `arm-none-eabi-size` 执行成功。
5. 分类与报告：按 9.2 规则分类，写出 `<输出目录>/build_report.json`。

进程退出码约定：0 表示转换成功且验证成功或未验证；1 表示转换失败；2 表示转换成功但验证失败。

### 9.2 错误分类判定规则

按下表顺序匹配，首个命中即为分类。首版四大类对外展示为：配置、头文件或源码、汇编、链接；子类保留在报告字段中。原始日志完整保留，禁止截断后丢弃。

| 顺序 | 类别 | 判定阶段 | 判定特征 |
| --- | --- | --- | --- |
| 1 | `TOOL_MISSING` | 前置 | `which` 未找到所需工具 |
| 2 | `CONFIG` | configure 退出非零 | 摘录含 `CMake Error` 的段落 |
| 3 | `HEADER_MISSING` | 构建 | 正则 `fatal error: .+\.h.*No such file` |
| 4 | `ASM` | 构建 | 失败文件扩展名为 `.s/.S/.asm`，或输出含 `Error: bad instruction`、`Error: unknown pseudo-op`、`Error: junk at end of line` |
| 5 | `SOURCE` | 构建 | 正则 `\.(c|cc|cpp|cxx):\d+(:\d+)?: (fatal )?error:` |
| 6 | `LINK` | 链接 | `undefined reference to`、`cannot find -l`、`will not fit in region`、`region .+ overflowed`、`multiple definition of` |
| 7 | `ARTIFACT` | 产物 | `.elf` 缺失或 `objcopy`、`size` 退出非零 |
| 8 | `UNKNOWN` | 兜底 | 无法归类，保留完整日志供人工分析 |

### 9.3 报告文件结构

```json
{
  "schema_version": 1,
  "status": "success | failed | unverified",
  "phase": "configure | compile | link | artifact | none",
  "error_class": "CONFIG | HEADER_MISSING | SOURCE | ASM | LINK | ARTIFACT | TOOL_MISSING | UNKNOWN | NONE",
  "first_error_excerpt": "首条错误及其前后各三行",
  "log_files": {"configure": "validate/configure.log", "build": "validate/build.log"},
  "toolchain": {"gcc_path": "", "gcc_version": "", "cmake_version": ""},
  "assets": [],
  "support_level": "direct | review | manual",
  "diagnostics": []
}
```

`assets` 为 `AssetRecord` 列表，`diagnostics` 为 `Diagnostic` 列表。`support_level` 取值含义：`direct` 免修改可编译；`review` 需人工确认标注项；`manual` 存在无法自动转换的部分。

## 十、各阶段执行规格

每阶段包含目标、任务、验收标准与测试要求。完成定义见第十六节。

### 阶段一：轻量中间模型

目标：建立稳定接口，解除后续模块对 `KeilProjectToCMake` 成员变量的依赖。

任务：新建 `project_model.py` 按第八节定义数据类；`KeilProjectToCMake` 增加 `build_project_model()`；生成逻辑本阶段不改动。

验收标准：既有测试全部通过；新增测试对合成样本断言 `device.cpu`、`memories`、源文件数量与 `include_in_build` 过滤结果；对同一样本工程，改造前后生成的 `CMakeLists.txt` 内容一致。

测试要求：`tests/test_project_model.py`，沿用既有测试的合成 `uvprojx` 构造方式与 `unittest` 框架。

### 阶段二：Build Validator

目标：建立自动编译验证与错误分类，作为后续所有改动的度量基线。

任务：按第九节实现 `build_validator.py` 与 `--validate` 参数；报告写出与退出码约定。

验收标准：真实工具链存在时，对最小可编译样本报 `success`；用测试内生成的假编译器脚本分别注入四类错误文本，分类结果正确；工具缺失时报 `unverified` 且不抛异常。

测试要求：`tests/test_build_validator.py`。分类函数接受日志文本输入，测试不依赖真实工具链；真实工具链存在时追加一条端到端用例。

### 阶段三：Asset Resolver

目标：启动文件与链接脚本按资产优先级序列成套选择，来源可追溯。

任务：新建 `asset_resolver.py`，归并 `_prepared_gcc_source_files()`、`_generate_gnu_startup_from_armasm()`、`_guess_gcc_linker_script()`、`_generate_fallback_gcc_linker_script()` 的选择逻辑。搜索顺序：工程目录内 gcc 资产（`gcc`、`GCC`、`gnu` 目录特征）、工程上级 SDK 目录、`RTEPATH` 下匹配 Pack、机械生成。产出 `AssetRecord` 写入 `CMakeLists.txt` 头注释与 `build_report.json`。实现启动文件与链接脚本的成套校验：扫描启动文件引用的符号与 `.ld` 定义的符号比对，缺失时产生 `manual-required` 诊断。

验收标准：含 SDK gcc 目录的合成样本选中 SDK 启动文件且 `origin` 为 `sdk`；无 SDK 样本回退机械生成且 `origin` 为 `generated`、`support_level` 为 `review`；符号成套校验对故意制造的符号缺失样本产生诊断而非静默。

测试要求：`tests/test_asset_resolver.py`，用临时目录构造带 `startup/gcc` 结构的合成 SDK。

### 阶段四：库解析与 ABI 检查

目标：工程引用的库进入模型并获得可迁移性判定。

任务：解析 `File` 节点中 `FileType` 为 3（object）与 4（library）的条目，以及 `LDads/Misc` 中的 `.lib`、`.a` 引用，填充 `LibraryRef`。格式判定：文件头为 `!<arch>` 且成员为 ELF 时 `format` 为 `ar-elf`、`migratable` 为 `trial`，交由验证器试链接；非 ar 归档或含 armlink 专有成员时 `armlink-only`、`no`，进入诊断。GCC 生成器把 `trial` 库以绝对路径加入 `target_link_libraries`，`no` 的库排除并记 `manual-required`。

验收标准：含 `FileType=4` 节点的样本 `libraries` 非空；构造的非 ar 假库判为 `no`；`CMakeLists.txt` 含 `trial` 库路径。

测试要求：`tests/test_library_parsing.py`，测试内构造最小 ar 归档与伪造文件头。

### 阶段五：文件级编译设置

目标：还原 Keil 单文件差异化编译。

任务：解析 `FileOption/FileArmAds/Cads/VariousControls` 的 `Define`、`IncludePath`、`MiscControls` 与 `Optim`，映射到 `FileOptions`；GCC 生成器输出 `set_source_files_properties` 设置 `COMPILE_DEFINITIONS`、`COMPILE_OPTIONS`、`INCLUDE_DIRECTORIES`。

验收标准：含文件级宏的样本，`compile_commands.json` 中对应条目出现该宏；无文件级设置的文件不受影响。

测试要求：扩展 `tests/test_keil_to_cmake.py` 的合成工程，增加带 `FileArmAds` 的文件节点。

### 阶段六：双前端

目标：CMSIS-Toolbox 存在时用 `uv2csolution` 输出交叉校验并补充器件与 Pack 信息。

任务：`shutil.which` 探测 `uv2csolution` 与 `csolution`；存在时执行转换并解析生成的 `*.csolution.yml` 与 `*.cproject.yml`，提取 device、pack、组件列表；与内置解析结果比对，差异写入 `diagnostics`；内置结果为主体，仅当内置缺失字段时采用增强值。YAML 解析使用 PyYAML，作为可选依赖，缺失时跳过该增强并记 `info` 诊断。

验收标准：无工具与无 PyYAML 环境下全部测试通过且增强分支被跳过；对预置的 yml 样本文本，差异比对逻辑正确记录不一致项。

测试要求：`tests/test_dual_frontend.py`，yml 解析与比对逻辑用静态样本文本测试，不要求安装 CMSIS-Toolbox。

### 阶段七：Pack 与器件数据

目标：以查表与 PDSC 解析替代散落的芯片硬编码分支。

任务：仓库内新增 `devices.json` 器件表（字段与 `DeviceInfo` 一致，另含 `openocd_target` 与 `jlink_device`）；实现 PDSC `<devices>` 段解析，提取 `Dcore`、`Dfpu`、`Dendian`、`memory`、`define`。查找顺序：器件表、Pack PDSC、现有推断逻辑。替换 `_infer_cpu_from_device()`、`_inferred_device_defines()`、`_default_openocd_target_script()` 中的分支为查表。

验收标准：器件表命中的器件产出正确的 `-mcpu`、`-mfpu`、器件宏与内存区域；未命中器件行为与改造前一致（样本快照比对）。

测试要求：`tests/test_device_data.py`，含器件表命中、PDSC 解析、未命中回退三组用例。

### 阶段八：SCT 分级转换

目标：利用 scatter 语义生成链接脚本，按第七节四级判定控制自动化程度。

任务：实现 `.sct` 解析器（检测预处理标记时置 `dynamic_expressions`）、`ScatterCapabilities` 能力分析、四级判定、前三级的 `.ld` 生成、九项静态验证器；`UNSUPPORTED` 时停止 GCC 链接脚本生成并输出分析报告；`GENERATED_RUNTIME_REQUIRED` 时生成多区拷贝与清零的启动运行时代码。

验收标准：单加载域单执行域样本判为 `EXACT` 且链接通过、map 中 VMA 与 LMA 与 SCT 一致；含 CCMRAM 的多执行域样本生成多 `MEMORY` 区；含 `OVERLAY` 的样本判为 `UNSUPPORTED` 且生成中止并有报告。

测试要求：`tests/test_scatter.py`，判定与生成逻辑用静态 `.sct` 文本测试；链接验证用例仅在真实工具链存在时执行。

### 阶段九：多厂商回归验证

目标：以编译成功率量化每次改动效果，守护既有能力。

任务：建立 `tests/samples/` 样本约定与 `tools/run_regression.py` 批量脚本，规范见第十一节；每阶段完成后更新 `docs/regression_baseline.md`。

验收标准：脚本对至少三类器件样本运行并产出报表；报表含成功率与错误类别分布；重复运行结果一致。

测试要求：脚本自身的参数解析与报表生成逻辑有单元测试；样本运行属于本地验证步骤。

## 十一、回归验证规范

2026-11-09 批量整理补充：本轮按批量 SDK 验证需求，先提供独立的
`tools/run_regression.py`，以 `tools/sdk_catalog.json` 登记版本、来源和筛选范围。
该入口支持原始 SDK 下载与多层归档校验、本地目录覆盖、全部 Keil 目标扫描、
并发构建、结果复用、失败分组及 ELF 结构检查。
样本和输出均保留在本地，官方 SDK 源码不随仓库分发。
实际用法与恢复规则见 `docs/batch_validation.md`，每次验收记录见
`docs/regression_baseline.md`。以下最小样本入库约定作为后续扩充规格保留。

样本目录约定：可入库的最小合成样本放在 `tests/samples/<vendor>_<series>_<name>/`，含 `uvprojx` 与必要源文件；体积或版权原因无法入库的本机真实工程，登记在 `tests/samples.local.json`，格式为 `[{"name": "", "uvprojx": "绝对路径", "target": "可选 TargetName"}]`，该文件加入 `.gitignore`。

运行方式：`python tools/run_regression.py --out _analysis_out/regression`。脚本对每个样本执行转换加验证，汇总 `build_report.json`。

指标定义：编译成功率等于 `status` 为 `success` 的样本数除以已验证样本总数；另统计各 `error_class` 的分布。每阶段完成后把带日期的结果表追加到 `docs/regression_baseline.md`，成功率下降视为该阶段未完成。

## 十二、工程约束与编码规范

1. 全部代码文件使用 UTF-8 无 BOM 编码；注释使用简体中文，描述意图与约束。
2. 测试框架沿用 `unittest`，测试命令为 `python -m unittest discover -s tests -v`。基线状态：2026-07-11 该命令通过 2 项测试。
3. 不引入必装外部依赖。PyYAML 等一律作为可选依赖，缺失时功能降级并写入诊断。
4. armclang 生成功能保持可用，每阶段以样本快照比对守护。
5. 每阶段结束时全部测试通过，仓库处于可用状态；允许破坏性重构，但不允许跨阶段的半成品状态遗留。
6. 生成文件的目录分隔符统一为 `/`，换行为 `\n`，沿用 `_write_text_file()` 的既有约定。
7. 新增模块文件放在仓库根目录，与 `keil_to_cmake.py`、`get_keil_mdk_info.py` 的现有组织方式一致；工具脚本放在 `tools/`。

## 十三、支持等级与产物标注

每次转换在 `build_report.json` 与 `CMakeLists.txt` 头注释中输出资产与支持等级清单，说明启动文件、system 文件、链接脚本、器件宏、库依赖各自的来源与可信等级，以及 scatter 转换所处等级。支持等级三档：`direct` 免修改可编译；`review` 需人工确认标注项；`manual` 存在需人工处理的部分并附指引。使用者在打开生成工程之前即可判断其可用程度。

## 十四、与现有代码的映射

| 改造方向 | 现有入口 | 现状 | 对应阶段 |
| --- | --- | --- | --- |
| 中间模型 | `parse()` 系列方法的成员变量产出 | 状态分散在实例成员 | 一 |
| 编译验证 | `_generate_configured_output()` | 写出即返回成功 | 二 |
| 启动文件成套选择 | `_prepared_gcc_source_files()`、`_generate_gnu_startup_from_armasm()` | 仅机械生成 STM32 与 AT32 | 三 |
| 链接脚本成套选择 | `_guess_gcc_linker_script()`、`_generate_fallback_gcc_linker_script()` | 查找 `.ld` 加两段内存回退 | 三 |
| 库解析 | `_parse_source_files()`、`_parse_linker_settings()` | 库列表始终为空 | 四 |
| 文件级选项 | `_is_file_included_in_build()` | 只读 `IncludeInBuild` | 五 |
| 前端增强 | `parse()` | 仅内置解析 | 六 |
| 器件宏与 CPU 推断 | `_inferred_device_defines()`、`_infer_stm32f1_stdperiph_define()`、`_infer_cpu_from_device()` | 覆盖少数系列 | 七 |
| OpenOCD 目标 | `_default_openocd_target_script()` | 少数系列硬编码 | 七 |
| scatter 语义 | `_parse_linker_settings()` 的 `ScatterFile` | GCC 分支未利用 | 八 |
| 回归度量 | 无 | 无 | 九 |

## 十五、边界与不承诺范围

预编译的 armlink 专用格式库、依赖 Arm C Library 私有机制的 scatter 布局、经预处理动态生成的链接脚本、TrustZone 与多核镜像，属于无法承诺自动等价转换的范围。对这些情形的处理方式是准确识别、进入诊断报告并给出人工处理指引，而非勉强产出无法编译或行为不一致的工程。

## 十六、执行须知（面向后续执行的 agent）

**完成定义。** 某阶段完成的充分必要条件：该阶段验收标准全部满足；既有与新增测试全部通过；第十七节状态表更新；阶段九就绪后回归报表同步更新且成功率未下降。

**执行顺序约束。** 阶段一至三为后续阶段的前置，完成前禁止进入阶段七、八。阶段四、五、六相互独立，可在阶段三完成后并行。

**禁止事项。** 禁止跳过验收标准声明完成；禁止捕获异常后静默继续；禁止删除或跳过既有测试；禁止在未运行验证器的情况下声明转换成功；禁止把机械生成资产标注为 `direct` 支持等级。

**未决事项登记。** 执行中遇到本规格未覆盖的决策，先在第十七节备注列登记决策内容与理由，再实施。当前已知未决事项：`uv2csolution` 对旧版 `uvproj` 的兼容程度待实测；`armlink-only` 库的精确判定特征待样本积累；J-Link 器件名映射表的数据来源待定。

**环境说明。** 开发环境为 Windows，shell 为 bash 语法；真实编译验证需本机安装 arm-none-eabi-gcc、CMake 与 Ninja，缺失时相关用例自动降级为跳过；回归样本中的本机真实工程需使用者在 `tests/samples.local.json` 中自行登记。

## 十七、阶段状态跟踪表

| 阶段 | 名称 | 状态 | 完成日期 | 备注 |
| --- | --- | --- | --- | --- |
| 一 | 轻量中间模型 | 未开始 | | |
| 二 | Build Validator | 未开始 | | |
| 三 | Asset Resolver | 未开始 | | |
| 四 | 库解析与 ABI 检查 | 未开始 | | |
| 五 | 文件级编译设置 | 未开始 | | |
| 六 | 双前端 | 未开始 | | |
| 七 | Pack 与器件数据 | 未开始 | | |
| 八 | SCT 分级转换 | 未开始 | | |
| 九 | 多厂商回归验证 | 部分完成 | | SDK 清单与批量命令完成；本轮验收见 regression_baseline.md；最小样本入库约定和持续构建集成待补充 |

2026-11-08 既有转换器兼容性修复范围：FreeRTOS 仅在同一内核目录存在配套的
GCC `port.c` 与 `portmacro.h` 时替换 RVDS 移植层，包含源文件的包装代码仅修改生成副本。
Keil `FileType=3` 中带 SYMDEFS 标识的文本输入复用现有符号转换功能，保留原表的固定地址。
导入符号需与实际配套固件地址和 ABI 一致；该轮仅完成符号导入，后续配套布局修复见下文。
本次修复和验证记录在 `docs/verified_keil_projects.md`，上表的架构阶段状态保持原值。

2026-11-08 SLIB 配套转换补充：依据 AT32F415 官方静态 SCT 分区生成 GNU 链接脚本，
保护代码采用 GCC pure-code 编译，运行库保留在 SCT 指定的独立 Flash 区域。
函数入口按原始符号表固定并由链接断言验证，构建后从实际 ELF 导出符号。
L0 与 L1 均保留并初始化算法专用 RAM；当前仅接受保护区零初始化状态，含初始化数据时链接报错。
新增 `slib_layout.py` 处理这一受限结构；未知 SCT 属性或分区组合明确报错。
这项修复依据当前任务授权实施，不代表通用 SCT 转换阶段完成。
Debug 和 Release 两组 L0、L1 完成独立完整构建及配套 ARM 指令模拟，
真实复位入口和 L1 调用指令验证通过，滤波结果达到官方阈值；151 项单元测试通过。
硬件保护寄存器和物理开发板尚未验收，详细证据见验证清单的 SLIB 配套章节。

2026-11-09 GD32 配套转换补充：新增按需加载的 `gd32_assets.py`，依据原始 ARM 启动
文件定位同一 SDK 的 GCC 启动文件，并比较完整中断向量；按器件型号、容量标识定位
`GCC/Ld` 中的配套脚本，仅允许工程实际引用的源码目录提供同型号专用脚本覆盖。
生成副本使用 Keil 目标的内存、栈及堆设置，保留源码中成对出现的 Arm 固定地址和
GCC 段属性，补充链接断言及有界堆符号。自定义 SCT、多区内存、缺失配套文件和
地址冲突明确失败。该实现面向官方 GD32 静态布局，不代表通用资产解析或 SCT 阶段完成。
GD32F30x V3.0.3、GD32E23x V2.5.0 的 27 个原始工程、31 个目标全部通过 GCC 构建，
全部 ELF 通过向量与内存检查，两个内核的实际启动代码通过指令模拟；外设行为尚待硬件测试。
