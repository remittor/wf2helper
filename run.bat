rem @echo off
chcp 866 >NUL
setlocal

set PYTHONUNBUFFERED=TRUE
set SCRIPT_DIR=%~dp0

if not exist "%SCRIPT_DIR%run.py" (
    echo ERROR: run.py not found in "%SCRIPT_DIR%"
    exit /b 1
)

cd /D "%SCRIPT_DIR%"

if exist "%SCRIPT_DIR%python311\python.exe" goto runembed

"%SCRIPT_DIR%run.py" %*
goto :EOF

:runembed
"%SCRIPT_DIR%python311\python.exe" "%SCRIPT_DIR%run.py" %*
