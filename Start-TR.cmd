@echo off
setlocal
cd /d "%~dp0"
py -3 "%~dp0tr_gui.py"
if errorlevel 1 pause
