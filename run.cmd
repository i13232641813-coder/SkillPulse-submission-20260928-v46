@echo off
setlocal
cd /d "%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"
set "NO_PROXY=*"
set "no_proxy=*"
set "ROOT=%~dp0"
set "PYTHON=%LOCALAPPDATA%\skillpulse-venv\Scripts\python.exe"
set "APP_DIR=%ROOT%backend"
set "REQ=%ROOT%backend\requirements.txt"

echo [SkillPulse] 一键启动（Windows）：自动准备环境并启动本地服务。

rem ---------------- 1) 定位系统 Python 3.10+ ----------------
set "SYS_PY="
py -V >nul 2>&1 && set "SYS_PY=py"
if not defined SYS_PY (
  python -V >nul 2>&1 && set "SYS_PY=python"
)
if not defined SYS_PY (
  echo [SkillPulse] 未检测到 Python。请安装 Python 3.10 或更高版本（安装时勾选 Add to PATH）后重试。
  echo               下载地址：https://www.python.org/downloads/
  pause
  exit /b 1
)
%SYS_PY% -c "import sys; raise SystemExit(0 if sys.version_info.__ge__((3, 10)) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [SkillPulse] 需要 Python 3.10 或更高版本，当前版本过低。
  pause
  exit /b 1
)

rem ---------------- 2) 虚拟环境：缺失或不可用（如从其它机器拷来）则自动重建并装依赖 ----------------
set "NEED_VENV=1"
if exist "%PYTHON%" (
  "%PYTHON%" -c "import fastapi, uvicorn, pydantic, multipart" >nul 2>&1 && set "NEED_VENV=0"
)
if "%NEED_VENV%"=="1" (
  echo [SkillPulse] 准备虚拟环境（首次运行或现有 venv 不可用，将重建，置于 %LOCALAPPDATA%）...
  if exist "%LOCALAPPDATA%\skillpulse-venv" rmdir /s /q "%LOCALAPPDATA%\skillpulse-venv"
  %SYS_PY% -m venv "%LOCALAPPDATA%\skillpulse-venv"
  if errorlevel 1 (
    echo [SkillPulse] 创建虚拟环境失败。
    pause
    exit /b 1
  )
  echo [SkillPulse] 安装依赖（官方源；失败自动尝试清华/阿里镜像）...
  "%PYTHON%" -m pip install --disable-pip-version-check --no-input --no-compile --only-binary=:all: -r "%REQ%" >nul 2>&1
  if not errorlevel 1 goto deps_ok
  echo   [回退 1/2] 官方源失败，尝试清华镜像...
  "%PYTHON%" -m pip install --disable-pip-version-check --no-input --no-compile --only-binary=:all: -i https://pypi.tuna.tsinghua.edu.cn/simple -r "%REQ%" >nul 2>&1
  if not errorlevel 1 goto deps_ok
  echo   [回退 2/2] 清华镜像失败，尝试阿里云镜像...
  "%PYTHON%" -m pip install --disable-pip-version-check --no-input --no-compile --only-binary=:all: -i https://mirrors.aliyun.com/pypi/simple -r "%REQ%" >nul 2>&1
  if errorlevel 1 (
    echo [SkillPulse] 依赖安装失败（已尝试官方与国内镜像）。请检查网络或配置 pip 镜像后重试。
    pause
    exit /b 1
  )
)
:deps_ok

rem ---------------- 2.5) StepFun 可选配置（根目录 stepfun.env；缺失则停用，本地检索仍可用） ----------------
if exist "%ROOT%stepfun.env" (
  echo [SkillPulse] 已读取 stepfun.env：StepFun 需求解析将启用。
  for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%ROOT%stepfun.env") do if not "%%b"=="" set "%%a=%%b"
) else (
  echo [SkillPulse] 未配置 stepfun.env：StepFun 需求解析停用，本地检索与安检仍可用。模板见 stepfun.env.example。
)

rem ---------------- 3) 端口自适应（默认 8000，被占用自动选空闲端口） ----------------
set "PORT=8000"
for /L %%p in (8000,1,8010) do (
  netstat -ano | findstr /c:":%%p " | findstr LISTENING >nul 2>&1
  if errorlevel 1 (
    set "PORT=%%p"
    goto port_found
  )
)
echo [SkillPulse] 8000-8010 端口均被占用，请释放端口后重试，或设置环境变量 SKILLPULSE_PORT。
pause
exit /b 1
:port_found

rem ---------------- 4) 启动后端 ----------------
echo [SkillPulse] 启动本地 API（端口 %PORT%，不读取 backend\.env）。
start "SkillPulse Backend" cmd /k ""%PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port %PORT% --app-dir "%APP_DIR%""

set /a ATTEMPTS=0
:wait
timeout /t 1 >nul
curl -fsS "http://127.0.0.1:%PORT%/api/environment/status" >nul 2>&1
if not errorlevel 1 goto ready
set /a ATTEMPTS+=1
if %ATTEMPTS% GEQ 30 (
  echo [SkillPulse] 后端未能在 30 秒内就绪，请查看 SkillPulse Backend 窗口。
  pause
  exit /b 1
)
goto wait

:ready
echo [SkillPulse] 已启动：
echo   Skill 市场与流程编排：http://127.0.0.1:%PORT%/frontend/lego/index.html
echo   健康检查：http://127.0.0.1:%PORT%/frontend/index.html
echo   API 文档：http://127.0.0.1:%PORT%/docs
start "" "http://127.0.0.1:%PORT%/frontend/lego/index.html"
pause
