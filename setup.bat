@echo off
setlocal
rem ===========================================================================
rem  Scribe setup - double-click it once after downloading (or cloning), and
rem  it does everything:
rem    1. Python: uses a 64-bit Python 3.11-3.13 if you have one; if you
rem       don't, it downloads a private copy just for Scribe (into .tools\ -
rem       nothing is installed on the rest of your PC). Then it creates
rem       Scribe's own environment in venv\
rem    2. installs Scribe's dependencies (a few minutes the first time)
rem    3. checks Windows has Microsoft's Visual C++ runtime (the speech
rem       libraries need it) and installs it if it's missing
rem    4. adds Scribe to the Start menu
rem    5. starts Scribe - its welcome opens on the first run
rem  Updates run it too (update.bat). Your data is never touched: it lives in
rem  %APPDATA%\Scribe, not in this folder.
rem ===========================================================================
cd /d "%~dp0"

rem Windows can "open" setup.bat from inside the ZIP without extracting it:
rem it then runs from a temporary copy that disappears - Scribe would break.
set "HERE=%~dp0"
if not "%HERE:.zip\=%"=="%HERE%" goto :inside_zip

echo.
echo  Setting up Scribe. This takes a few minutes the first time.
echo.

rem OneDrive copies every file in its folder to the cloud - thousands of
rem them for Scribe's environment - and can lock files mid-install.
set "INONEDRIVE="
if defined OneDrive call :check_onedrive "%OneDrive%"
if defined OneDriveConsumer call :check_onedrive "%OneDriveConsumer%"
if defined OneDriveCommercial call :check_onedrive "%OneDriveCommercial%"
if defined INONEDRIVE (
  echo  Note: this Scribe folder is inside OneDrive, which copies every file to
  echo  the cloud and can lock them while Scribe installs or updates. Scribe works
  echo  best in a folder outside OneDrive, such as C:\Scribe - move the folder
  echo  there and run setup.bat again. Or press a key to continue here anyway.
  pause >nul
  echo.
)

rem One dependency (onnxruntime) has very deep file paths; under a long
rem folder path its install fails unless Windows long paths are enabled.
if not "%HERE:~100,1%"=="" (
  echo  Note: this folder's path is long. If the install fails deep inside
  echo  "onnxruntime", move the Scribe folder somewhere short, such as
  echo  C:\Scribe, and run setup.bat again.
  echo.
)

rem ---- 1. Python -----------------------------------------------------------
echo  [1/5] Python...
rem An environment that no longer runs (the folder was moved, or its Python
rem was uninstalled) is rebuilt: it holds nothing of yours.
if exist "venv\Scripts\python.exe" (
  venv\Scripts\python -c "import sys" >nul 2>&1 && goto :have_venv
  echo        Scribe's environment doesn't run any more - rebuilding it.
  rmdir /s /q venv
)
rem A 64-bit Python for regular (x64) Windows: the speech libraries come in
rem no other build.
set "PY="
for %%V in (3.11 3.12 3.13) do (
  if not defined PY (
    py -%%V -c "import sys, platform; sys.exit(0 if sys.maxsize > 2**32 and platform.machine() in ('AMD64', 'x86_64') else 1)" >nul 2>&1 && set "PY=py -%%V"
  )
)
if defined PY (
  echo        Using %PY%.
  %PY% -m venv venv || goto :failed
  goto :have_venv
)
call :private_python || goto :failed

:have_venv
call :wait_for_quit

rem ---- 2. Dependencies ------------------------------------------------------
echo  [2/5] Installing Scribe's dependencies...
venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt || goto :failed

rem ---- 3. Microsoft's Visual C++ runtime -----------------------------------
echo  [3/5] Checking Windows components...
call :check_runtime || goto :failed

rem ---- 4. Start menu ------------------------------------------------------------
echo  [4/5] Adding Scribe to the Start menu...
venv\Scripts\python install_shortcut.py || goto :failed

rem ---- 5. Start ---------------------------------------------------------------
echo  [5/5] Starting Scribe...
start "" "venv\Scripts\pythonw.exe" "%~dp0app.py" --show
echo.
echo  Done. Scribe is starting - on the first run its welcome window opens
echo  in a moment.
timeout /t 4 >nul 2>&1
exit /b 0


