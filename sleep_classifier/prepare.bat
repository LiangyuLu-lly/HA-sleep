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
:: v1.3.0 simplification
:: ---------------------
:: The add-on no longer ships a CNN-BiLSTM model -- it subscribes to a
:: sleep-stage entity the user already has in HA.  That removes the
:: 9 MB .h5 weight file and the whole TensorFlow-only requirements-train
:: list.  models\ is not mirrored any more; only requirements-runtime.txt
:: is required.
::
:: Re-run after every change to src\, scripts\, training_config\ or the
:: runtime requirements file.  Safe to re-run; existing files are
:: overwritten.
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
:: Add-on sits at the repository root now (v1.2.3+) so the project root
:: is exactly one level up, not two.  HA Supervisor needs this layout to
:: discover the add-on directly when the user adds the repo URL.
pushd "%SCRIPT_DIR%\.." >nul
set "REPO_ROOT=%CD%"
popd >nul
set "ROOTFS=%SCRIPT_DIR%\rootfs"

echo [prepare] repo root : %REPO_ROOT%
echo [prepare] add-on    : %SCRIPT_DIR%
echo [prepare] target    : %ROOTFS%

if exist "%ROOTFS%" rmdir /s /q "%ROOTFS%"
mkdir "%ROOTFS%"

:: Since v1.3.0 every mirrored directory is required -- there are no
:: optional inputs any more.  Missing one means the add-on image will be
:: broken at build time, so fail loudly here instead of 30 minutes later
:: on the Pi.
for %%D in (src scripts training_config) do (
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

:: requirements-runtime.txt is the only file the Dockerfile actually
:: pip-installs; missing it would yield a broken image so we hard-fail.
:: The full requirements.txt is nice-to-have for users who SSH into the
:: running container to reproduce bugs.
if exist "%REPO_ROOT%\requirements-runtime.txt" (
    copy /y "%REPO_ROOT%\requirements-runtime.txt" "%ROOTFS%\requirements-runtime.txt" >nul
    echo [prepare] copied requirements-runtime.txt
) else (
    echo [prepare] ERROR: requirements-runtime.txt missing -- the add-on Dockerfile
    echo [prepare]        cannot pip-install without it.
    exit /b 1
)
if exist "%REPO_ROOT%\requirements.txt" (
    copy /y "%REPO_ROOT%\requirements.txt" "%ROOTFS%\requirements.txt" >nul
    echo [prepare] copied requirements.txt
)

:: Strip Python cache and large datasets that don't belong in an image.
for /d /r "%ROOTFS%" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D"
)
if exist "%ROOTFS%\data\sleep-edf-telemetry" rmdir /s /q "%ROOTFS%\data\sleep-edf-telemetry"
del /s /q "%ROOTFS%\*.pyc" >nul 2>&1

:: v3.0.0 训练产物兜底镜像（R7.5 / R12.1）
:: ----------------------------------------------------------------
:: 上面的整树 xcopy training_config 已经把 population_prior.pickle /
:: stage_predictor.onnx 拷贝到 rootfs；这里再显式 copy 一次是「腰带+
:: 背带」式的防御，防止未来 .gitignore 把这两个产物排除后训练脚本生
:: 成的本地副本被忽略。两个产物都是可选输入：缺失时 if exist 直接跳
:: 过，不应让 prepare 挂掉。
if not exist "%ROOTFS%\training_config" mkdir "%ROOTFS%\training_config"
if exist "%REPO_ROOT%\training_config\population_prior.pickle" (
    copy /y "%REPO_ROOT%\training_config\population_prior.pickle" "%ROOTFS%\training_config\" >nul
)
if exist "%REPO_ROOT%\training_config\stage_predictor.onnx" (
    copy /y "%REPO_ROOT%\training_config\stage_predictor.onnx" "%ROOTFS%\training_config\" >nul
)

:: 非 strict 模式校验训练产物尺寸 + 嵌入 SHA-256（R7.5 / R12.1）
:: ----------------------------------------------------------------
:: 缺失文件以 WARN 级别提示，只有 size / sha256 违规才 exit 1。即便
:: check_artifacts.py 返回非零也不要让 prepare 挂掉——本地 prepare 仅
:: 提示，发布闸门留给 CI 的 --strict 调用。
python "%REPO_ROOT%\scripts\check_artifacts.py"

echo [prepare] done
endlocal
exit /b 0
