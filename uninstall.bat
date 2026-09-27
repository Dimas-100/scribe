@echo off
rem ===========================================================================
rem  Scribe uninstall - removes Scribe from this PC (uninstall.py does the
rem  work and asks before deleting anything of yours). Afterwards, delete this
rem  folder to finish.
rem ===========================================================================
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Scribe was never set up here - just delete this folder.
  pause
  exit /b 0
)
venv\Scripts\python uninstall.py
rem Step out of the folder, so it can be deleted while this window is open.
cd /d "%TEMP%"
pause
