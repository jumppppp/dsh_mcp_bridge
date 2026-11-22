@echo off
rem Start the DSH MCP Bridge control panel: http://127.0.0.1:8765
rem Extra args are forwarded, e.g.:  start-panel.bat --port 9000 --http-port 9001
cd /d "%~dp0"
python main.py panel %*
echo.
echo Console exited. Press any key to close...
pause >nul
