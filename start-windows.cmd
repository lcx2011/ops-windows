@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 goto run-with-py
)

where python >nul 2>nul
if errorlevel 1 (
  goto missing-python
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto old-python
python server.py --launcher
exit /b %errorlevel%

:run-with-py
py -3 server.py --launcher
exit /b %errorlevel%

:missing-python
echo Error: Python 3.9 or newer was not found. Install Python from https://www.python.org/downloads/windows/
exit /b 127

:old-python
echo Error: Python 3.9 or newer is required. Upgrade Python from https://www.python.org/downloads/windows/
exit /b 126
