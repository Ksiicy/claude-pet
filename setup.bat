@echo off
chcp 65001 >nul
rem 第一次使用：建虚拟环境并安装依赖，装好后直接启动桌宠
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist ".venv\Scripts\python.exe" (
    echo 正在创建虚拟环境 .venv ...
    %PY% -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
echo.
echo 安装完成，以后双击 run.bat 即可启动。
call run.bat
exit /b 0
:fail
echo.
echo 安装失败：请先安装 Python 3.10 或更新版本（https://www.python.org/downloads/），并勾选 "Add python.exe to PATH"。
pause
exit /b 1