rem ===========================================================================
:private_python
rem No 64-bit Python 3.11-3.13 on this PC: download uv (a small, well-known
rem Python installer from Astral - checked against its published SHA-256) and
rem let it fetch Python 3.11 into .tools\ - a copy only Scribe uses. Nothing
rem is added to PATH or to Windows' app list.
echo        No suitable Python on this PC - downloading a private copy for
echo        Scribe (about 40 MB, once).
set "SCRIBE_TOOLS=%~dp0.tools"
if not exist "%SCRIBE_TOOLS%" mkdir "%SCRIBE_TOOLS%"
if not exist "%SCRIBE_TOOLS%\uv\uv.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; $zip = Join-Path $env:SCRIBE_TOOLS 'uv.zip'; Invoke-WebRequest -UseBasicParsing 'https://github.com/astral-sh/uv/releases/download/0.12.19/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip; if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne '6DBB02D79E419522F1C500F0ADB1CDDCFF0CDA7D59B0D66EA7F5E3B4A1B2F5F0') { Remove-Item $zip; throw 'The download did not match its checksum.' }; Expand-Archive -Force $zip (Join-Path $env:SCRIBE_TOOLS 'uv'); Remove-Item $zip"
  if errorlevel 1 (
    echo  Couldn't download Python's installer. Check your internet connection,
    echo  or install Python 3.11 ^(64-bit^) yourself from https://www.python.org/downloads/windows/
    echo  ^(tick "py launcher"^), then run setup.bat again.
    exit /b 1
  )
)
set "UV_PYTHON_INSTALL_DIR=%SCRIBE_TOOLS%\python"
set "UV_CACHE_DIR=%SCRIBE_TOOLS%\cache"
"%SCRIBE_TOOLS%\uv\uv.exe" venv venv --python cpython-3.11-windows-x86_64-none --seed --python-preference only-managed || exit /b 1
exit /b 0

:check_runtime
rem The speech libraries (CTranslate2, ONNX Runtime) need Microsoft's Visual
rem C++ runtime. Most PCs have it; a fresh Windows may not. Installing it
rem needs an administrator's OK, so Windows asks - but only when the runtime
rem really is what's missing (Windows can't load msvcp140.dll, or the
rem msvcp140_1.dll that only the 2017+ runtime has).
venv\Scripts\python -c "import ctranslate2, onnxruntime" >nul 2>&1 && exit /b 0
venv\Scripts\python -c "import ctypes; ctypes.WinDLL('msvcp140.dll'); ctypes.WinDLL('msvcp140_1.dll')" >nul 2>&1 && goto :libs_broken
echo        Scribe needs Microsoft's Visual C++ runtime, which this PC doesn't
echo        have yet. Downloading it from Microsoft - Windows will ask you to
echo        allow the install.
set "SCRIBE_TOOLS=%~dp0.tools"
if not exist "%SCRIBE_TOOLS%" mkdir "%SCRIBE_TOOLS%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile (Join-Path $env:SCRIBE_TOOLS 'vc_redist.x64.exe')"
if errorlevel 1 goto :runtime_manual
"%SCRIBE_TOOLS%\vc_redist.x64.exe" /install /passive /norestart
venv\Scripts\python -c "import ctranslate2, onnxruntime" >nul 2>&1 && exit /b 0
:runtime_manual
echo.
echo  Microsoft's Visual C++ runtime couldn't be set up. Install it from
echo      https://aka.ms/vs/17/release/vc_redist.x64.exe
echo  then run setup.bat again.
exit /b 1

:libs_broken
echo        Scribe's speech libraries didn't load:
venv\Scripts\python -c "import ctranslate2, onnxruntime"
echo.
echo  Run setup.bat again. If this keeps happening, delete the "venv" folder
echo  inside Scribe's folder and run setup.bat once more.
exit /b 1

:check_onedrive
rem Is this folder inside the OneDrive folder %1 (personal or work)? Replace
rem that path in HERE: if anything changed, it was there.
set "OD=%~1"
call set "REST=%%HERE:%OD%\=%%"
if not "%REST%"=="%HERE%" set "INONEDRIVE=1"
exit /b 0

:wait_for_quit
rem An update can't replace files a running Scribe is using: ask to quit it.
rem (instance.is_running checks Scribe's single-instance mutex - see instance.py.)
venv\Scripts\python -c "import sys, instance; sys.exit(3 if instance.is_running() else 0)" >nul 2>&1
if errorlevel 3 (
  echo  Scribe is running. Quit it from its tray icon ^(right-click -^> Quit^), then press a key.
  pause >nul
  goto :wait_for_quit
)
exit /b 0

:inside_zip
echo.
echo  Scribe is still inside the ZIP file.
echo.
echo  Right-click the ZIP, choose "Extract All...", type C:\ as the folder and
echo  click Extract - you'll get C:\Scribe. Then open C:\Scribe and
echo  double-click setup.bat there.
echo.
pause
exit /b 1

:failed
echo.
echo  Setup didn't finish - see the messages above. Running setup.bat again
echo  usually continues where it stopped.
pause
exit /b 1
