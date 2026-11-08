# SDK 批量转换验证

`tools/run_regression.py` 将 SDK 获取、工程扫描、转换、构建和错误汇总组织为同一套命令。
新增 SDK 通常只需增加 JSON 登记项；转换器的兼容规则仍需根据实际失败原因补充。
每项修复可以用整个样本集合重新验证，以检查是否影响原有工程。

## 环境与首次执行

使用 Python 3.11 或更新版本、CMake、GNU Arm Embedded GCC，以及 Ninja 或 Make。
Python 模块全部使用标准库，无新增必装依赖。7z 文件需要单独安装 7-Zip，或者通过
`--seven-zip` 指定现有的 `7z`、`7za`、`7zr` 程序。ZIP 和 tar 使用 Python 内置解包器。
当前真实构建验收环境为 Windows；其他宿主环境仍需实际验证。

所有相对输出目录均相对于本仓库根目录。输出目录和缓存目录应为空目录，或由本工具
创建并持有 `.sdk-batch.json` 标记；现有的无关文件目录会被拒绝。
默认输出为 `.cache/sdk-regression`，缓存为 `.cache/sdk-cache`，两者分别保存。
原始 SDK 可以位于仓库外，作为只读输入；输出与 SDK 根目录须相互独立。

```powershell
python -B tools/run_regression.py discover --sdk gd32e23x --seven-zip C:\Tools\7zr.exe
python -B tools/run_regression.py run --sdk gd32e23x --seven-zip C:\Tools\7zr.exe
python -B tools/run_regression.py report
```

第一次执行会下载清单指定的版本。后续执行复用经过校验的下载文件和解包目录。
`fetch` 完成 SDK 获取及工程扫描；`discover` 同样获取缺失 SDK，并输出目标清单。
两者均不会执行编译。省略子命令时执行 `run`。

本地 SDK 可以覆盖清单中的下载源：

```powershell
python -B tools/run_regression.py run --sdk at32f415 `
  --local-sdk "at32f415=D:\SDK\AT32F415_Firmware_Library_V2.1.8" `
  --generator "MinGW Makefiles" --make-program D:\Tools\mingw\bin\make.exe
```

清单中的 AT32F415 还支持环境变量 `AT32F415_SDK`。省略 `--sdk` 会选择清单中的全部
SDK，因此需要同时提供 AT32 本地输入。版本字段是来源登记信息；本地覆盖时应提供对应
版本，工具通过内容 SHA-256 识别变化，无法仅凭目录内容推断官方版本。

## 登记 SDK

默认清单为 `tools/sdk_catalog.json`，可用 `--catalog` 指定另一份 JSON 文件。
归档来源必须包含 HTTPS 下载地址、明确版本、SHA-256 和归档格式：

```json
{
  "schema_version": 1,
  "sdks": [{
    "id": "vendor-series",
    "version": "1.0.0",
    "source": {
      "type": "archive",
      "url": "https://example.invalid/SDK-1.0.0.zip",
      "sha256": "填写从官方发布资料核对的64位SHA256",
      "format": "zip",
      "root": "SDK-1.0.0"
    },
    "include": ["**/*.uvprojx", "**/*.uvproj"],
    "exclude": ["**/unsupported-example/**"],
    "targets": ["*"]
  }]
}
```

这份示例需要替换 URL 和校验值后使用。工具支持 `zip`、`tar`、`7z`。
外层压缩包包含内层压缩包时，在 `source.layers` 中按解包顺序登记
`member`、`sha256`、`format`，具体格式见仓库 GD32 清单。
每层 `member` 相对于上一层解包目录，`source.root` 相对于最后一层解包目录。

本地来源使用 `{"type":"local","root":"SDK目录"}` 或
`{"type":"local","root_env":"环境变量名"}`。清单中的相对本地目录相对于清单文件。
目录下的原始 `.uvprojx` 和 `.uvproj` 均会扫描，每个 `TargetName` 独立构建。
省略过滤字段表示全部工程和目标；筛选模式区分大小写，统一使用 `/`，
`*` 可以匹配目录分隔符，`**/` 也允许匹配根目录中的文件。

默认 GD32F30x 清单选择全部 MDK5 工程和一个 MDK4 模板，与既有验证集合一致，
共 26 个工程、30 个目标。完整 SDK 含 50 个工程、54 个目标；需要扩大范围时调整
`include`。GD32E23x 包含一个 Keil 模板；AT32F415 全量扫描得到 132 个工程、134 个目标。

## 批次选择与恢复

