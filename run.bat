@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 X 媒体下载器...
python main.py
if errorlevel 1 (
    echo.
    echo 启动失败，请确认已安装 Python 3.10+ 以及依赖：
    echo     python -m pip install requests websocket-client
    pause
)
