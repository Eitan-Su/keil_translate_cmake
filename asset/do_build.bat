@echo off
setlocal

if not defined ARMCLANG_PATH if defined KEIL_MDK_ROOT if exist "%KEIL_MDK_ROOT%\ARM\ARMCLANG\bin\armclang.exe" set "ARMCLANG_PATH=%KEIL_MDK_ROOT%\ARM\ARMCLANG\bin"
if not defined ARMCLANG_PATH if exist "%USERPROFILE%\AppData\Local\Keil_v5\ARM\ARMCLANG\bin\armclang.exe" set "ARMCLANG_PATH=%USERPROFILE%\AppData\Local\Keil_v5\ARM\ARMCLANG\bin"
if not defined ARMCLANG_PATH if exist "D:\install\keil5 mdk\ARM\ARMCLANG\bin\armclang.exe" set "ARMCLANG_PATH=D:\install\keil5 mdk\ARM\ARMCLANG\bin"

if not defined ARMCLANG_PATH (
echo Error: ARMCLANG_PATH was not found.
echo Set ARMCLANG_PATH or KEIL_MDK_ROOT before running this script.
exit /b 1
)

set "ARMCLANG_PATH=%ARMCLANG_PATH:\=/%"

echo "ARMCLANG_PATH: %ARMCLANG_PATH%"

REM Clean up previous build directory
del build /Q /S >NUL 2>&1

REM Configure the build with CMake
cmake -DCMAKE_BUILD_TYPE:STRING=Debug ^
-G Ninja -B build -S .

echo Compile in progress...

cmake --build build --clean-first

echo Done!
endlocal
