@echo off
setlocal
set "PYTHONUTF8=1"
"%~dp0.venv\Scripts\python.exe" -m log_tools.client --config "%~dp0config.json" %*
exit /b %errorlevel%
