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
  echo Error: Python 3.9 or newer was not found.
  exit /b 127
)
python -m PyInstaller --noconfirm --clean desktop-app.spec
exit /b %errorlevel%

:run-with-py
py -3 -m PyInstaller --noconfirm --clean desktop-app.spec
exit /b %errorlevel%
