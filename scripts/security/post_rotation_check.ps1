Param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$OpsToken
)

$ErrorActionPreference = "Stop"

function Invoke-CheckedGet {
    Param(
        [string]$Url,
        [hashtable]$Headers
    )

    $resp = Invoke-WebRequest -Uri $Url -Headers $Headers -Method Get -UseBasicParsing
    if ($resp.StatusCode -lt 200 -or $resp.StatusCode -ge 300) {
        throw "Falha em $Url (HTTP $($resp.StatusCode))"
    }
    return $resp.Content
}

$base = $BaseUrl.TrimEnd('/')
$opsHeaders = @{ "X-Ops-Token" = $OpsToken }

Write-Host "[1/3] Health check"
$health = Invoke-CheckedGet -Url "$base/health" -Headers @{}
Write-Host $health

Write-Host "[2/3] OAuth diagnostics"
$oauth = Invoke-CheckedGet -Url "$base/oauth-diagnostics" -Headers $opsHeaders
Write-Host $oauth

Write-Host "[3/3] Login endpoint smoke check"
$login = Invoke-WebRequest -Uri "$base/login" -Method Get -UseBasicParsing
if ($login.StatusCode -lt 200 -or $login.StatusCode -ge 400) {
    throw "Falha no endpoint /login (HTTP $($login.StatusCode))"
}

Write-Host "Validacao pos-rotacao concluida com sucesso."
