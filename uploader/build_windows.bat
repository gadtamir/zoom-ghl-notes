@echo off
REM Build ZoomGHL.exe on Windows.
REM Run this from a Windows machine with Python 3.12 installed.
REM Output: dist\ZoomGHL\ZoomGHL.exe (and supporting files)
setlocal enabledelayedexpansion

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt

REM 1) Render the icons.
python build_icon.py

REM 2) Clean previous build.
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

REM 3) Build the .exe.
pyinstaller zghl.spec --noconfirm --clean

if not exist "dist\ZoomGHL\ZoomGHL.exe" (
    echo X ZoomGHL.exe was not produced. Check the PyInstaller output.
    exit /b 1
)

echo.
echo ✓ Built: dist\ZoomGHL\ZoomGHL.exe
echo.
echo Test the .exe by double-clicking it.
echo For distribution: zip the entire dist\ZoomGHL\ folder and send it.
echo.
echo (Optional next step: bundle with Inno Setup or NSIS to make a Setup.exe installer.)
