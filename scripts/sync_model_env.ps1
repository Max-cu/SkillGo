param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Target
)

$keys = @(
    "SKILLGO_MODEL_BASE_URL",
    "SKILLGO_MODEL_API_KEY",
    "SKILLGO_MODEL_NAME",
    "SKILLGO_MODEL_TIMEOUT_SECONDS",
    "SKILLGO_MODEL_TEMPERATURE",
    "SKILLGO_MODEL_JSON_MODE",
    "SKILLGO_MODEL_TLS_VERIFY"
)

function Read-EnvValues([string]$Path) {
    $result = @{}
    foreach ($line in [System.IO.File]::ReadAllLines((Resolve-Path -LiteralPath $Path))) {
        if ($line -match '^([^#=][^=]*)=(.*)$') {
            $result[$matches[1].Trim()] = $matches[2]
        }
    }
    return $result
}

$sourceValues = Read-EnvValues $Source
$targetPath = (Resolve-Path -LiteralPath $Target).Path
$lines = [System.Collections.Generic.List[string]]::new()
$seen = @{}

foreach ($line in [System.IO.File]::ReadAllLines($targetPath)) {
    if ($line -match '^([^#=][^=]*)=(.*)$') {
        $key = $matches[1].Trim()
        if ($keys -contains $key -and $sourceValues.ContainsKey($key)) {
            $lines.Add("$key=$($sourceValues[$key])")
            $seen[$key] = $true
            continue
        }
    }
    $lines.Add($line)
}

foreach ($key in $keys) {
    if (-not $seen.ContainsKey($key) -and $sourceValues.ContainsKey($key)) {
        $lines.Add("$key=$($sourceValues[$key])")
    }
}

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($targetPath, $lines, $utf8NoBom)

$configured = @{}
foreach ($key in $keys) {
    $configured[$key] = $sourceValues.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace($sourceValues[$key])
}
$configured | ConvertTo-Json
