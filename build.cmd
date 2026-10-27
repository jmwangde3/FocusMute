@echo off
rem Builds FocusMute.exe (one file, no console window) into dist\
setlocal
python -m pip install -r requirements.txt
python -m PyInstaller --onefile --noconsole --name FocusMute --icon focusmute.ico focusmute.pyw
echo.
echo Built: dist\FocusMute.exe
endlocal
