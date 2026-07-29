@echo off
setlocal
cd /d "%~dp0"

if not exist "main.py" (
  echo ERROR: main.py is missing from this folder.
  echo Extract the ZIP again so all files are in C:\HGSYSTEM.
  pause
  exit /b 1
)

if not exist "HFPLUME.BAT" (
  echo ERROR: HFPLUME.BAT is missing from this folder.
  echo Keep this launcher in the HGSYSTEM root folder.
  pause
  exit /b 1
)

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=py"
  goto :python_ready
)

where python >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=python"
  goto :python_ready
)

echo.
echo Python 3.11 or 3.12 is not installed or not available in PATH.
echo Install Python, check "Add python.exe to PATH", and run this file again.
echo.
pause
exit /b 1

:python_ready
if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment. This happens only once.
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 goto :install_error
  call ".venv\Scripts\activate.bat"
  python -m pip install --upgrade pip
  if errorlevel 1 goto :install_error
  python -m pip install -r requirements.txt
  if errorlevel 1 goto :install_error
) else (
  call ".venv\Scripts\activate.bat"
)

set "HGSYSTEM=%~dp0"
start "" http://127.0.0.1:8501
python -m streamlit run main.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
exit /b 0

:install_error
echo.
echo ERROR: Installation failed. Check the Internet connection and Python installation.
echo.
pause
exit /b 1
