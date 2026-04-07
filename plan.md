# 修复计划

## 问题总结

1. **"调试后端"概念混乱** — 当前UI将硬件调试器(J-Link/ST-Link/DAP-Link)和调试后端软件(JLinkGDBServer/OpenOCD/pyOCD)混为一谈
2. **launch.json为空** — `generate_vscode_launch`在2894行写入空配置，`generate_launch_json`的返回值被丢弃
3. **非J-Link调试器无tasks** — `generate_vscode_tasks`只生成JLinkGDBServer启动任务
4. **compile_commands.json无法实现代码跳转** — 未生成`c_cpp_properties.json`告知VSCode编译数据库位置
5. **Build任务不生成固件** — VSCode build task只运行`cmake --build`但从未运行configure步骤(`cmake -G ...`)

## 修复方案

### 1. 将UI拆分为"硬件调试器"和"调试后端"两个独立下拉框

**硬件调试器选项：**
- J-Link
- ST-Link
- DAP-Link

**调试后端选项（根据硬件调试器动态过滤）：**
- J-Link → JLinkGDBServer, OpenOCD
- ST-Link → OpenOCD, ST-Util
- DAP-Link → OpenOCD, pyOCD

修改GUI部分（约3700-3800行），将原`debugger_combo`拆分为`hw_debugger_combo`和`debug_backend_combo`，添加联动逻辑。

### 2. 修复launch.json生成

修改`generate_vscode_launch`函数（2887行）：
- 调用`generate_launch_json`获取配置内容
- 将其写入launch.json文件（而非写入空配置后丢弃返回值）
- 根据不同的调试后端生成对应的launch配置：
  - JLinkGDBServer: `"servertype": "jlink"`
  - OpenOCD: `"servertype": "openocd"` + 对应interface/target cfg
  - ST-Util: `"servertype": "stutil"`
  - pyOCD: `"servertype": "pyocd"`
- 使用cortex-debug扩展格式，确保在VSCode Run and Debug窗口可用

### 3. 为所有调试后端生成对应的VSCode tasks

修改`generate_vscode_tasks`函数（2753行）：
- JLinkGDBServer: 保持现有JLink GDB Server启动任务
- OpenOCD: 生成OpenOCD启动任务 (`openocd -f interface/xxx.cfg -f target/xxx.cfg`)
- ST-Util: 生成st-util启动任务
- pyOCD: 生成pyocd gdbserver启动任务

### 4. 生成c_cpp_properties.json实现代码跳转

在`.vscode/`目录下生成`c_cpp_properties.json`，配置：
- `compileCommands`指向`${workspaceFolder}/build/debug/compile_commands.json`
- 或直接将compile_commands.json复制到项目根目录

采用生成`c_cpp_properties.json`的方案更规范。

### 5. 修复Build任务使其能生成固件

修改VSCode build task，添加CMake configure步骤：
- 添加"CMake Configure"任务：`cmake -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Debug -S ${workspaceFolder} -B ${workspaceFolder}/build/debug`
- 修改"Build GCC Debug"任务的`dependsOn`为"CMake Configure"任务
- 或者将configure和build合并到一个任务中

## 涉及修改的函数

1. GUI部分（~3700-3800行）：拆分调试器选项
2. `generate_vscode_tasks`（~2753行）：生成所有调试后端的tasks
3. `generate_vscode_launch`（~2887行）：修复写入正确配置
4. `generate_launch_json`（~2900行）：适配不同调试后端
5. 新增：`generate_c_cpp_properties函数
6. `generate`函数（~2390行）：调用新函数，传递正确参数
