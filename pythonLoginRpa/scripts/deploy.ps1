# ChatGPT Auto-Register — Windows 一键部署脚本
# 用法: 在 PowerShell 中以管理员身份运行
#   cd <项目目录>\pythonLoginRpa
#   .\deploy.ps1
#
# 可选参数:
#   .\deploy.ps1 -Count 3             # 注册 3 个账号
#   .\deploy.ps1 -Fresh               # 使用临时无痕 Profile
#   .\deploy.ps1 -OpenOnly            # 仅打开登录页
#   .\deploy.ps1 -InstallPatchright   # 同时安装 patchright（更好的反检测）

param(
    [int]$Count = 1,
    [switch]$Fresh,
    [switch]$OpenOnly,
    [switch]$InstallPatchright
)

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Python312InstallerUrl = "https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe"
$Python312InstallerPath = "$env:TEMP\python-3.12.0-amd64.exe"
$ChromeInstallerUrl = "https://dl.google.com/chrome/install/latest/chrome_installer.exe"
$ChromeInstallerPath = "$env:TEMP\chrome_installer.exe"

Set-Location $ProjectRoot
$ErrorActionPreference = "Stop"

# ──────────────────────────────────────────────────────────────
#  工具函数
# ──────────────────────────────────────────────────────────────
function Write-Step([string]$msg) {
    Write-Host "`n>>> $msg" -ForegroundColor Cyan
}

function Write-OK([string]$msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "  [!] $msg" -ForegroundColor Yellow
}

function Write-Fail([string]$msg) {
    Write-Host "  [X] $msg" -ForegroundColor Red
}

function Get-Python312Exe {
    $candidatePaths = @(
        "C:\Program Files\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "C:\Python312\python.exe"
    )

    foreach ($candidate in $candidatePaths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $resolved = & py -3.12 -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path $resolved.Trim())) {
            return $resolved.Trim()
        }
    }

    return $null
}

function Get-ChromeExe {
    $candidatePaths = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
    )

    foreach ($candidate in $candidatePaths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

# ──────────────────────────────────────────────────────────────
#  头部输出
# ──────────────────────────────────────────────────────────────
Write-Host "=" * 52 -ForegroundColor Cyan
Write-Host "  ChatGPT Auto-Register — 一键部署" -ForegroundColor Cyan
Write-Host "  项目目录: $ProjectRoot" -ForegroundColor Cyan
Write-Host "=" * 52 -ForegroundColor Cyan

# ──────────────────────────────────────────────────────────────
#  检查必要文件
# ──────────────────────────────────────────────────────────────
Write-Step "检查项目文件"

if (!(Test-Path "$ProjectRoot\autoregister.py")) {
    Write-Fail "未找到 autoregister.py，请确认当前目录是否为 pythonLoginRpa/"
    exit 1
}

if (!(Test-Path "$ProjectRoot\requirements.txt")) {
    Write-Fail "未找到 requirements.txt"
    exit 1
}

if (!(Test-Path "$ProjectRoot\rpalogin.js")) {
    Write-Warn "未找到 rpalogin.js，脚本运行时可能报错。请确认 userscript 文件已放入本目录。"
}

Write-OK "项目文件检查完毕"

# ──────────────────────────────────────────────────────────────
#  检查 / 安装 Python 3.12
# ──────────────────────────────────────────────────────────────
Write-Step "检查 Python 3.12"

$PythonExe = Get-Python312Exe

if (-not $PythonExe) {
    Write-Warn "未检测到 Python 3.12，开始自动下载安装..."
    Write-Host "  下载地址: $Python312InstallerUrl" -ForegroundColor DarkGray

    try {
        Invoke-WebRequest -Uri $Python312InstallerUrl -OutFile $Python312InstallerPath -UseBasicParsing
        Write-Host "  正在安装 Python 3.12（静默安装，约需 1-2 分钟）..." -ForegroundColor Yellow
        Start-Process -FilePath $Python312InstallerPath `
            -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1" `
            -Wait

        # 刷新环境变量
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")

        $PythonExe = Get-Python312Exe
    } catch {
        Write-Fail "下载或安装 Python 3.12 失败: $_"
        Write-Host "  请手动下载安装: $Python312InstallerUrl" -ForegroundColor Yellow
        exit 1
    }

    if (-not $PythonExe) {
        Write-Fail "Python 3.12 安装完成后仍未找到 python.exe"
        Write-Host "  请手动安装后重新打开 PowerShell 再执行本脚本" -ForegroundColor Yellow
        Write-Host "  安装器路径: $Python312InstallerPath" -ForegroundColor Yellow
        exit 1
    }
}

& $PythonExe --version
Write-OK "Python 3.12: $PythonExe"

# ──────────────────────────────────────────────────────────────
#  检查 Google Chrome（脚本用 channel="chrome" 需真实 Chrome）
# ──────────────────────────────────────────────────────────────
Write-Step "检查 Google Chrome"

$ChromeExe = Get-ChromeExe

if (-not $ChromeExe) {
    Write-Warn "未检测到 Google Chrome，开始自动下载安装..."
    Write-Host "  注意: autoregister.py 使用 channel='chrome'，必须安装真实 Chrome" -ForegroundColor Yellow

    try {
        Invoke-WebRequest -Uri $ChromeInstallerUrl -OutFile $ChromeInstallerPath -UseBasicParsing
        Write-Host "  正在安装 Google Chrome..." -ForegroundColor Yellow
        Start-Process -FilePath $ChromeInstallerPath -ArgumentList "/silent /install" -Wait

        $ChromeExe = Get-ChromeExe
    } catch {
        Write-Warn "Chrome 自动安装失败: $_"
        Write-Host "  请手动安装 Google Chrome 后再运行脚本" -ForegroundColor Yellow
        Write-Host "  https://www.google.com/chrome/" -ForegroundColor Yellow
        # 不退出，让用户决定是否继续
    }
}

if ($ChromeExe) {
    Write-OK "Google Chrome: $ChromeExe"
} else {
    Write-Warn "未找到 Google Chrome，脚本运行时可能失败"
}

# ──────────────────────────────────────────────────────────────
#  创建虚拟环境（先删除旧的，避免跨机器复用问题）
# ──────────────────────────────────────────────────────────────
Write-Step "创建 Python 虚拟环境"

if (Test-Path "$ProjectRoot\.venv") {
    Write-Host "  删除旧虚拟环境..." -ForegroundColor DarkGray
    Remove-Item "$ProjectRoot\.venv" -Recurse -Force
}

& $PythonExe -m venv "$ProjectRoot\.venv"
if ($LASTEXITCODE -ne 0 -or !(Test-Path "$ProjectRoot\.venv\Scripts\python.exe")) {
    Write-Fail "创建虚拟环境失败"
    exit 1
}

$VenvPython = "$ProjectRoot\.venv\Scripts\python.exe"
$VenvPip    = "$ProjectRoot\.venv\Scripts\pip.exe"
Write-OK "虚拟环境: $ProjectRoot\.venv"

# ──────────────────────────────────────────────────────────────
#  安装依赖
# ──────────────────────────────────────────────────────────────
Write-Step "升级 pip"
& $VenvPython -m pip install --upgrade pip

Write-Step "安装 requirements.txt"
& $VenvPython -m pip install -r "$ProjectRoot\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install requirements.txt 失败"
    exit 1
}
Write-OK "依赖安装完成"

