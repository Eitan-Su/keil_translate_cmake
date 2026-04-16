@echo off
setlocal

cmake -S . -B "build-gcc" -G "Ninja" -DCMAKE_BUILD_TYPE=Debug
if errorlevel 1 exit /b %errorlevel%
cmake --build "build-gcc" --parallel
endlocal
