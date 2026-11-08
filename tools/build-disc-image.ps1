param(
    [string]$BuildRoot,
    [string]$TempRoot,
    [string]$SourceCuePath = 'roms\Wachenroeder (Japan).cue',
    [string]$SourceBinPath = 'roms\Wachenroeder (Japan).bin'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'build-paths.ps1')
$buildRoot = Resolve-TranslationBuildRoot $root $BuildRoot
$tempRoot = Resolve-TranslationTempRoot $root $TempRoot
$sourceCue = if ([IO.Path]::IsPathRooted($SourceCuePath)) { $SourceCuePath } else { Join-Path $root $SourceCuePath }
$sourceBin = if ([IO.Path]::IsPathRooted($SourceBinPath)) { $SourceBinPath } else { Join-Path $root $SourceBinPath }
$extracted = Join-Path $tempRoot 'disc-extracted'
$finalDir = Join-Path $buildRoot 'final'
$patchedTrack = Join-Path $extracted 'track01-patched.bin'
$toolDir = Join-Path $tempRoot 'disc-tool'
$tool = Join-Path $toolDir 'chd-disc-tool.exe'
$toolSource = Join-Path $PSScriptRoot 'chd-disc-tool.cpp'

foreach ($required in @($sourceCue, $sourceBin, $toolSource)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Missing disc build input: $required" }
}

function Resolve-VisualStudioPath {
    $vswhereCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'),
        (Join-Path ${env:ProgramFiles} 'Microsoft Visual Studio\Installer\vswhere.exe')
    )
    foreach ($vswhere in $vswhereCandidates) {
        if (-not (Test-Path -LiteralPath $vswhere)) { continue }
        $path = (& $vswhere -latest -products * -property installationPath 2>$null | Select-Object -First 1)
        if ($path -and (Test-Path -LiteralPath $path)) { return $path.Trim() }
    }
    return $null
}

function Resolve-CxxCompiler {
    $compiler = Get-Command clang-cl -ErrorAction SilentlyContinue
    if ($compiler) { return $compiler }
    $vsPath = Resolve-VisualStudioPath
    $candidates = @()
    if ($vsPath) {
        $candidates += Get-ChildItem -LiteralPath (Join-Path $vsPath 'VC\Tools\MSVC') -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'bin\Hostx64\x64\cl.exe' }
    }
    $compiler = Get-Command cl -ErrorAction SilentlyContinue
    if ($compiler) { $candidates += $compiler.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return Get-Command $candidate }
    }
    return $null
}

function Set-MsvcEnvironment([string] $compilerPath, [string] $vsPath) {
    $compilerItem = Get-Item -LiteralPath $compilerPath
    $msvcVersionRoot = $compilerItem.Directory.Parent.Parent.Parent.FullName
    $sdkRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'
    $sdkVersion = Get-ChildItem -LiteralPath (Join-Path $sdkRoot 'Include') -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            (Test-Path (Join-Path $_.FullName 'um')) -and
            (Test-Path (Join-Path $_.FullName 'ucrt')) -and
            (Test-Path (Join-Path $_.FullName 'shared'))
        } |
        Sort-Object { [version]$_.Name } -Descending |
        Select-Object -First 1
    if (-not $sdkVersion) { throw 'Windows SDK headers were not found for the helper build.' }
    $includePaths = @(
        (Join-Path $msvcVersionRoot 'include'),
        (Join-Path $sdkVersion.FullName 'ucrt'),
        (Join-Path $sdkVersion.FullName 'shared'),
        (Join-Path $sdkVersion.FullName 'um'),
        (Join-Path $sdkVersion.FullName 'winrt')
    )
    $libPaths = @(
        (Join-Path $msvcVersionRoot 'lib\x64'),
        (Join-Path $sdkRoot ("Lib\{0}\ucrt\x64" -f $sdkVersion.Name)),
        (Join-Path $sdkRoot ("Lib\{0}\um\x64" -f $sdkVersion.Name))
    )
    foreach ($path in $includePaths + $libPaths) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Helper build dependency was not found: $path" }
    }
    $compilerDir = $compilerItem.Directory.FullName
    $sdkBin = Join-Path $sdkRoot ("bin\{0}\x64" -f $sdkVersion.Name)
    $env:INCLUDE = (($includePaths + @($env:INCLUDE -split ';')) | Where-Object { $_ } | Select-Object -Unique) -join ';'
    $env:LIB = (($libPaths + @($env:LIB -split ';')) | Where-Object { $_ } | Select-Object -Unique) -join ';'
    $env:PATH = ((@($compilerDir, $sdkBin, (Join-Path $vsPath 'Common7\IDE')) + @($env:PATH -split ';')) | Where-Object { $_ } | Select-Object -Unique) -join ';'
}