# ──────────────────────────────────────────────────────────────
#  可选：安装 patchright（更好的反 bot 检测绕过）
# ──────────────────────────────────────────────────────────────
if ($InstallPatchright) {
    Write-Step "安装 patchright（可选，更强反检测）"
    & $VenvPython -m pip install patchright
    & $VenvPython -m patchright install chromium
    Write-OK "patchright 安装完成"
}

# ──────────────────────────────────────────────────────────────
#  安装 Playwright Chromium（备用浏览器）
# ──────────────────────────────────────────────────────────────
Write-Step "安装 Playwright Chromium"
& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Warn "playwright install chromium 失败，脚本会优先使用系统 Chrome"
}
Write-OK "Playwright Chromium 安装完成"

# ──────────────────────────────────────────────────────────────
#  检查配置文件
# ──────────────────────────────────────────────────────────────
Write-Step "检查配置"

if (!(Test-Path "$ProjectRoot\config.json") -and (Test-Path "$ProjectRoot\config.example.json")) {
    Write-Warn "未找到 config.json，已从 config.example.json 复制，请按需修改"
    Copy-Item "$ProjectRoot\config.example.json" "$ProjectRoot\config.json"
}

Write-Host ""
Write-Host "=" * 52 -ForegroundColor Green
Write-Host "  部署完成！" -ForegroundColor Green
Write-Host "=" * 52 -ForegroundColor Green
Write-Host ""
Write-Host "  重要提示：" -ForegroundColor Yellow
Write-Host "  1. 修改 autoregister.py 顶部配置区的 API Key 和代理地址" -ForegroundColor Yellow
Write-Host "     NPCMAIL_API_KEY / GPTMAIL_API_KEY / HTTP_PROXY" -ForegroundColor Yellow
Write-Host "  2. 确保 rpalogin.js 已放入本目录" -ForegroundColor Yellow
Write-Host "  3. 确保本地代理 (默认 127.0.0.1:6987) 已启动" -ForegroundColor Yellow
Write-Host ""

# ──────────────────────────────────────────────────────────────
#  启动脚本
# ──────────────────────────────────────────────────────────────
Write-Step "启动 autoregister.py"

$RunArgs = @()
if ($Count -gt 1)  { $RunArgs += $Count }
if ($Fresh)        { $RunArgs += "--fresh" }
if ($OpenOnly)     { $RunArgs += "--open-only" }

Write-Host "  命令: $VenvPython autoregister.py $($RunArgs -join ' ')" -ForegroundColor DarkGray
Write-Host ""

& $VenvPython "$ProjectRoot\autoregister.py" @RunArgs

# ──────────────────────────────────────────────────────────────
#  快速参考
# ──────────────────────────────────────────────────────────────
# 注册 1 个账号:
#   .\deploy.ps1
#
# 注册 5 个账号（并发）:
#   .\deploy.ps1 -Count 5 -Fresh
#
# 仅打开登录页（调试用）:
#   .\deploy.ps1 -OpenOnly
#
# 带 patchright 反检测（首次需要安装）:
#   .\deploy.ps1 -InstallPatchright
#
# 仅重新运行（跳过部署，直接用已有 venv）:
#   .\.venv\Scripts\python.exe autoregister.py
#   .\.venv\Scripts\python.exe autoregister.py 3 --fresh
