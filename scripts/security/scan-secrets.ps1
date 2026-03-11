Param(
    [switch]$HistoryOnly,
    [switch]$DirOnly
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gitleaks -ErrorAction SilentlyContinue)) {
    Write-Error "gitleaks nao encontrado no PATH. Instale em https://github.com/gitleaks/gitleaks"
}

if (-not (Test-Path ".gitleaks.toml")) {
    Write-Error "Arquivo .gitleaks.toml nao encontrado na raiz do projeto."
}

if (-not $DirOnly) {
    Write-Host "[1/2] Scan de historico git"
    gitleaks git . --config .gitleaks.toml --redact --verbose
}

if (-not $HistoryOnly) {
    Write-Host "[2/2] Scan de diretorio de trabalho"
    gitleaks dir . --config .gitleaks.toml --redact --verbose
}

Write-Host "Scan finalizado sem vazamentos detectados."
