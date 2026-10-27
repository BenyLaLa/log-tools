@echo off
"%~dp0.venv\Scripts\python.exe" -m log_tools --config "%~dp0config.json" %*
