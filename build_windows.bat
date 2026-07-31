@echo off
setlocal
cd /d %~dp0
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name StrategyResearchLab ^
  --add-data "plugins;plugins" ^
  --add-data "presets;presets" ^
  app.py
echo.
echo Build complete: dist\StrategyResearchLab.exe
pause
