@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] 检查 PyInstaller...
python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo       未安装，正在安装...
  python -m pip install pyinstaller || goto fail
)

echo [2/2] 打包 ms-bv-viewer.exe ...
python -m PyInstaller --onefile --noconfirm --clean ^
  --name ms-bv-viewer ^
  --add-data "%~dp0app.html;." ^
  --distpath dist --workpath build --specpath build ^
  server.py || goto fail

echo.
echo   完成：dist\ms-bv-viewer.exe
goto end

:fail
echo.
echo   打包失败，看上面的报错。
:end
pause
