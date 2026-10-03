@echo off
rem Builds dist\GTA IV Map Exporter\GTA IV Map Exporter.exe and the Blender add-on zip.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 build_exe.py) else (python build_exe.py)
echo.
pause
