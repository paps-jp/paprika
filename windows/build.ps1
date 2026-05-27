# Build the Windows paprika.exe portable distribution.
#
# Usage (from project root):
#   .\windows\build.ps1                    # full build
#   .\windows\build.ps1 -SkipRedisFetch    # reuse cached windows\bin\redis
#   .\windows\build.ps1 -Clean             # wipe dist/, build/, caches first
#
# Output:
#   dist\paprika\paprika.exe + supporting files
#   dist\paprika-windows-vX.Y.Z.zip        (ready-to-publish)
#
# Requirements on the build machine:
#   * Python 3.11+
#   * pip install pyinstaller pywebview pystray pillow
#   * pip install -r requirements.txt   (paprika hub/worker deps)

param(
    [switch]$Clean,
    [switch]$SkipRedisFetch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> Build root: $Root"

# --- Read VERSION ---------------------------------------------------------
$Version = "dev"
if (Test-Path "$Root\VERSION") {
    $Version = (Get-Content "$Root\VERSION" -Raw).Trim()
}
Write-Host "==> Building paprika $Version (Windows portable)"

# --- Clean ---------------------------------------------------------------
if ($Clean) {
    Write-Host "==> Cleaning dist/ build/ __pycache__/"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue dist, build
    Get-ChildItem -Recurse -Directory -Filter __pycache__ |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Fetch bundled Redis (tporadowski/redis) -----------------------------
$RedisDir = "$Root\windows\bin\redis"
if (-not $SkipRedisFetch -and -not (Test-Path "$RedisDir\redis-server.exe")) {
    Write-Host "==> Fetching tporadowski/redis (~5MB)"
    $RedisUrl = "https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip"
    $RedisZip = "$env:TEMP\paprika-redis.zip"
    Invoke-WebRequest -Uri $RedisUrl -OutFile $RedisZip
    New-Item -ItemType Directory -Force -Path $RedisDir | Out-Null
    Expand-Archive -Path $RedisZip -DestinationPath $RedisDir -Force
    Remove-Item $RedisZip
    Write-Host "    Redis extracted to $RedisDir"
}

# --- PyInstaller ---------------------------------------------------------
Write-Host "==> Running PyInstaller"
pyinstaller windows\paprika.spec --noconfirm --clean

# --- Zip the dist --------------------------------------------------------
$DistDir = "$Root\dist\paprika"
$ZipName = "paprika-windows-$Version.zip"
$ZipPath = "$Root\dist\$ZipName"
if (Test-Path $ZipPath) { Remove-Item $ZipPath }

Write-Host "==> Compressing -> $ZipName"
Compress-Archive -Path "$DistDir\*" -DestinationPath $ZipPath -CompressionLevel Optimal

$SizeMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "✓ Build complete:"
Write-Host "    $ZipPath  ($SizeMb MB)"
Write-Host ""
Write-Host "Sanity check: unzip somewhere clean and double-click paprika.exe."
