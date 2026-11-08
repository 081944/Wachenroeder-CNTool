param(
    [string]$BuildRoot,
    [string]$TempRoot,
    [ValidateSet('8x8', '8x12')]
    [string]$FontMode = '8x12'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'build-paths.ps1')
if ($FontMode -eq '8x12') {
    if ([string]::IsNullOrWhiteSpace($BuildRoot)) { $BuildRoot = 'work/build-8x12' }
    if ([string]::IsNullOrWhiteSpace($TempRoot)) { $TempRoot = 'temp/build-8x12' }
}
$buildRoot = Resolve-TranslationBuildRoot $root $BuildRoot
$tempRoot = Resolve-TranslationTempRoot $root $TempRoot
$resourceBuildRoot = $buildRoot
$resourceTempRoot = $tempRoot
if ($FontMode -eq '8x12') {
    $resourceBuildRoot = Join-Path $buildRoot 'baseline'
    $resourceTempRoot = Join-Path $tempRoot 'baseline'
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required for the full translation build.' }

function Invoke-PowerShellStep([string] $scriptName, [string] $stepBuildRoot = $resourceBuildRoot, [string] $stepTempRoot = $resourceTempRoot) {
    Write-Host "=== $scriptName ==="
    $stepArguments = @('-NoProfile', '-File', (Join-Path $PSScriptRoot $scriptName),
                       '-BuildRoot', $stepBuildRoot, '-TempRoot', $stepTempRoot)
    if ($scriptName -eq 'validate-translation.ps1') { $stepArguments += '-SummaryOnly' }
    if ($scriptName -eq 'build-translation-pacs.ps1') { $stepArguments += @('-FontMode', $FontMode) }
    & pwsh @stepArguments
    if ($LASTEXITCODE -ne 0) { throw "$scriptName failed with exit code $LASTEXITCODE" }
}

Write-Host "Building full translation with $FontMode small font."
Write-Host '=== Checking event source displays against original glyph maps ==='
& $python.Source (Join-Path $PSScriptRoot 'refresh-event-source-displays.py') `
    --root $root --backup-dir (Join-Path $tempRoot 'catalog-migration-backups')
if ($LASTEXITCODE -ne 0) { throw "Event source display refresh failed with exit code $LASTEXITCODE" }

Write-Host '=== Checking shared text catalogs and migrating outdated boundaries ==='
& $python.Source (Join-Path $PSScriptRoot 'export-mm-narrow-byte-text.py') `
    --root $root --ensure-current --backup-dir (Join-Path $tempRoot 'catalog-migration-backups')
if ($LASTEXITCODE -ne 0) { throw "Shared text catalog refresh failed with exit code $LASTEXITCODE" }

Write-Host '=== Checking MM equipment catalog ==='
& $python.Source (Join-Path $PSScriptRoot 'export-mm-equipment-text.py')
if ($LASTEXITCODE -ne 0) { throw "MM equipment catalog refresh failed with exit code $LASTEXITCODE" }

& $python.Source (Join-Path $PSScriptRoot 'build-chinese-fonts.py') --root $root --build-root $resourceBuildRoot --font-mode $FontMode
if ($LASTEXITCODE -ne 0) { throw "build-chinese-fonts.py failed with exit code $LASTEXITCODE" }

Invoke-PowerShellStep 'validate-translation.ps1'
Invoke-PowerShellStep 'import-translation.ps1'
Invoke-PowerShellStep 'repack-event-text.ps1'
Invoke-PowerShellStep 'build-translation-pacs.ps1'
if ($FontMode -eq '8x12') {
    Write-Host '=== Building resident 8x12 font and runtime hooks ==='
    & $python.Source (Join-Path $PSScriptRoot 'build-resident-small-font-probe.py') `
        --output (Join-Path $tempRoot 'font-8x12') --build-root $buildRoot `
        --baseline-build-root $resourceBuildRoot --temp-root $resourceTempRoot
    if ($LASTEXITCODE -ne 0) { throw "8x12 font build failed with exit code $LASTEXITCODE" }
}
Invoke-PowerShellStep 'build-disc-image.ps1' $buildRoot $tempRoot
Write-Host '=== Verifying resources read back from the final BIN ==='
& $python.Source (Join-Path $PSScriptRoot 'verify-translation-build.py') `
    --root $root --build-root $buildRoot --font-mode $FontMode
if ($LASTEXITCODE -ne 0) { throw "Final BIN verification failed with exit code $LASTEXITCODE" }

Write-Output "Full translation package built under $buildRoot\final; temporary files are under $tempRoot"
