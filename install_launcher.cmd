@echo off
rem Double-click to add PartsBin to the Start menu. Pass -Desktop for a Desktop shortcut too,
rem or -Uninstall to remove them.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_launcher.ps1" %*
pause
