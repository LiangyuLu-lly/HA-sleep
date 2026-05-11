@echo off
:: Mirror the project source tree into addons\sleep_classifier\rootfs\.
::
:: Why this exists
:: ---------------
:: Home Assistant's add-on builder uses the add-on directory as the Docker
:: build context.  Files outside of addons\sleep_classifier\ are therefore
:: unreachable from the Dockerfile's COPY instructions.  Run this script
:: once before pushing to GitHub so that the Supervisor has everything it
:: needs to build the image on the Pi.
::
:: Re-run after every change to src\, scripts\, config\, models\ or
:: requirements.txt.  Safe to re-run; existing files are overwritten.
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
pushd "%SCRIPT_DIR%\..\.." >nul
set "REPO_ROOT=%CD%"
popd >nul
set "ROOTFS=%SCRIPT_DIR%\rootfs"

echo [prepare] repo root : %REPO_ROOT%
echo [prepare] add-on    : %SCRIPT_DIR%
echo [prepare] target    : %ROOTFS%

if exist "%ROOTFS%" rmdir /s /q "%ROOTFS%"
mkdir "%ROOTFS%"

for %%D in (src scripts config models) do (
    if exist "%REPO_ROOT%\%%D" (
        xcopy /e /i /q /y "%REPO_ROOT%\%%D" "%ROOTFS%\%%D" >nul
        echo [prepare] mirrored %%D\
    ) else (
        echo [prepare] WARNING: %%D\ not found at repo root
    )
)

:: The add-on Dockerfile only installs requirements-runtime.txt (no
:: TensorFlow), so mirror all three files: runtime is what pip actually
:: consumes; train/full lists are kept for devs who need to reproduce
:: training-time errors inside the running container.
for %%R in (requirements-runtime.txt requirements-train.txt requirements.txt) do (
    if exist "%REPO_ROOT%\%%R" (
        copy /y "%REPO_ROOT%\%%R" "%ROOTFS%\%%R" >nul
        echo [prepare] copied %%R
    ) else (
        echo [prepare] WARNING: %%R not found at repo root
    )
)

:: Strip Python cache and large datasets that don't belong in an image.
for /d /r "%ROOTFS%" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D"
)
if exist "%ROOTFS%\data\sleep-edf-telemetry" rmdir /s /q "%ROOTFS%\data\sleep-edf-telemetry"
del /s /q "%ROOTFS%\*.pyc" >nul 2>&1

echo [prepare] done
endlocal
exit /b 0
