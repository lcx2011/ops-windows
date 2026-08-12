@echo off
setlocal
cd /d "%~dp0"

where pyw >nul 2>nul
if not errorlevel 1 (
  pyw -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 goto run-with-pyw
)

where pythonw >nul 2>nul
if errorlevel 1 goto missing-python
pythonw -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto old-python
pythonw -m desktop_app
exit /b %errorlevel%

:run-with-pyw
pyw -3 -m desktop_app
exit /b %errorlevel%

:missing-python
echo Error: Python 3.9 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
pause
exit /b 127

:old-python
echo Error: Python 3.9 or newer is required.
pause
exit /b 126
