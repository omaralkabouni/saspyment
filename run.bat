@echo off
echo ==============================================
echo       SAS Radius Control Dashboard
echo ==============================================
echo.
echo Starting the local server...
call venv\Scripts\activate.bat
python app.py
pause
