[CmdletBinding()]
param(
    [ValidateSet('apply', 'inspect')][string]$Phase = 'apply',
    [Parameter(Mandatory)][ValidateSet('default', 'refs-devdrive')][string]$Layout,
    [Parameter(Mandatory)][ValidateRange(1, 3)][int]$Trial,
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$ReportPath
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'storage_ab_core.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'storage_ab_native.psm1') -Force

$operations = New-AbNativeOperations -Python $Python
if ($Phase -eq 'inspect') {
    $context = & $operations.Context
    if (@(Get-AbContextErrors $context).Count -ne 0) {
        throw 'Storage inspection refuses an unsupported runner context.'
    }
    $result = Get-Content -LiteralPath $ReportPath -Raw | ConvertFrom-Json -AsHashtable
    if ($result.requested_layout -ne $Layout -or $result.trial -ne $Trial -or
        -not $result.comparable -or -not $result.applied) {
        throw 'Cannot inspect an unapplied or mismatched storage trial.'
    }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        $result['postcheck'] = & $operations.Inspect $result
    } catch {
        $result.comparable = $false
        $result.status = 'verification_failed'
        $result.reason = & $operations.SafeError $_
    } finally {
        $timer.Stop()
        $result.timings['postcheck_seconds'] = [math]::Round($timer.Elapsed.TotalSeconds, 6)
        $result.timings.total_seconds += $result.timings.postcheck_seconds
        $result | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $ReportPath -Encoding utf8
    }
    if (-not $result.comparable) { throw "Storage postcheck failed: $($result.reason)" }
    exit 0
}
$result = Invoke-AbStorage -Layout $Layout -Trial $Trial -Operations $operations

# A local/self-hosted refusal must not create files or alter its environment.
if ($result.context.github_actions -cne 'true' -or
    $result.context.runner_environment -cne 'github-hosted') {
    Write-Error 'Storage A/B refuses local or self-hosted execution.'
    exit 1
}
$parent = [IO.Path]::GetDirectoryName($ReportPath)
$null = New-Item -ItemType Directory -Path $parent -Force
$result | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $ReportPath -Encoding utf8
if (-not $result.comparable) {
    Write-Error "Storage layout not applied/comparable: $($result.reason)"
    exit 1
}

foreach ($name in @('UV_CACHE_DIR', 'PIP_CACHE_DIR', 'TEMP', 'TMP')) {
    $value = $result.environment[$name]
    if ($value -match '[\r\n]') { throw 'Invalid environment path.' }
    "$name=$value" | Out-File -LiteralPath $env:GITHUB_ENV -Encoding utf8 -Append
}
@(
    'comparable=true'
    "workspace_physical=$($result.paths.workspace.physical)"
    "wheelhouse=$($result.paths.wheelhouse.physical)"
) | Out-File -LiteralPath $env:GITHUB_OUTPUT -Encoding utf8 -Append
Write-Host "Applied $Layout on the verified hosted ARM64 runner."
