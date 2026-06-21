@echo off
chcp 65001 >nul
title 照片归类工具

:: 添加 photo-tools 到 PATH（供 ffprobe / exiftool 使用）
set "TOOLS_DIR=%USERPROFILE%\photo-tools\bin"
if exist "%TOOLS_DIR%" (
    set "PATH=%TOOLS_DIR%;%PATH%"
)

echo 正在启动照片归类工具...
echo.
python3 "%~dp0photo_organizer_gui.py"
if errorlevel 1 (
    echo.
    echo ⚠ 启动失败，尝试 python 命令...
    python "%~dp0photo_organizer_gui.py"
)
pause
