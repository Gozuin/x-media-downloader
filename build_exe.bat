@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先用项目自带的打包虚拟环境（build\venv312），没有就用 PATH 里的 python
set "PY=python"
if exist "build\venv312\Scripts\python.exe" set "PY=build\venv312\Scripts\python.exe"

echo [1/2] 生成图标（需要 Pillow；build\icon.ico 已存在就跳过）...
if exist "build\icon.ico" (
  echo       build\icon.ico 已存在，跳过
) else (
  %PY% build\make_icon.py || goto :err
)

echo [2/2] PyInstaller 打包（配置：build\X媒体下载器.spec）...
%PY% -m PyInstaller --noconfirm --clean --distpath "dist" --workpath "build\work" "build\X媒体下载器.spec" || goto :err

echo.
echo 打包完成，dist\ 下产物：
dir /b dist
echo.
echo 想再生成发布 zip，接着跑：  %PY% build\package_release.py
pause
exit /b 0

:err
echo.
echo 打包失败，请看上面的输出。
pause
exit /b 1
