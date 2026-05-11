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

:: Snapshot any pre-existing model weights *before* the wipe so they
:: survive even when the source models\*.h5 is hidden by .gitignore.
:: See prepare.sh for the long-form explanation.
set "SNAPSHOT_DIR=%TEMP%\sleep_classifier_models_%RANDOM%%RANDOM%"
if exist "%ROOTFS%\models" (
    mkdir "%SNAPSHOT_DIR%" >nul 2>&1
    for %%E in (h5 hdf5) do (
        for %%F in ("%ROOTFS%\models\*.%%E") do (
            if exist "%%F" (
                copy /y "%%F" "%SNAPSHOT_DIR%\" >nul
                echo [prepare] snapshotted %%~nxF
            )
        )
    )
)

if exist "%ROOTFS%" rmdir /s /q "%ROOTFS%"
mkdir "%ROOTFS%"

:: Critical inputs the Dockerfile relies on -- missing means the add-on
:: image will be broken at build time, so we want to fail loudly here
:: rather than 30 minutes later on the Pi.
for %%D in (src scripts config) do (
    if exist "%REPO_ROOT%\%%D" (
        xcopy /e /i /q /y "%REPO_ROOT%\%%D" "%ROOTFS%\%%D" >nul
        if errorlevel 1 (
            echo [prepare] ERROR: xcopy failed for %%D\
            exit /b 2
        )
        echo [prepare] mirrored %%D\
    ) else (
        echo [prepare] ERROR: required directory %%D\ not found at %REPO_ROOT%\%%D
        echo [prepare]        Run this script from a clean repo checkout.
        exit /b 1
    )
)

:: Optional inputs -- missing is a soft warning so users without a trained
:: model can still package the add-on and have it fall back to bootstrap
:: weights at runtime.
for %%D in (models) do (
    if exist "%REPO_ROOT%\%%D" (
        xcopy /e /i /q /y "%REPO_ROOT%\%%D" "%ROOTFS%\%%D" >nul
        echo [prepare] mirrored %%D\
    ) else (
        echo [prepare] WARNING: %%D\ not found -- add-on will use random weights
    )
)

:: Restore any *.h5 / *.hdf5 weights from snapshot whose corresponding
:: source was hidden by .gitignore on this checkout.
if exist "%SNAPSHOT_DIR%" (
    if not exist "%ROOTFS%\models" mkdir "%ROOTFS%\models"
    for %%F in ("%SNAPSHOT_DIR%\*") do (
        if not exist "%ROOTFS%\models\%%~nxF" (
            copy /y "%%F" "%ROOTFS%\models\%%~nxF" >nul
            echo [prepare] restored %%~nxF from snapshot
        )
    )
    rmdir /s /q "%SNAPSHOT_DIR%"
)

:: requirements-runtime.txt is the only file the Dockerfile actually
:: pip-installs (no TensorFlow); missing it would yield a broken image
:: so we hard-fail.  The train + full lists are nice-to-have for users
:: who SSH into the running container to reproduce training-time errors.
if exist "%REPO_ROOT%\requirements-runtime.txt" (
    copy /y "%REPO_ROOT%\requirements-runtime.txt" "%ROOTFS%\requirements-runtime.txt" >nul
    echo [prepare] copied requirements-runtime.txt
) else (
    echo [prepare] ERROR: requirements-runtime.txt missing -- the add-on Dockerfile
    echo [prepare]        cannot pip-install without it.
    exit /b 1
)
for %%R in (requirements-train.txt requirements.txt) do (
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
