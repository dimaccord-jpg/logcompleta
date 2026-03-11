Param(
    [string[]]$EnvFiles = @("app/.env.dev", "app/.env.homolog"),
    [string[]]$SetValues = @(),
    [switch]$AutoOnly,
    [switch]$DryRun,
    [switch]$InsertMissing
)

$ErrorActionPreference = "Stop"

$scriptPath = "./scripts/security/rotate_secrets.py"
if (-not (Test-Path $scriptPath)) {
    Write-Error "Script nao encontrado: $scriptPath"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python nao encontrado no PATH."
}

$argsList = @($scriptPath)
foreach ($file in $EnvFiles) {
    $argsList += @("--env-file", $file)
}
foreach ($item in $SetValues) {
    $argsList += @("--set", $item)
}
if ($AutoOnly) {
    $argsList += "--auto-only"
}
if ($DryRun) {
    $argsList += "--dry-run"
}
if ($InsertMissing) {
    $argsList += "--insert-missing"
}

& python @argsList
