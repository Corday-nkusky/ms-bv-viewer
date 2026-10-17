@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto havepy
where python >nul 2>nul
if not errorlevel 1 goto havepython
echo.
echo   [!] Python 3 not found. Install it from python.org and retry.
echo.
pause
exit /b 1

:havepy
py -3 server.py %*
goto done

:havepython
python server.py %*
goto done

:done
echo.
echo   Server stopped.
pause
