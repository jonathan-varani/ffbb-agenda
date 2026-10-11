# Lancé par la tâche planifiée Windows « FFBB Agenda - scrape focus » (toutes les 3 h).
# Scrape les équipes suivies, commit + push si changements, puis déclenche la
# synchro des calendriers Google. Journal : logs\run_focus.log
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo
New-Item -ItemType Directory -Force logs | Out-Null
$log = Join-Path $repo "logs\run_focus.log"
if ((Test-Path $log) -and (Get-Item $log).Length -gt 500KB) { Move-Item $log "$log.old" -Force }
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content -Encoding utf8 $log }

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$python = "C:\Users\utilisateur\AppData\Local\Programs\Python\Python312\python.exe"

Log "=== début ==="
$out = & $python scrape_focus.py 2>&1 | Out-String
Log $out.Trim()
if ($LASTEXITCODE -ne 0) { Log "scrape en échec (code $LASTEXITCODE) : pas de commit"; exit 1 }

git add docs/calendars/teams
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { Log "aucun changement"; exit 0 }

git commit -q -m "Auto: mise à jour calendriers équipes suivies $((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')) UTC" 2>&1 | Out-Null
$pushed = $false
foreach ($i in 1..3) {
    git push -q 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $pushed = $true; break }
    Log "push rejeté ($i/3), rebase"
    git pull -q --rebase origin main 2>&1 | Out-Null
}
if (-not $pushed) { Log "ÉCHEC du push"; exit 1 }
Log "push OK"

gh workflow run sync-google.yml 2>&1 | Out-Null
Log "sync Google déclenchée (code $LASTEXITCODE)"
