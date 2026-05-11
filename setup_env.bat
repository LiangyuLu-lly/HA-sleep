@echo off
echo ========================================
echo CNN-BiLSTM Sleep Algorithm Setup
echo ========================================
echo.

echo Creating virtual environment...
python -m venv venv
if %errorlevel% neq 0 (
    echo Error: Failed to create virtual environment
    pause
    exit /b 1
)

echo.
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Installing dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo Error: Failed to install dependencies
    pause
    exit /b 1
)

echo.
echo ========================================
echo Setup completed successfully!
echo ========================================
echo.
echo To activate the virtual environment, run:
echo   venv\Scripts\activate
echo.
echo To run tests, use:
echo   pytest tests/
echo.
pause