| 参数 | 用途 |
| --- | --- |
| `--sdk ID` | 限定 SDK，允许重复 |
| `--project GLOB` | 按 SDK 内工程文件名筛选，多个模式取并集 |
| `--limit N` | 仅运行排序后的前 N 个目标，用于初步检查 |
| `--jobs N` | 同时构建的目标数，默认 2 |
| `--build-jobs N` | 每个目标的编译并发数，默认 2 |
| `--timeout N` | 每条转换、配置、构建及 size 命令的超时秒数，默认 300 |
| `--configuration Debug` | 构建配置，可选择 Release |
| `--retry-failed` | 重试失败及待人工核查的目标，复用有效成功结果 |
| `--force` | 在新目录中重新构建全部所选目标 |
| `--out DIRECTORY` | 独立批次目录，必须位于仓库内 |
| `--cache DIRECTORY` | SDK 下载及解包缓存，必须位于仓库内 |

通常编译进程上限接近 `jobs × build-jobs`。默认值控制内存占用；较大的工程集合宜先
调整目标并发数，依据内存和磁盘负载再增加编译并发数。

重复运行相同命令会检查 SDK 内容、转换器模块、工具程序、GCC 运行库及头文件、
构建选项、相关环境变量、Python 版本和外部依赖；输入一致且日志、生成文件和固件
校验通过时，复用结果。SDK 内任一文件改变会使该 SDK 的全部所选目标重新构建，
这是为了覆盖共享头文件和间接依赖。校验本身仍会读取输入文件，因此恢复运行具有扫描成本。

已完成的普通构建失败也可以复用；转换失败导致外部依赖无法完整收集、超时、输入完整性
失败和未完成目标会自动重试。`--retry-failed` 适合修复环境后再次检查。
`Ctrl+C` 终止正在运行的构建进程树，保留各次尝试和日志，随后使用同一命令恢复。
下载中断保留 `.part` 证据文件，下次重新下载该文件；当前没有 HTTP 分段续传。

缓存内容损坏会明确报错并保留现场。选择新的 `--cache` 目录可重新获取 SDK。
自动清理和覆盖损坏缓存未提供，以保护现有诊断材料。

## 输出与状态

`plan.json` 记录本次选中的 SDK、工程、目标、器件与内存信息；`context.json` 记录
工具版本及文件校验值。`inputs/` 保存 SDK 内容清单。每个目标在
`runs/<尝试标识>/` 下保存生成工程、构建目录、各阶段日志及 `result.json`。
`results/` 保存该目标最近一次结果，旧尝试继续保留。
缓存和构建使用短目录名称，以减少 Windows GCC 的文件名长度问题；完整 SHA-256
和目标标识保存在 JSON 中。仓库根目录或自定义输出目录过长时，仍应优先选用仓库内
较短的 `--out` 和 `--cache` 目录。

`summary.json` 和 `summary.md` 汇总成功率、已完成数、复用数、完整通过及部分通过
的工程数，并按错误类别和规范化诊断分组。常见分组包括缺少头文件、缺少符号、汇编
语法、ABI 差异、重复定义、内存溢出和不支持的布局。同组错误提供代表日志和目标标识，
用于优先分析出现频率较高的兼容问题；分组相同仍需结合原始日志判断实际原因。

| 状态 | 含义 |
| --- | --- |
| `success` | 转换、配置、全部编译、最终链接、ELF、HEX、BIN 和结构检查均通过 |
| `review_required` | 构建和产物生成完成，但缺少向量或完整内存信息，无法完成结构判定 |
| `failed` | 某个执行阶段或完整性检查失败，日志及已有产物保留 |

结构检查限定为 Cortex-M ELF32 小端固件，检查装载区域、Thumb 入口、向量表复位入口、
初始栈对齐及范围。自定义启动地址和多镜像工程可能需要人工核查与后续规则扩展。
构建通过及结构检查通过均不能替代开发板上的启动和外设测试。

`integrity.json` 记录构建前后 SDK、外部依赖和转换器内容是否一致。生成输入也逐目标
检查，CMake 导出的 `compile_commands.json` 作为允许新增的文件。
退出码 `0` 表示所有所选目标通过且 SDK 准备无错误，`2` 表示失败、待核查或未完成，
`130` 表示中断。`fetch` 与 `discover` 的 `0` 只代表获取及扫描成功。

`report` 重新汇总保存的当前批次，忽略属于其他输入版本的结果；该命令展示历史构建
记录。使用 `run` 才会重新检查当前输入、工具环境和固件，并决定是否复用结果。

## 使用范围

批量工具减少 SDK 获取、重复构建和逐份日志分析的操作量。厂商专用编译语法、启动流程、
链接布局和预编译库的适配仍属于转换器功能。新增 SDK 应先少量选择代表工程，修复共同
原因后运行全量集合，并把真实通过的工程同步登记到验证清单。
完整验收记录见 [回归基线](regression_baseline.md)。
