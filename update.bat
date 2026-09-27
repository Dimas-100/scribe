@echo off
rem ===========================================================================
rem  Scribe update - brings this copy up to date. updates.py does the work:
rem  it quits Scribe, gets the new version (git pull for a git clone, the
rem  latest release otherwise), then runs setup.bat, which starts Scribe again.
rem  Your data is never touched: it lives in %APPDATA%\Scribe.
rem  (Settings -> About -> "Update now" does exactly the same.)
rem ===========================================================================
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Scribe isn't set up yet - double-click setup.bat first.
  pause
  exit /b 1
)
rem Everything happens on this one line: the update replaces this very file,
rem and cmd reads a batch file as it runs - so nothing may come after it.
venv\Scripts\python updates.py --install & exit /b
