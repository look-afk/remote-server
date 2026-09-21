# PCRC Bootstrap — качает готовый .exe с GitHub Releases
# Использование:
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/ТВО_ЮЗ/pcrc/main/bootstrap.ps1 | iex"

$ErrorActionPreference = "Stop"

# ══════════════════════════════════════════════════════
#  ЗАПОЛНИ ОДИН РАЗ
# ══════════════════════════════════════════════════════
$Repo    = "look-afk/remote-server"
$ZipName    = "dist.zip"                              # <-- имя zip-файла на Releases
$SecretKey  = "Zafarjon1224"
$ServerUrl  = "wss://remote-server-mr8v.onrender.com"
$UpdateUrl  = "https://raw.githubusercontent.com/look-afk/pcrc-versions/main"
# ══════════════════════════════════════════════════════

$InstDir    = "$env:USERPROFILE\PCRC"
$ReleaseUrl = "https://github.com/$Repo/releases/latest/download/$ZipName"

Write-Host ""
Write-Host "  ==============================" -ForegroundColor Blue
Write-Host "   PCRC Installer" -ForegroundColor White
Write-Host "  ==============================" -ForegroundColor Blue
Write-Host ""

# Имя ПК
$PCName = ""
while ($PCName.Trim() -eq "") {
    $PCName = Read-Host "  Как назвать этот ПК (например HomePC)"
}

# ── 1. Скачиваем ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  [1/3] Скачиваем $ZipName..." -ForegroundColor Cyan
$TmpZip     = "$env:TEMP\pcrc_release.zip"
$TmpExtract = "$env:TEMP\pcrc_extract"
Invoke-WebRequest -Uri $ReleaseUrl -OutFile $TmpZip -UseBasicParsing
Write-Host "        OK" -ForegroundColor Green

# ── 2. Распаковываем — ищем RemoteControl.exe где бы он ни лежал ─────────────
Write-Host "  [2/3] Распаковываем..." -ForegroundColor Cyan

if (Test-Path $TmpExtract) { Remove-Item $TmpExtract -Recurse -Force }
Expand-Archive -Path $TmpZip -DestinationPath $TmpExtract -Force

# Ищем папку где лежит RemoteControl.exe (может быть dist\ или корень)
$ExeFile = Get-ChildItem -Path $TmpExtract -Recurse -Filter "RemoteControl.exe" | Select-Object -First 1
if (-not $ExeFile) {
    Write-Host "  ОШИБКА: RemoteControl.exe не найден в архиве!" -ForegroundColor Red
    exit 1
}
$SourceDir = $ExeFile.DirectoryName

if (Test-Path $InstDir) { Remove-Item $InstDir -Recurse -Force }
New-Item -ItemType Directory -Path $InstDir | Out-Null
Copy-Item "$SourceDir\*" $InstDir -Recurse -Force

Remove-Item $TmpZip     -Force -ErrorAction SilentlyContinue
Remove-Item $TmpExtract -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "        OK  → $InstDir" -ForegroundColor Green

# ── 3. Config ────────────────────────────────────────────────────────────────
Write-Host "  [3/3] Создаём config.json..." -ForegroundColor Cyan
@{
    name       = $PCName.Trim()
    secret_key = $SecretKey
    server_url = $ServerUrl
    update_url = $UpdateUrl
} | ConvertTo-Json | Set-Content "$InstDir\config.json" -Encoding UTF8
Write-Host "        OK" -ForegroundColor Green

# ── Готово ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ==============================" -ForegroundColor Green
Write-Host "   Готово! Запускай RemoteControl.exe" -ForegroundColor White
Write-Host "  ==============================" -ForegroundColor Green
Write-Host ""
Write-Host "   ПК:    $PCName" -ForegroundColor White
Write-Host "   Папка: $InstDir" -ForegroundColor White
Write-Host ""

Start-Process explorer.exe $InstDir
