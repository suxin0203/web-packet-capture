@echo off
echo ========================================
echo 打包网页版抓包工具为独立EXE
echo ========================================
echo.

echo [1/3] 检查 PyInstaller...
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在安装 PyInstaller...
    pip install pyinstaller
) else (
    echo PyInstaller 已安装
)
echo.

echo [2/3] 清理旧文件...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo.

echo [3/3] 开始打包（约3-5分钟）...
echo.

pyinstaller --onefile ^
    --name "抓包工具网页版" ^
    --add-data "web;web" ^
    --add-data "capture_addon.py;." ^
    --add-data "config.ini;." ^
    --hidden-import mitmproxy ^
    --hidden-import mitmproxy.tools.dump ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops ^
    --hidden-import uvicorn.loops.asyncio ^
    --hidden-import uvicorn.protocols ^
    --hidden-import uvicorn.protocols.http ^
    --hidden-import uvicorn.protocols.http.h11_impl ^
    --hidden-import uvicorn.protocols.websockets ^
    --hidden-import uvicorn.protocols.websockets.websockets_impl ^
    --hidden-import uvicorn.lifespan ^
    --hidden-import uvicorn.lifespan.on ^
    --exclude-module tkinter ^
    --exclude-module _tkinter ^
    --exclude-module test ^
    --exclude-module unittest ^
    --exclude-module pydoc_data ^
    --exclude-module lib2to3 ^
    --exclude-module setuptools ^
    --exclude-module pkg_resources ^
    --exclude-module numpy ^
    --exclude-module PIL ^
    --exclude-module pandas ^
    --exclude-module scipy ^
    --exclude-module matplotlib ^
    --console ^
    server.py

if %errorlevel% equ 0 (
    copy /y config.ini dist\config.ini >nul
    copy /y docs\操作手册.txt dist\操作手册.txt >nul
    echo.
    echo ========================================
    echo 打包成功
    echo ========================================
    echo 分发文件（dist 目录）:
    echo   抓包工具网页版.exe
    echo   config.ini
    echo   操作手册.txt
    echo.
) else (
    echo.
    echo 打包失败，请检查上方报错信息
)
pause
