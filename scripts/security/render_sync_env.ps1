Param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile,

    [string[]]$Keys = @(
        "SECRET_KEY",
        "OPS_TOKEN",
        "CRON_SECRET",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "MAIL_PASSWORD",
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_1",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_ROBERTO"
    ),

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $EnvFile)) {
    throw "Arquivo nao encontrado: $EnvFile"
}

if (-not $env:RENDER_API_TOKEN) {
    throw "Defina RENDER_API_TOKEN no ambiente para usar a API da Render."
}

if (-not $env:RENDER_SERVICE_ID) {
    throw "Defina RENDER_SERVICE_ID no ambiente para usar a API da Render."
}

$map = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^[A-Za-z_][A-Za-z0-9_]*=') {
        $parts = $_.Split('=', 2)
        $map[$parts[0]] = $parts[1]
    }
}

$envVars = @()
foreach ($k in $Keys) {
    if ($map.ContainsKey($k)) {
        $envVars += @{ key = $k; value = $map[$k] }
    }
}

if ($envVars.Count -eq 0) {
    throw "Nenhuma chave alvo encontrada em $EnvFile"
}

$serviceId = $env:RENDER_SERVICE_ID
$uri = "https://api.render.com/v1/services/$serviceId/env-vars"
$headers = @{
    Authorization = "Bearer $($env:RENDER_API_TOKEN)"
    Accept = "application/json"
}

if ($DryRun) {
    Write-Host "Dry-run: as seguintes chaves seriam enviadas para Render:"
    $envVars | ForEach-Object { Write-Host "- $($_.key)" }
    exit 0
}

$payloadAttempts = @(
    @{ label = "PUT wrapped"; method = "Put"; body = (@{ envVars = $envVars } | ConvertTo-Json -Depth 10 -Compress) },
    @{ label = "PUT array"; method = "Put"; body = ($envVars | ConvertTo-Json -Depth 10 -Compress) },
    @{ label = "PATCH wrapped"; method = "Patch"; body = (@{ envVars = $envVars } | ConvertTo-Json -Depth 10 -Compress) },
    @{ label = "PATCH array"; method = "Patch"; body = ($envVars | ConvertTo-Json -Depth 10 -Compress) }
)

$lastError = $null
foreach ($attempt in $payloadAttempts) {
    try {
        Invoke-RestMethod -Method $attempt.method -Uri $uri -Headers $headers -ContentType "application/json" -Body $attempt.body | Out-Null
        Write-Host "Atualizacao na Render concluida com tentativa: $($attempt.label)"
        $lastError = $null
        break
    }
    catch {
        $statusCode = $null
        $responseText = ""
        if ($_.Exception.Response) {
            try {
                $statusCode = [int]$_.Exception.Response.StatusCode
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $responseText = $reader.ReadToEnd()
                $reader.Dispose()
            }
            catch {
                $responseText = $_.Exception.Message
            }
        }
        $lastError = "Tentativa '$($attempt.label)' falhou" +
            ($(if ($statusCode) { " (HTTP $statusCode)" } else { "" })) +
            $(if ($responseText) { ": $responseText" } else { "" })
        Write-Host $lastError
    }
}

if ($lastError) {
    throw "Nao foi possivel sincronizar variaveis na Render. Ultimo erro: $lastError"
}

Write-Host "Variaveis sincronizadas na Render com sucesso."
Write-Host "Reinicie o servico no painel da Render para aplicar imediatamente, se necessario."
