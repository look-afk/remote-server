$ErrorActionPreference = "Stop"

$Repo      = "look-afk/remote-server"
$ZipName   = "dist.zip"
$SecretKey = "Zafarjon1224"
$ServerUrl = "wss://remote-server-mr8v.onrender.com"
$UpdateUrl = "https://raw.githubusercontent.com/look-afk/pcrc-versions/main"
$InstDir   = "$env:USERPROFILE\PCRC"
$ReleaseUrl = "https://github.com/$Repo/releases/latest/download/$ZipName"

Write-Host ""
Write-Host "  PCRC Installer" -ForegroundColor Cyan
Write-Host ""

$PCName = ""
while ($PCName.Trim() -eq "") {
    $PCName = Read-Host "  PC name (e.g. HomePC, WorkPC)"
}

Write-Host ""
Write-Host "  [1/3] Downloading..." -ForegroundColor Yellow
$TmpZip     = "$env:TEMP\pcrc_release.zip"
$TmpExtract = "$env:TEMP\pcrc_extract"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $ReleaseUrl -OutFile $TmpZip -UseBasicParsing
Write-Host "        OK" -ForegroundColor Green

Write-Host "  [2/3] Extracting..." -ForegroundColor Yellow
if (Test-Path $TmpExtract) { Remove-Item $TmpExtract -Recurse -Force }
Expand-Archive -Path $TmpZip -DestinationPath $TmpExtract -Force

$ExeFile = Get-ChildItem -Path $TmpExtract -Recurse -Filter "RemoteControl.exe" | Select-Object -First 1
if (-not $ExeFile) {
    Write-Host "  ERROR: RemoteControl.exe not found in archive!" -ForegroundColor Red
    exit 1
}
$SourceDir = $ExeFile.DirectoryName

if (Test-Path $InstDir) { Remove-Item $InstDir -Recurse -Force }
New-Item -ItemType Directory -Path $InstDir | Out-Null
Copy-Item "$SourceDir\*" $InstDir -Recurse -Force

Remove-Item $TmpZip     -Force -ErrorAction SilentlyContinue
Remove-Item $TmpExtract -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "        OK -> $InstDir" -ForegroundColor Green

Write-Host "  [3/3] Creating config.json..." -ForegroundColor Yellow
@{
    name       = $PCName.Trim()
    secret_key = $SecretKey
    server_url = $ServerUrl
    update_url = $UpdateUrl
} | ConvertTo-Json | Set-Content "$InstDir\config.json" -Encoding UTF8
Write-Host "        OK" -ForegroundColor Green

Write-Host ""
Write-Host "  Done! Run RemoteControl.exe" -ForegroundColor Green
Write-Host "  PC: $PCName" -ForegroundColor White
Write-Host "  Folder: $InstDir" -ForegroundColor White
Write-Host ""

Start-Process explorer.exe $InstDir
