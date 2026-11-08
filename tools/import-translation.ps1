param(
    [string]$BuildRoot,
    [string]$TempRoot
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'build-paths.ps1')
$buildRoot = Resolve-TranslationBuildRoot $root $BuildRoot
$tempRoot = Resolve-TranslationTempRoot $root $TempRoot
$output = Join-Path $tempRoot 'imported.json'
$null = New-Item -ItemType Directory -Path $tempRoot -Force
$records = @(& (Join-Path $PSScriptRoot 'validate-translation.ps1') -BuildRoot $buildRoot)
$translationRoot = Join-Path $root 'work/translation'
$inputHashes = [ordered]@{}
Get-ChildItem -LiteralPath (Join-Path $translationRoot 'events'), (Join-Path $translationRoot 'narrow') -File -Filter '*.txt' |
    Sort-Object FullName | ForEach-Object {
        # [IO.Path]::GetRelativePath needs .NET Core; this has to run on 5.1 too.
        $relative = $_.FullName.Substring($translationRoot.Length + 1).Replace('\', '/')
        $inputHashes[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
$payload = [ordered]@{
    format = 'wachenroeder-translation-intermediate-v1'
    source = 'japan'
    translation_input_sha256 = $inputHashes
    records = $records
}
Set-Content -LiteralPath $output -Value (ConvertTo-Json -InputObject $payload -Depth 8) -Encoding UTF8
Write-Host "Imported $($records.Count) translated records to $output. No game resource was written."
