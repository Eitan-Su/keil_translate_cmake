# SDK 批量验证入口

`run_regression.py` 使用 `sdk_catalog.json` 获取和扫描 SDK，逐目标调用原有转换器及 GCC 构建。
当前登记 GD32F30x V3.0.3、GD32E23x V2.5.0 和 AT32F415 V2.1.8。

```powershell
python -B tools/run_regression.py run --sdk gd32e23x --seven-zip C:\Tools\7zr.exe
```

支持 `fetch`、`discover`、`run`、`report` 四种命令。重复运行同一批次可复用有效结果，
`--retry-failed` 重试失败目标，`--force` 重新构建全部所选目标。
新增 SDK 使用 JSON 登记版本、下载地址、SHA-256 和工程筛选规则。

完整参数、清单格式及使用范围见 [SDK 批量转换验证](../docs/batch_validation.md)，
验收记录见 [回归基线](../docs/regression_baseline.md)。
