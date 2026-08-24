@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\vpstitch-gui.exe" (
  echo VP Stitch GUI is not installed in .venv.
  echo Run: .venv\Scripts\python.exe -m pip install -e .
  pause
  exit /b 1
)
start "VP Stitch" ".venv\Scripts\vpstitch-gui.exe"
