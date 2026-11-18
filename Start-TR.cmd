@echo off
setlocal
cd /d "%~dp0"
py -3 -X faulthandler "%~dp0tr_gui.py"
set "TR_EXIT=%ERRORLEVEL%"
if "%TR_EXIT%"=="0" exit /b 0
echo.
echo TR stopped with exit code %TR_EXIT%.
pause
exit /b %TR_EXIT%
