# ============================================================
# ManyReach API Exploration Script (PowerShell 5.1 compatible)
# ============================================================
# Lance-le avec ta cle API en variable d'env :
#
#   $env:MANYREACH_API_KEY = "4976089a-8faf-45e3-9811-212fef8fb493"
#   .\explore_api.ps1
#
# Aucun mail n'est envoye, c'est read-only (GET seulement).
# Colle TOUTE la sortie dans le chat Claude.
# ============================================================

$ErrorActionPreference = "Continue"

$apiKey = $env:MANYREACH_API_KEY
if (-not $apiKey) {
    Write-Host "ERREUR : variable d'env MANYREACH_API_KEY non definie." -ForegroundColor Red
    Write-Host 'Lance dans la meme session PowerShell :' -ForegroundColor Yellow
    Write-Host '  $env:MANYREACH_API_KEY = "ta_cle"' -ForegroundColor Yellow
    Write-Host '  .\explore_api.ps1' -ForegroundColor Yellow
    exit 1
}

$baseUrls = @(
    "https://api.manyreach.com/v2",
    "https://api.manyreach.com/api/v2",
    "https://api.manyreach.com/v1",
    "https://api.manyreach.com/api/v1",
    "https://api.manyreach.com"
)

$authSchemes = @(
    @{ name = "Bearer"; header = @{ Authorization = "Bearer $apiKey" } },
    @{ name = "X-API-Key"; header = @{ "X-API-Key" = $apiKey } },
    @{ name = "Api-Key"; header = @{ "Api-Key" = $apiKey } },
    @{ name = "x-api-key"; header = @{ "x-api-key" = $apiKey } }
)

$paths = @(
    "/", "/campaigns", "/prospects", "/leads", "/replies",
    "/threads", "/messages", "/inbox", "/conversations",
    "/webhooks", "/tags", "/labels", "/me", "/account",
    "/users/me", "/v2/campaigns", "/api/campaigns"
)

Write-Host ""
Write-Host "=== Probing ManyReach API (read-only) ===" -ForegroundColor Cyan
Write-Host ""

$results = @()

foreach ($base in $baseUrls) {
    foreach ($auth in $authSchemes) {
        foreach ($path in $paths) {
            $url = "$base$path"
            $status = $null
            $bodySnippet = ""
            try {
                $resp = Invoke-WebRequest -Uri $url -Method GET -Headers $auth.header -TimeoutSec 5 -ErrorAction Stop -UseBasicParsing
                $status = $resp.StatusCode
                if ($resp.Content) {
                    $bodySnippet = $resp.Content.Substring(0, [Math]::Min(300, $resp.Content.Length))
                    $bodySnippet = $bodySnippet -replace "`r?`n", " "
                }
            } catch [System.Net.WebException] {
                if ($_.Exception.Response) {
                    $status = [int]$_.Exception.Response.StatusCode
                    try {
                        $stream = $_.Exception.Response.GetResponseStream()
                        $reader = New-Object System.IO.StreamReader($stream)
                        $body = $reader.ReadToEnd()
                        if ($body) {
                            $bodySnippet = $body.Substring(0, [Math]::Min(300, $body.Length))
                            $bodySnippet = $bodySnippet -replace "`r?`n", " "
                        }
                        $reader.Close()
                    } catch { }
                }
            } catch {
                # autres erreurs : ignore silencieusement (timeout, DNS, etc.)
            }

            if ($null -ne $status -and $status -ne 404) {
                $color = "Magenta"
                if ($status -lt 300) { $color = "Green" }
                elseif ($status -lt 400) { $color = "Yellow" }
                elseif ($status -eq 401 -or $status -eq 403) { $color = "DarkYellow" }

                Write-Host ("[{0}] {1,-12} {2}" -f $status, $auth.name, $url) -ForegroundColor $color
                if ($bodySnippet) {
                    $shortBody = $bodySnippet.Substring(0, [Math]::Min(150, $bodySnippet.Length))
                    Write-Host "       $shortBody" -ForegroundColor Gray
                }
                $results += [PSCustomObject]@{
                    status = $status
                    auth   = $auth.name
                    url    = $url
                    body   = $bodySnippet
                }
            }
        }
    }
}

Write-Host ""
Write-Host "=== Resume ===" -ForegroundColor Cyan
if ($results.Count -eq 0) {
    Write-Host "Aucun endpoint n'a repondu (tous 404 ou timeout)." -ForegroundColor Red
    Write-Host "Verifie ta cle ou demande la doc a ManyReach support." -ForegroundColor Yellow
} else {
    Write-Host ("Endpoints repondants : {0}" -f $results.Count) -ForegroundColor Green
    Write-Host ""
    Write-Host "Combinaisons (status 2xx en vert = ton couple base/auth qui marche):" -ForegroundColor Cyan
    $results | Sort-Object status | Format-Table -AutoSize status, auth, url
}

Write-Host ""
Write-Host ">>> COLLE TOUTE CETTE SORTIE DANS LE CHAT CLAUDE <<<" -ForegroundColor Green
Write-Host ""
