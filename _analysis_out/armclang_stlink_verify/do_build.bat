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

cmake -S . -B "build" -G "Ninja" -DCMAKE_BUILD_TYPE=Debug
if errorlevel 1 exit /b %errorlevel%
cmake --build "build" --parallel
endlocal
