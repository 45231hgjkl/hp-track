<#
build_windows.ps1 — 出 Windows 单文件 → dist\release\v<ver>\windows-x64\hp-track.exe

    .\scripts\build_windows.ps1                     # 用 PATH 里的 python
    .\scripts\build_windows.ps1 -PythonExe "C:\...\python.exe"
    .\scripts\build_windows.ps1 -SkipVenv           # 复用已有 .build\venv-win
    .\scripts\build_windows.ps1 -SkipVerify         # 只出包不冒烟（CI 里分开跑）

产物双击即用：自动开浏览器 → http://127.0.0.1:8737 → 页面「关闭本地服务」退出。
会话写 %APPDATA%\hp-track\session.json。
#>
[CmdletBinding()]
param(
  [string]$PythonExe = "python",
  [string]$Name      = "hp-track",
  [switch]$SkipVenv,
  [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
# CI/控制台可能是 cp1252/GBK：强制 Python 以 UTF-8 输出中文
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Get-Content (Join-Path $root "VERSION") -Raw).Trim()
$outDir  = Join-Path $root "dist\release\v$version\windows-x64"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# 1) 组装 stage（确保 index.html 与源码同版本）
& $PythonExe scripts\stage.py | Out-Host
if ($LASTEXITCODE -ne 0) { throw "stage 失败" }

# 2) 构建环境
$venv = Join-Path $root ".build\venv-win"
if (-not $SkipVenv) {
  if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }
  & $PythonExe -m venv $venv
  & (Join-Path $venv "Scripts\python.exe") -m pip install --disable-pip-version-check -q --upgrade pip
  & (Join-Path $venv "Scripts\python.exe") -m pip install --disable-pip-version-check -q pyinstaller
}
$pyi = Join-Path $venv "Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) { throw "找不到 $pyi（先去掉 -SkipVenv 建一次环境）" }

# 3) 打包：只带 src 下 4 个运行时文件，排除所有不需要的重模块
$icon = Join-Path $root "assets\icon.ico"
$iconArg = @()
if (Test-Path $icon) { $iconArg = @("--icon", $icon) }

& $pyi --onefile --name $Name `
  --add-data "$root\src\index.html;." `
  --paths src `
  --distpath $outDir `
  --workpath (Join-Path $root ".build\pyi-work") `
  --specpath (Join-Path $root ".build\pyi-spec") `
  --exclude-module cryptography --exclude-module tkinter --exclude-module unittest `
  --exclude-module pydoc --exclude-module setuptools --exclude-module pkg_resources `
  --exclude-module mitmproxy --exclude-module frida `
  @iconArg --noconfirm --clean src\school_web.py | Out-Host
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Join-Path $outDir "$Name.exe"
if (-not (Test-Path $exe)) { throw "产物不存在: $exe" }

# 4) 冒烟
if (-not $SkipVerify) {
  & $PythonExe scripts\verify_artifacts.py $exe | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "冒烟测试失败" }
}

"`n产物: $exe  (" + [math]::Round((Get-Item $exe).Length/1MB,2) + " MB)  v$version"

