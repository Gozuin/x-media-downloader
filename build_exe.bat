@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 生成图标...
python build\make_icon.py || goto :err

echo [2/3] PyInstaller 打包（单文件、无控制台窗口）...
python -m PyInstaller --noconfirm --clean ^
  --onefile --windowed ^
  --name "X媒体下载器" ^
  --icon "build\icon.ico" ^
  --version-file "build\version_info.txt" ^
  --distpath "dist" --workpath "build\work" --specpath "build" ^
  --exclude-module pytest --exclude-module numpy --exclude-module pandas ^
  main.py || goto :err

echo [3/3] 完成：
dir /b dist
pause
exit /b 0

:err
echo 打包失败，请查看上面的输出。
pause
exit /b 1
