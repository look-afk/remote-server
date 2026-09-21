# PCRC Bootstrap — запускай на чистом компе, ничего заранее не нужно
# Использование (в PowerShell или CMD):
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/ТВО_ЮЗ/pcrc/main/bootstrap.ps1 | iex"

$ErrorActionPreference = "Stop"
$Repo    = "ТВО_ЮЗ/pcrc"           # <-- замени на свой GitHub user/repo
$Branch  = "main"
$InstDir = "$env:USERPROFILE\PCRC"

function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "  [$n] $msg" -ForegroundColor Cyan
}
function Write-Ok($msg)   { Write-Host "      OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "      >>  $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  ================================" -ForegroundColor Blue
Write-Host "   PCRC Bootstrap Installer" -ForegroundColor White
Write-Host "  ================================" -ForegroundColor Blue
Write-Host ""

# ── 1. Python ────────────────────────────────────────────────────────────────
Write-Step "1/5" "Проверяем Python..."

$py = $null
try { $py = (python --version 2>&1).ToString() } catch {}

if ($py -match "Python 3") {
    Write-Ok $py
} else {
    Write-Warn "Python не найден — устанавливаем через winget..."
    try {
        winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        # Обновляем PATH в текущей сессии
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + `
                    [System.Environment]::GetEnvironmentVariable("PATH","User")
        Write-Ok "Python установлен"
    } catch {
        # winget недоступен (старый Windows) — качаем installer напрямую
        Write-Warn "winget недоступен, скачиваем installer с python.org..."
        $PyInstaller = "$env:TEMP\python_installer.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe" `
            -OutFile $PyInstaller -UseBasicParsing
        Start-Process $PyInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1" -Wait
        Remove-Item $PyInstaller -Force
        # Обновляем PATH
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + `
                    [System.Environment]::GetEnvironmentVariable("PATH","User")
        Write-Ok "Python установлен"
    }
}

# ── 2. Скачиваем PCRC ────────────────────────────────────────────────────────
Write-Step "2/5" "Скачиваем PCRC с GitHub..."

$ZipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
$TmpZip = "$env:TEMP\pcrc_bootstrap.zip"
$TmpDir = "$env:TEMP\pcrc_bootstrap_extract"

Invoke-WebRequest -Uri $ZipUrl -OutFile $TmpZip -UseBasicParsing
Write-Ok "Архив скачан"

# ── 3. Распаковываем ─────────────────────────────────────────────────────────
Write-Step "3/5" "Распаковываем..."

if (Test-Path $TmpDir) { Remove-Item $TmpDir -Recurse -Force }
Expand-Archive -Path $TmpZip -DestinationPath $TmpDir -Force

$Extracted = Get-ChildItem $TmpDir | Select-Object -First 1
if (Test-Path $InstDir) { Remove-Item $InstDir -Recurse -Force }
Copy-Item $Extracted.FullName $InstDir -Recurse -Force

Remove-Item $TmpZip -Force -ErrorAction SilentlyContinue
Remove-Item $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Ok "Установлено в $InstDir"

# ── 4. Зависимости ───────────────────────────────────────────────────────────
Write-Step "4/5" "Устанавливаем зависимости pip..."

$Req = "$InstDir\requirements-client.txt"
if (Test-Path $Req) {
    python -m pip install --upgrade pip -q
    python -m pip install -r $Req -q
} else {
    python -m pip install -q websockets requests pillow pystray pywin32 `
        SpeechRecognition pyaudio psutil fastmcp
}
Write-Ok "Зависимости установлены"

# ── 5. Config ────────────────────────────────────────────────────────────────
Write-Step "5/5" "Создаём config.json..."

$Config   = "$InstDir\config.json"
$ConfigEx = "$InstDir\config.example.json"

if (-not (Test-Path $Config)) {
    if (Test-Path $ConfigEx) {
        Copy-Item $ConfigEx $Config
    } else {
        @{
            name       = "MyPC"
            secret_key = "измени_пароль"
            server_url = "wss://your-server.onrender.com"
            update_url = ""
        } | ConvertTo-Json | Set-Content $Config -Encoding UTF8
    }
}

# Открываем config.json в блокноте
Write-Warn "Открываю config.json — заполни secret_key и server_url"
Start-Process notepad.exe $Config

# ── Готово ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ================================" -ForegroundColor Green
Write-Host "   Установка завершена!" -ForegroundColor White
Write-Host "  ================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Папка:  $InstDir" -ForegroundColor White
Write-Host ""
Write-Host "  Запуск:" -ForegroundColor Cyan
Write-Host "    1. Заполни config.json (уже открыт в блокноте)" -ForegroundColor White
Write-Host "    2. Запусти dev.bat для теста" -ForegroundColor White
Write-Host "    3. Или .\build-client.ps1 для сборки exe" -ForegroundColor White
Write-Host ""

# Открываем папку в проводнике
Start-Process explorer.exe $InstDir