function Ensure-RawHelper {
    New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
    if ((Test-Path -LiteralPath $tool -PathType Leaf) -and
        ((Get-Item -LiteralPath $tool).LastWriteTimeUtc -ge (Get-Item -LiteralPath $toolSource).LastWriteTimeUtc)) {
        Write-Output "Using cached raw-sector helper: $tool"
        return
    }

    $ymirRoot = Join-Path (Split-Path -Parent $root) 'Ymir-src-VDP'
    $ymirBuild = Join-Path $ymirRoot 'out\build\Full-x86-64-AVX2-MSVC-Release'
    $libchdrInclude = Join-Path $ymirRoot 'vendor\libchdr\libchdr\include'
    $chdrLib = Join-Path $ymirBuild 'vendor\libchdr\chdr-static.lib'
    $lzmaLib = Join-Path $ymirBuild 'vendor\libchdr\libchdr\deps\lzma-25.01\chdr-lzma.lib'
    $zstdLib = Join-Path $ymirBuild 'vcpkg_installed\x64-windows\lib\zstd.lib'
    $minizLib = Join-Path $ymirBuild 'vcpkg_installed\x64-windows\lib\miniz.lib'
    $compiler = Resolve-CxxCompiler
    if (-not $compiler) { throw 'Missing cached raw-sector helper and no C++ compiler is available to build it.' }
    $vsPath = Resolve-VisualStudioPath
    if ($compiler.Name -eq 'cl.exe') {
        if (-not $vsPath) { throw 'Visual Studio was not found for the helper build.' }
        Set-MsvcEnvironment $compiler.Source $vsPath
    }
    foreach ($required in @($libchdrInclude, $chdrLib, $lzmaLib, $zstdLib, $minizLib)) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Missing helper build dependency: $required" }
    }
    Write-Output "Building raw-sector helper once: $tool"
    $compileArgs = @(
        '/nologo', '/std:c++20', '/EHsc', '/MD', '/O2',
        "/I$libchdrInclude", "/Fe$tool", $toolSource,
        $chdrLib, $lzmaLib, $zstdLib, $minizLib,
        '/link', '/machine:x64'
    )
    & $compiler.Source @compileArgs
    if ($LASTEXITCODE -ne 0) { throw "Raw-sector helper compilation failed with exit code $LASTEXITCODE" }
}

Ensure-RawHelper

if (Test-Path -LiteralPath $extracted) {
    $resolvedExtracted = (Resolve-Path -LiteralPath $extracted).Path
    $resolvedTempRoot = [IO.Path]::GetFullPath($tempRoot).TrimEnd('\') + '\'
    if (-not $resolvedExtracted.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace extraction path outside temp root: $resolvedExtracted"
    }
    Remove-Item -LiteralPath $extracted -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $extracted, $finalDir | Out-Null

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required to build the BIN/CUE image.' }
$builder = Join-Path $PSScriptRoot 'build-disc-image.py'
& $python.Source $builder --root $root --build-root $buildRoot --source-cue $sourceCue --source-bin $sourceBin --extracted $extracted --patched-track $patchedTrack
if ($LASTEXITCODE -ne 0) { throw "CUE/BIN patch failed with exit code $LASTEXITCODE" }

& $tool verify-raw (Join-Path $extracted 'track01.bin')
if ($LASTEXITCODE -ne 0) { throw "Original data-track verification failed with exit code $LASTEXITCODE" }
for ($attempt = 1; $attempt -le 5; $attempt++) {
    & $tool rewrite-ecc $patchedTrack
    if ($LASTEXITCODE -eq 0) { break }
    if ($attempt -eq 5) { throw "ECC rewrite failed with exit code $LASTEXITCODE" }
    Start-Sleep -Milliseconds 200
}
& $tool verify-raw $patchedTrack
if ($LASTEXITCODE -ne 0) { throw "Patched data-track verification failed with exit code $LASTEXITCODE" }

# build-disc-image.py writes the combined BIN before ECC is rewritten above;
# refresh it once from the corrected data track while preserving all audio tracks.
$metadata = Get-Content -Raw (Join-Path $extracted 'metadata.json') | ConvertFrom-Json
$bin = Join-Path $finalDir 'Wachenroeder-CN.bin'
$audioTracks = @($metadata.tracks | Where-Object { $_.number -ne $metadata.tracks[0].number })
$out = [IO.File]::Open($bin, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try {
    $input = [IO.File]::OpenRead($patchedTrack)
    try { $input.CopyTo($out) } finally { $input.Dispose() }
    foreach ($track in $audioTracks) {
        $trackPath = Join-Path $extracted $track.file
        $input = [IO.File]::OpenRead($trackPath)
        try { $input.CopyTo($out) } finally { $input.Dispose() }
    }
} finally { $out.Dispose() }

$manifestPath = Join-Path $finalDir 'build-manifest.json'
$manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
$manifest.output.bin = $bin
$manifest.output | Add-Member -NotePropertyName bin_bytes -NotePropertyValue (Get-Item -LiteralPath $bin).Length -Force
$manifest | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 -LiteralPath $manifestPath
Write-Output "Built final BIN/CUE image in $finalDir from $sourceCue"
