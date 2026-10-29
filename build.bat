@echo off
rem Builds dist\Snapshots.exe from this folder. Needs Python 3.11 or newer.
rem Double-click it. It is safe to run again: it repairs its own build environment if needed.
setlocal EnableExtensions
cd /d "%~dp0"

rem ---- 1. Find a suitable Python
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python" & goto :havepython
py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3" & goto :havepython
goto :nopython

:havepython
echo Using Python:
%PY% --version

rem ---- 2. A private build environment, kept outside this folder
set "VENV=%LOCALAPPDATA%\Snapshots-build\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "USERFLAG="

if not exist "%VPY%" goto :makevenv
"%VPY%" -m pip --version >nul 2>nul
if not errorlevel 1 goto :venvok
echo The build environment looks damaged. Rebuilding it...
rmdir /s /q "%VENV%"

:makevenv
echo Creating the build environment...
%PY% -m venv "%VENV%"
if errorlevel 1 goto :venvfail
"%VPY%" -m pip --version >nul 2>nul
if errorlevel 1 goto :venvfail
goto :venvok

:venvfail
echo.
echo Could not set up a private build environment. Security software may be blocking it.
echo Trying with your main Python installation instead...
set "PYRUN=%PY%"
set "USERFLAG=--user"
goto :install

:venvok
set PYRUN="%VPY%"

rem ---- 3. Install and build
:install
echo.
echo Installing requirements...
%PYRUN% -m pip install --disable-pip-version-check --progress-bar off %USERFLAG% -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo.
echo Building Snapshots.exe, this takes a minute or two...
%PYRUN% -m PyInstaller --noconfirm --clean snapshots.spec
if errorlevel 1 goto :fail

if not exist "dist\Snapshots.exe" goto :missingexe

echo.
echo Done. Your program is:
echo   %~dp0dist\Snapshots.exe
pause
exit /b 0

:nopython
echo.
echo Python 3.11 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then run this file again.
pause
exit /b 1

:missingexe
echo.
echo The build finished but dist\Snapshots.exe is not there.
echo Windows Security may have removed it. Open Windows Security, then
echo Virus and threat protection, then Protection history, and restore it if it is listed.
echo (Programs built with PyInstaller are sometimes flagged by mistake.)
pause
exit /b 1

:fail
echo.
echo The build failed. Scroll up to see why.
pause
exit /b 1
