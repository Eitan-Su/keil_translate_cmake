@echo off
setlocal
"D:/singlechip/gcc+gdb+openocd/tools/arm-gnu-toolchain-13.3.rel1-ming/bin/arm-none-eabi-objcopy" -O ihex "Project.elf" "Project.hex"
if errorlevel 1 exit /b %errorlevel%
"D:/singlechip/gcc+gdb+openocd/tools/arm-gnu-toolchain-13.3.rel1-ming/bin/arm-none-eabi-objcopy" -O binary "Project.elf" "Project.bin"
if errorlevel 1 exit /b %errorlevel%
"D:/singlechip/gcc+gdb+openocd/tools/arm-gnu-toolchain-13.3.rel1-ming/bin/arm-none-eabi-size" --format=berkeley "Project.elf"
if errorlevel 1 exit /b %errorlevel%
"C:/Program Files/CMake/bin/cmake.exe" -E touch "Project.artifacts.stamp"
if errorlevel 1 exit /b %errorlevel%
if exist "compile_commands.json" "C:/Program Files/CMake/bin/cmake.exe" -E copy_if_different "compile_commands.json" "../compile_commands.json"
if errorlevel 1 exit /b %errorlevel%

