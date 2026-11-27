@echo off
rem Compila EasyLauncher in modalita' portable (--onedir) preservando i dati utente.
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt

if not exist _dist_bak mkdir _dist_bak
for %%D in (data emulators media roms) do (
    if exist "dist\EasyLauncher\%%D" move /y "dist\EasyLauncher\%%D" "_dist_bak\%%D" >nul
)

pyinstaller --noconfirm --onedir --windowed --name EasyLauncher main.py

if exist "icons" xcopy /e /i /y "icons" "dist\EasyLauncher\icons" >nul
if exist "docs" xcopy /e /i /y "docs" "dist\EasyLauncher\docs" >nul

for %%D in (data emulators media roms) do (
    if exist "_dist_bak\%%D" move /y "_dist_bak\%%D" "dist\EasyLauncher\%%D" >nul
)
rmdir /s /q _dist_bak 2>nul

echo.
echo Build completata in: dist\EasyLauncher\EasyLauncher.exe
pause
