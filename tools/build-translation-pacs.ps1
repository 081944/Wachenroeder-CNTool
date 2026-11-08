param(
    [string]$BuildRoot,
    [string]$TempRoot,
    [ValidateSet('8x8', '8x12')]
    [string]$FontMode = '8x8'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'build-paths.ps1')
$buildRoot = Resolve-TranslationBuildRoot $root $BuildRoot
$tempRoot = Resolve-TranslationTempRoot $root $TempRoot
if (Get-Command python -ErrorAction SilentlyContinue) {
    $python = 'python'
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = 'py'
} else {
    throw 'Python 3 is required to build translated PAC files.'
}
& $python (Join-Path $PSScriptRoot 'build-translation-pacs.py') --root $root --build-root $buildRoot --temp-root $tempRoot --font-mode $FontMode
if ($LASTEXITCODE -ne 0) { throw "PAC build failed with exit code $LASTEXITCODE" }
