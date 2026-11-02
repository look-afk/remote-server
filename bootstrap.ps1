# PCRC Bootstrap — запускай на чистом компе, ничего заранее не нужно
# Использование (в PowerShell или CMD):
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/look-afk/pcrc-client/master/bootstrap.ps1 | iex"
#
# FIX (safety): this version never deletes an existing install outright.
# It stages the new files, backs up the old install (including config.json,
# which holds your secret_key), swaps atomically, and rolls back to the
# previous version automatically if anything fails. Use uninstall.ps1
# (written into the install folder) to remove PCRC on purpose.

$ErrorActionPreference = "Stop"
$Repo     = "look-afk/pcrc-client"   # real client repo (was a placeholder before)
$Branch   = "master"
$InstDir  = "$env:USERPROFILE\PCRC"
$BackupRoot = "$env:LOCALAPPDATA\PCRC_Backups"
$Timestamp  = Get-Date -Format "yyyyMMdd_HHmmss"

function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "  [$n] $msg" -ForegroundColor Cyan
}
function Write-Ok($msg)   { Write-Host "      OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "      >>  $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "      !!  $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "  ================================" -ForegroundColor Blue
Write-Host "   PCRC Bootstrap Installer" -ForegroundColor White
Write-Host "  ================================" -ForegroundColor Blue
Write-Host ""

$PreviousBackupDir = $null
$StageDir  = "$env:TEMP\pcrc_stage_$Timestamp"
$RenamedOldDir = "$InstDir`_old_$Timestamp"
$SwapDone = $false

function Restore-OnFailure {
    # Best-effort rollback: put the previous install back exactly where it was.
    if ($SwapDone -and (Test-Path $RenamedOldDir)) {
        Write-Err "Install failed after the swap — rolling back to the previous version."
        try {
            if (Test-Path $InstDir) { Remove-Item $InstDir -Recurse -Force -ErrorAction SilentlyContinue }
            Rename-Item -Path $RenamedOldDir -NewName (Split-Path $InstDir -Leaf) -ErrorAction Stop
            Write-Ok "Rolled back. Your previous install at $InstDir is untouched."
        } catch {
            Write-Err "Automatic rollback failed. Your previous install is safe at: $RenamedOldDir"
            Write-Err "You can restore it manually by renaming that folder back to 'PCRC'."
        }
    } elseif (Test-Path $RenamedOldDir) {
        Write-Err "Install failed before completing. Your previous install is untouched at $InstDir"
        Write-Err "(a stray backup copy also exists at $RenamedOldDir — safe to delete it later)"
    } else {
        Write-Err "Install failed. No existing install was found, so nothing needed rolling back."
    }
    if (Test-Path $StageDir) { Remove-Item $StageDir -Recurse -Force -ErrorAction SilentlyContinue }
}

try {
    # ── 1. Python ────────────────────────────────────────────────────────────
    Write-Step "1/6" "Проверяем Python..."

    $py = $null
    try { $py = (python --version 2>&1).ToString() } catch {}

    if ($py -match "Python 3") {
        Write-Ok $py
    } else {
        Write-Warn "Python не найден — устанавливаем через winget..."
        try {
            winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + `
                        [System.Environment]::GetEnvironmentVariable("PATH","User")
            Write-Ok "Python установлен"
        } catch {
            Write-Warn "winget недоступен, скачиваем installer с python.org..."
            $PyInstaller = "$env:TEMP\python_installer_$Timestamp.exe"
            Invoke-WebRequest "https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe" `
                -OutFile $PyInstaller -UseBasicParsing
            Start-Process $PyInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1" -Wait
            Remove-Item $PyInstaller -Force -ErrorAction SilentlyContinue
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + `
                        [System.Environment]::GetEnvironmentVariable("PATH","User")
            Write-Ok "Python установлен"
        }
    }

    # ── 2. Скачиваем PCRC в staging (НЕ в $InstDir) ─────────────────────────
    Write-Step "2/6" "Скачиваем PCRC с GitHub..."

    $ZipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
    $TmpZip = "$env:TEMP\pcrc_bootstrap_$Timestamp.zip"
    $TmpExtract = "$env:TEMP\pcrc_extract_$Timestamp"

    Invoke-WebRequest -Uri $ZipUrl -OutFile $TmpZip -UseBasicParsing
    if (-not (Test-Path $TmpZip) -or (Get-Item $TmpZip).Length -lt 1024) {
        throw "Downloaded archive looks empty or too small — aborting before touching any existing install."
    }
    Write-Ok "Архив скачан ($((Get-Item $TmpZip).Length) bytes)"

    # ── 3. Распаковываем в staging-папку ────────────────────────────────────
    Write-Step "3/6" "Распаковываем во временную папку..."

    Expand-Archive -Path $TmpZip -DestinationPath $TmpExtract -Force
    $Extracted = Get-ChildItem $TmpExtract | Select-Object -First 1
    if (-not $Extracted) { throw "Archive extracted but contained no files." }

    New-Item -ItemType Directory -Path $StageDir -Force | Out-Null
    Copy-Item "$($Extracted.FullName)\*" $StageDir -Recurse -Force
    Write-Ok "Распаковано во временную папку (ещё не установлено)"

    # ── 4. Бэкап текущей установки (если есть) ─────────────────────────────
    Write-Step "4/6" "Делаем резервную копию текущей установки (если есть)..."

    $OldConfig = $null
    if (Test-Path $InstDir) {
        New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
        $PreviousBackupDir = Join-Path $BackupRoot "backup_$Timestamp"
        New-Item -ItemType Directory -Path $PreviousBackupDir -Force | Out-Null
        Copy-Item "$InstDir\*" $PreviousBackupDir -Recurse -Force -ErrorAction Stop
        Write-Ok "Резервная копия: $PreviousBackupDir"

        $ExistingConfigPath = Join-Path $InstDir "config.json"
        if (Test-Path $ExistingConfigPath) {
            $OldConfig = Get-Content $ExistingConfigPath -Raw
            Write-Ok "config.json (с твоим secret_key) сохранён для переноса в новую версию"
        }

        # Keep only the 5 most recent backups so disk usage doesn't grow forever.
        Get-ChildItem $BackupRoot -Directory |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip 5 |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Ok "Предыдущей установки не найдено — это первая установка."
    }

    # ── 5. Атомарная замена: переименование, а не удаление ──────────────────
    Write-Step "5/6" "Устанавливаем новую версию..."

    if (Test-Path $InstDir) {
        Rename-Item -Path $InstDir -NewName (Split-Path $RenamedOldDir -Leaf) -ErrorAction Stop
    }
    $SwapDone = $true  # from here on, a failure must trigger rollback

    # FIX: Rename-Item only renames within the SAME parent directory — it
    # cannot move $StageDir (under %TEMP%) to $InstDir (under %USERPROFILE%).
    # That bug left the new files stranded in %TEMP% while the script went on
    # to write config.json into the now-missing $InstDir, which is exactly
    # what caused "Could not find a part of the path ...\config.json".
    # Move-Item is the correct cmdlet for moving across directories.
    Move-Item -Path $StageDir -Destination $InstDir -ErrorAction Stop

    # Carry the old config.json forward so the install keeps working with the
    # same secret_key / server_url instead of silently reverting to a template.
    # FIX: Set-Content -Encoding UTF8 on Windows PowerShell writes a UTF-8 BOM,
    # which makes Python's json.loads() throw (silently swallowed by the
    # client's except/pass), so it never actually reads name/secret_key back.
    # [System.IO.File]::WriteAllText with UTF8Encoding($false) writes no BOM.
    $NoBomUtf8 = New-Object System.Text.UTF8Encoding($false)
    if ($OldConfig) {
        [System.IO.File]::WriteAllText((Join-Path $InstDir "config.json"), $OldConfig, $NoBomUtf8)
    } elseif (-not (Test-Path (Join-Path $InstDir "config.json"))) {
        $ConfigEx = Join-Path $InstDir "config.example.json"
        if (Test-Path $ConfigEx) {
            Copy-Item $ConfigEx (Join-Path $InstDir "config.json")
        } else {
            $defaultJson = @{
                name       = "MyPC"
                secret_key = "измени_пароль"
                server_url = "wss://your-server.onrender.com"
                update_url = ""
            } | ConvertTo-Json
            [System.IO.File]::WriteAllText((Join-Path $InstDir "config.json"), $defaultJson, $NoBomUtf8)
        }
    }
    Write-Ok "Установлено в $InstDir"

    # Drop in the uninstall script every time, so it always matches this version.
    $UninstallContent = @'
# PCRC Uninstaller
$ErrorActionPreference = "Stop"
$InstDir = "$env:USERPROFILE\PCRC"

Write-Host "This will remove $InstDir" -ForegroundColor Yellow
$confirm = Read-Host "Type YES to continue"
if ($confirm -ne "YES") { Write-Host "Cancelled."; exit }

# Best-effort: offer to save config.json (holds your secret_key) before deleting.
$cfg = Join-Path $InstDir "config.json"
if (Test-Path $cfg) {
    $save = Read-Host "Save a copy of config.json to your Desktop first? (Y/N)"
    if ($save -match "^[Yy]") {
        Copy-Item $cfg "$env:USERPROFILE\Desktop\PCRC_config_backup.json" -Force
        Write-Host "Saved to Desktop\PCRC_config_backup.json" -ForegroundColor Green
    }
}

# Stop a running client if one is active (best-effort, ignores errors).
Get-Process -Name "RemoteControl" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name "python","pythonw" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*PCRC*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue

Remove-Item $InstDir -Recurse -Force
Write-Host "PCRC removed from $InstDir" -ForegroundColor Green
Write-Host "Backups (if any) remain at $env:LOCALAPPDATA\PCRC_Backups — delete manually if not needed." -ForegroundColor Yellow
'@
    Set-Content -Path (Join-Path $InstDir "uninstall.ps1") -Value $UninstallContent -Encoding UTF8

    # ── 6. Зависимости (non-fatal: a pip failure should not trigger rollback
    #        of a successfully-installed copy of the source) ───────────────
    Write-Step "6/6" "Устанавливаем зависимости pip..."

    try {
        $Req = Join-Path $InstDir "requirements-client.txt"
        if (Test-Path $Req) {
            python -m pip install --upgrade pip -q
            python -m pip install -r $Req -q
        } else {
            python -m pip install -q websockets requests pillow pystray pywin32 `
                SpeechRecognition pyaudio psutil fastmcp
        }
        Write-Ok "Зависимости установлены"
    } catch {
        Write-Warn "Не удалось установить часть зависимостей pip: $($_.Exception.Message)"
        Write-Warn "Файлы PCRC установлены корректно — доустанови зависимости вручную при необходимости:"
        Write-Warn "  python -m pip install -r `"$InstDir\requirements-client.txt`""
    }

    Remove-Item $TmpZip -Force -ErrorAction SilentlyContinue
    Remove-Item $TmpExtract -Recurse -Force -ErrorAction SilentlyContinue

    Write-Warn "Открываю config.json — проверь secret_key и server_url"
    Start-Process notepad.exe (Join-Path $InstDir "config.json")

    Write-Host ""
    Write-Host "  ================================" -ForegroundColor Green
    Write-Host "   Установка завершена!" -ForegroundColor White
    Write-Host "  ================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Папка:    $InstDir" -ForegroundColor White
    if ($PreviousBackupDir) {
        Write-Host "  Бэкап предыдущей версии: $PreviousBackupDir" -ForegroundColor White
    }
    Write-Host "  Удаление: powershell -ExecutionPolicy Bypass -File `"$InstDir\uninstall.ps1`"" -ForegroundColor White
    Write-Host ""
    Write-Host "  Запуск:" -ForegroundColor Cyan
    Write-Host "    1. Заполни config.json (уже открыт в блокноте)" -ForegroundColor White
    Write-Host "    2. Запусти dev.bat для теста" -ForegroundColor White
    Write-Host "    3. Или .\build-client.ps1 для сборки exe" -ForegroundColor White
    Write-Host ""

    Start-Process explorer.exe $InstDir

    # Success — the renamed old copy is no longer needed as a rollback source,
    # but keep it briefly under the backup root instead of deleting outright.
    if (Test-Path $RenamedOldDir) {
        if (-not (Test-Path $BackupRoot)) { New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null }
        $FinalOldBackup = Join-Path $BackupRoot "preswap_$Timestamp"
        # FIX: same Rename-Item-across-directories bug as above — $RenamedOldDir
        # lives under %USERPROFILE% and $BackupRoot under %LOCALAPPDATA%, so this
        # must be a move, not a rename.
        Move-Item -Path $RenamedOldDir -Destination $FinalOldBackup -ErrorAction SilentlyContinue
    }

} catch {
    Write-Err "Ошибка установки: $($_.Exception.Message)"
    Restore-OnFailure
    Write-Host ""
    Write-Host "  Установка не завершена. Ничего не сломано — смотри сообщения выше." -ForegroundColor Yellow
    exit 1
}
