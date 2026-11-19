<#
build_apk.ps1 — 出 Android 单包 → dist\release\v<ver>\android-arm64\hp-track.apk

前置：JDK 17+、Android SDK（platforms;android-36 + build-tools）、Gradle 9.3.1（缺则自动下载）

    .\scripts\build_apk.ps1
    .\scripts\build_apk.ps1 -GradleHome C:\gradle\gradle-9.3.1
    .\scripts\build_apk.ps1 -SkipVerify
#>
[CmdletBinding()]
param(
  [string]$GradleHome = "",
  [string]$Task       = "assembleRelease",
  [string]$GradleVersion = "9.3.1",
  [string]$PythonExe = "python",
  [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$android = Join-Path $root "android"
$version = (Get-Content (Join-Path $root "VERSION") -Raw).Trim()

# ---------- SDK ----------
$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) { $sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk" }
if (-not (Test-Path $sdk)) { throw "找不到 Android SDK（设 ANDROID_HOME）" }
"SDK: $sdk"
Set-Content -Path (Join-Path $android "local.properties") -Value "sdk.dir=$($sdk -replace '\\','/')" -Encoding ASCII

# ---------- JDK ----------
if (-not $env:JAVA_HOME) {
  $j = Get-Command java -ErrorAction SilentlyContinue
  if ($j) { $env:JAVA_HOME = Split-Path -Parent (Split-Path -Parent $j.Source) }
}
if (-not $env:JAVA_HOME) { throw "找不到 JDK（设 JAVA_HOME）" }
"JDK: $env:JAVA_HOME"

# ---------- Gradle ----------
if (-not $GradleHome) {
  $cached = Join-Path $root ".build\gradle\gradle-$GradleVersion"
  if (-not (Test-Path $cached)) {
    $zip = Join-Path $root ".build\gradle\gradle-$GradleVersion-bin.zip"
    if (-not (Test-Path $zip)) {
      New-Item -ItemType Directory -Force -Path (Split-Path $zip) | Out-Null
      "下载 Gradle $GradleVersion（约 130 MB）…"
      Invoke-WebRequest "https://services.gradle.org/distributions/gradle-$GradleVersion-bin.zip" -OutFile $zip -UseBasicParsing
    }
    Expand-Archive $zip -DestinationPath (Join-Path $root ".build\gradle") -Force
  }
  $GradleHome = $cached
}
$gradle = Join-Path $GradleHome "bin\gradle.bat"
if (-not (Test-Path $gradle)) { throw "找不到 $gradle" }
"Gradle: $GradleHome"

# ---------- 构建 ----------
Push-Location $android
try {
  & $gradle --no-daemon $Task | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "Gradle 失败" }
} finally { Pop-Location }

# ---------- 产物 → release 路径 ----------
$apk = Get-ChildItem (Join-Path $android "app\build\outputs\apk") -Recurse -Filter "*.apk" |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $apk) { throw "没找到 APK 产物" }

$outDir = Join-Path $root "dist\release\v$version\android-arm64"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$out = Join-Path $outDir "hp-track.apk"
Copy-Item $apk.FullName $out -Force
"`n产物: $out  (" + [math]::Round((Get-Item $out).Length / 1MB, 2) + " MB)  v$version"

# ---------- 验签 / 清单 ----------
$bt = Get-ChildItem (Join-Path $sdk "build-tools") -Directory | Sort-Object Name -Descending | Select-Object -First 1
$apksigner = Join-Path $bt.FullName "apksigner.bat"
if (Test-Path $apksigner) { & $apksigner verify --print-certs $out | Select-Object -First 3 }

if (-not $SkipVerify) {
  & $PythonExe scripts\verify_apk.py $out --sdk $sdk | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "APK 结构验收失败" }
}

