@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -X utf8 -m tgearn store
) else (
    py -3 -X utf8 -m tgearn store
)
pause
