@echo off
REM ============================================================
REM  Start_JARVIS.bat
REM  Double-click this file to launch JARVIS.
REM
REM  What it does, every time you run it:
REM    1. Finds a Python interpreter (py launcher, then python.exe).
REM    2. Creates a private virtual environment inside this folder
REM       (jarvis\.venv) the FIRST time only -- no admin rights
REM       needed, nothing is installed system-wide, safe on Citrix.
REM    3. Installs requirements.txt into that venv the FIRST time
REM       only (marker file .venv\.deps_installed skips this on
REM       every later run, so startup after the first time is fast
REM       and works even with restricted/offline network policies).
REM    4. Launches the Streamlit UI, which opens your default
REM       browser automatically at http://localhost:8501
REM
REM  If step 3 fails because this machine has no internet access,
REM  see the "OFFLINE INSTALL" note at the bottom of this file.
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   JARVIS - Engineering Knowledge Assistant
echo ============================================
echo.

REM --- 1. Locate a Python interpreter --------------------------
set "PYTHON_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYTHON_CMD=python"
    )
)

if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python was not found on this machine.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         ^(or ask IT to provide an offline Python installer^) and re-run this file.
    pause
    exit /b 1
)

echo [OK] Found Python: 
%PYTHON_CMD% --version
echo.

REM --- 2. Create the virtual environment if it doesn't exist ---
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating a private environment in .venv ...
    %PYTHON_CMD% -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Environment created.
    echo.
)

set "VENV_PY=.venv\Scripts\python.exe"

REM --- 3. Install dependencies (first run only) -----------------
if not exist ".venv\.deps_installed" (
    echo [SETUP] Installing required packages ^(first run only, this may take a minute^)...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    "%VENV_PY%" -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo.
        echo [ERROR] Package installation failed.
        echo         If this machine has no internet access, see the
        echo         OFFLINE INSTALL note near the bottom of Start_JARVIS.bat
        echo         for how to install from a local wheel cache instead.
        pause
        exit /b 1
    )
    echo installed> ".venv\.deps_installed"
    echo [OK] Packages installed.
    echo.
) else (
    echo [OK] Packages already installed, skipping setup.
    echo.
)

REM --- 4. Launch the app -----------------------------------------
echo [START] Launching JARVIS in your browser...
echo         ^(this console window must stay open while JARVIS is running -
echo          closing it will shut the app down^)
echo.
"%VENV_PY%" -m streamlit run "ui\app.py" --server.headless false

endlocal

REM ============================================================
REM  OFFLINE INSTALL (no internet access on this machine)
REM  ------------------------------------------------------------
REM  Ask IT/another machine with internet to run, using the SAME
REM  Python version as this machine:
REM      pip download -r requirements.txt -d jarvis_offline_packages
REM  Copy the jarvis_offline_packages folder onto this machine, next
REM  to this .bat file, then replace step 3 above's install command
REM  (temporarily edit this file) with:
REM      "%VENV_PY%" -m pip install --no-index --find-links=jarvis_offline_packages -r requirements.txt
REM  Run once, then this file works exactly as before on every
REM  future launch.
REM ============================================================
