@echo off
setlocal

rem ============================================================
rem  DeepSeek Code launcher (dccode)
rem  Uses .venv and dccode.py in the same dir as this .bat
rem  Current working dir is sent to server as workdir
rem ============================================================

rem --- resolve script dir ---
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "VENV_PY=%SCRIPT_DIR%\.venv\Scripts\python.exe"
set "MAIN_PY=%SCRIPT_DIR%\dccode.py"

rem --- prefer venv python, fallback to system python ---
if exist "%VENV_PY%" (
    set "PY=%VENV_PY%"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [dccode] python not found. install Python 3.10+ or create .venv
        pause
        exit /b 1
    )
    set "PY=python"
)

rem --- main script check ---
if not exist "%MAIN_PY%" (
    echo [dccode] main script not found: %MAIN_PY%
    pause
    exit /b 1
)

rem --- auto install deps on first run ---
set "DEPS_FLAG=%SCRIPT_DIR%\.deps_installed"
if not exist "%DEPS_FLAG%" (
    echo [dccode] first run, installing deps...
    "%VENV_PY%" -m pip install -r "%SCRIPT_DIR%\requirements.txt" -i https://pypi.org/simple/ --quiet
    if errorlevel 1 (
        echo [dccode] deps install failed. check network or run pip install manually
        pause
        exit /b 1
    )
    echo done > "%DEPS_FLAG%"
)

rem --- launch client (cwd = workdir) ---
"%PY%" "%MAIN_PY%" %*
endlocal
