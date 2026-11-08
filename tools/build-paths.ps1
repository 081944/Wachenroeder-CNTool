Set-StrictMode -Version Latest

function Get-DefaultTranslationBuildRoot([string]$ProjectRoot) {
    return Join-Path $ProjectRoot 'work\build'
}

function Get-DefaultTranslationTempRoot([string]$ProjectRoot) {
    return Join-Path $ProjectRoot 'temp'
}

function Resolve-TranslationBuildRoot([string]$ProjectRoot, [string]$RequestedRoot) {
    $path = if ([string]::IsNullOrWhiteSpace($RequestedRoot)) {
        Get-DefaultTranslationBuildRoot $ProjectRoot
    } else {
        if ([IO.Path]::IsPathRooted($RequestedRoot)) { $RequestedRoot } else { Join-Path $ProjectRoot $RequestedRoot }
    }
    return [IO.Path]::GetFullPath($path)
}

function Resolve-TranslationTempRoot([string]$ProjectRoot, [string]$RequestedRoot) {
    $path = if ([string]::IsNullOrWhiteSpace($RequestedRoot)) {
        Get-DefaultTranslationTempRoot $ProjectRoot
    } else {
        if ([IO.Path]::IsPathRooted($RequestedRoot)) { $RequestedRoot } else { Join-Path $ProjectRoot $RequestedRoot }
    }
    return [IO.Path]::GetFullPath($path)
}
