@echo off
REM Builds Desktop Cat into a single standalone .exe (no Python needed to run it).
REM Run this from the same folder as main.py and requirements.txt.

pip install -r requirements.txt
pip install pyinstaller

pyinstaller --noconfirm --onefile --windowed ^
  --name "DesktopCat" ^
  --add-data "assets;assets" ^
  main.py

echo.
echo Build complete. Your exe is at: dist\DesktopCat.exe
echo You can move/copy that single file anywhere -- assets are bundled inside it.
pause