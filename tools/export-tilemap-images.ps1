$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sourceDir = Join-Path $root 'unpacked\japan'
$imageDir = Join-Path $root 'work\imageori\tilemaps'
$compositeDir = Join-Path $root 'work\imageori\previews\composites'
$manifestDir = Join-Path $root 'work\imageori\manifests'
$rawDir = Join-Path $root 'work\extracted\japan\graphics-decoded'
foreach ($dir in @($imageDir, $compositeDir, $manifestDir, $rawDir)) { [void](New-Item -ItemType Directory -Path $dir -Force) }
. (Join-Path $root 'analysis\decode-samples.ps1') | Out-Null
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

Add-Type -TypeDefinition @'
using System;

public sealed class SaturnTilemapInfo {
    public int PixelOffset, MapOffset, PaletteOffset, BitsPerPixel, TileCount;
    public int PaletteIndexZero;
    public double IndexZeroPercent;
    public bool TransparentIndexZero;
}

public static class SaturnTilemapPreview {
    static int BE32(byte[] raw, int pos) => (raw[pos] << 24) | (raw[pos + 1] << 16) | (raw[pos + 2] << 8) | raw[pos + 3];
    static int BE16(byte[] raw, int pos) => (raw[pos] << 8) | raw[pos + 1];

    public static SaturnTilemapInfo Probe(byte[] raw) {
        if (raw.Length < 0xA20) return null;
        int pix = BE32(raw, 0), map = BE32(raw, 4), pal = BE32(raw, 8);
        if (pix < 0x20 || map < 0x20 || pal < 0x20 || pix >= raw.Length ||
            map > raw.Length - 2048 || pal > raw.Length - 512 || pix == map) return null;
        int pixelEnd = raw.Length;
        if (map > pix) pixelEnd = Math.Min(pixelEnd, map);
        if (pal > pix) pixelEnd = Math.Min(pixelEnd, pal);
        int pixelBytes = pixelEnd - pix;
        if (pixelBytes < 128 || pixelBytes % 128 != 0) return null;
        int maxIndex = 0;
        bool odd = false, banked = false;
        for (int i = 0; i < 1024; i++) {
            int word = BE16(raw, map + i * 2);
            if ((word & 0x0C00) != 0) return null;
            int index = word & 1023;
            if (index > maxIndex) maxIndex = index;
            if ((index & 1) != 0) odd = true;
            if ((word & 0xF000) != 0) banked = true;
        }
        int bpp = odd || banked ? 4 : 8;
        if (maxIndex * 128 + (bpp == 8 ? 256 : 128) > pixelBytes) return null;
        var info = new SaturnTilemapInfo { PixelOffset = pix, MapOffset = map,
            PaletteOffset = pal, BitsPerPixel = bpp, TileCount = pixelBytes / (bpp == 8 ? 256 : 128) };
        info.PaletteIndexZero = BE16(raw, pal);
        int zeros = 0;
        for (int y = 0; y < 224; y += 8)
            for (int x = 0; x < 320; x += 8)
                if (PixelIndex(raw, info, x, y) == 0) zeros++;
        info.IndexZeroPercent = zeros * 100.0 / ((320 / 8) * (224 / 8));
        info.TransparentIndexZero = info.IndexZeroPercent > 50 && (info.PaletteIndexZero & 0x7FFF) != 0;
        return info;
    }

    static int PixelIndex(byte[] raw, SaturnTilemapInfo info, int x, int y) {
        int word = BE16(raw, info.MapOffset + ((y / 16) * 32 + x / 16) * 2);
        int px = x % 16, py = y % 16;
        int sample = ((py / 8) * 2 + px / 8) * 64 + (py % 8) * 8 + px % 8;
        int tile = info.PixelOffset + (word & 1023) * 128;
        if (info.BitsPerPixel == 8) return raw[tile + sample];
        byte packed = raw[tile + sample / 2];
        return ((word >> 12) & 15) * 16 + ((sample & 1) == 0 ? packed >> 4 : packed & 15);
    }

    public static byte[] RenderScreen(byte[] raw, SaturnTilemapInfo info) {
        var bgra = new byte[320 * 224 * 4];
        for (int y = 0; y < 224; y++) for (int x = 0; x < 320; x++) {
                int index = PixelIndex(raw, info, x, y);
                int color = BE16(raw, info.PaletteOffset + index * 2);
                int output = (y * 320 + x) * 4;
                // For 4bpp tiles, color zero is transparent within each
                // palette bank. The map word selects the bank, so bank 1's
                // zero nibble is not absolute palette index 0.
                if (info.TransparentIndexZero &&
                    (index == 0 || (info.BitsPerPixel == 4 && (index & 15) == 0))) continue;
                bgra[output] = (byte)(((color >> 10) & 31) * 255 / 31);
                bgra[output + 1] = (byte)(((color >> 5) & 31) * 255 / 31);
                bgra[output + 2] = (byte)((color & 31) * 255 / 31);
                bgra[output + 3] = 255;
        }
        return bgra;
    }
}
'@
Add-Type -AssemblyName System.Drawing

function Save-ScreenPng([byte[]]$raw, [SaturnTilemapInfo]$info, [string]$path) {
    $bgra = [SaturnTilemapPreview]::RenderScreen($raw, $info)
    $bitmap = [System.Drawing.Bitmap]::new(320, 224, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $rect = [System.Drawing.Rectangle]::new(0, 0, 320, 224)
    $bits = $bitmap.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::WriteOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        if ($bits.Stride -ne 1280) { throw "Unexpected PNG stride: $($bits.Stride)" }
        [Runtime.InteropServices.Marshal]::Copy($bgra, 0, $bits.Scan0, $bgra.Length)
    } finally { $bitmap.UnlockBits($bits) }
    try { $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
}

$records = [System.Collections.Generic.List[object]]::new()
$failures = [System.Collections.Generic.List[object]]::new()
foreach ($file in (Get-ChildItem -LiteralPath $sourceDir -File | Where-Object { $_.Extension -in @('.PAC', '.PS') } | Sort-Object Name)) {
    $source = [IO.File]::ReadAllBytes($file.FullName)
    $chunks = $null
    $direct = $false
    try { $chunks = [WachenroederResources]::SplitPac($source) }
    catch { $direct = $true; $chunks = ,$source }
    for ($entry = 0; $entry -lt $chunks.Length; $entry++) {
        if ($chunks[$entry].Length -lt 3) { continue }
        try { $raw = [WachenroederResources]::Decode([byte[]]$chunks[$entry]) }
        catch {
            $failures.Add([pscustomobject]@{ File = $file.Name; Entry = $entry; Reason = 'PS decode failed' })
            continue
        }
        $info = [SaturnTilemapPreview]::Probe($raw)
        if ($null -eq $info) { continue }
        $entryLabel = if ($direct) { 'raw' } else { 'entry{0:d2}' -f $entry }
        $stem = "$($file.Name).$entryLabel"
        $sourceImageDir = Join-Path $imageDir $file.Name
        [void](New-Item -ItemType Directory -Path $sourceImageDir -Force)
        $png = Join-Path $sourceImageDir "$stem.png"
        Save-ScreenPng $raw $info $png
        [IO.File]::WriteAllBytes((Join-Path $rawDir "$stem.bin"), $raw)
        $records.Add([pscustomobject]@{
            File = $file.Name; Entry = if ($direct) { 'raw' } else { $entry }
            BitsPerPixel = $info.BitsPerPixel; Tiles = $info.TileCount
            MapOffset = ('0x{0:X}' -f $info.MapOffset)
            PixelOffset = ('0x{0:X}' -f $info.PixelOffset)
            PaletteOffset = ('0x{0:X}' -f $info.PaletteOffset)
            DecodedBytes = $raw.Length
            SHA256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($raw))
            PaletteIndexZero = ('0x{0:X4}' -f $info.PaletteIndexZero)
            IndexZeroPercent = [math]::Round($info.IndexZeroPercent, 1)
            Transparency = if ($info.TransparentIndexZero) { 'index-0-inferred' } else { 'opaque-preview' }
            Png = "work/imageori/tilemaps/$($file.Name)/$stem.png"
        })
    }
}
$records | Export-Csv -LiteralPath (Join-Path $manifestDir 'tilemap-manifest.csv') -NoTypeInformation -Encoding UTF8
$failures | Export-Csv -LiteralPath (Join-Path $manifestDir 'decode-failures.csv') -NoTypeInformation -Encoding UTF8

$composites = foreach ($name in @('SENG00.PAC', 'SHOPBG.PAC', 'TITLEBG.PAC')) {
    $basePath = Join-Path (Join-Path $imageDir $name) "$name.entry00.png"
    $overlayPath = Join-Path (Join-Path $imageDir $name) "$name.entry01.png"
    $previewSourceDir = Join-Path $compositeDir $name
    [void](New-Item -ItemType Directory -Path $previewSourceDir -Force)
    $previewPath = Join-Path $previewSourceDir "$name.preview.png"
    $base = [System.Drawing.Image]::FromFile($basePath)
    $overlay = [System.Drawing.Image]::FromFile($overlayPath)
    $preview = [System.Drawing.Bitmap]::new(320, 224, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($preview)
    try {
        $graphics.DrawImage($base, 0, 0, 320, 224)
        $graphics.DrawImage($overlay, 0, 0, 320, 224)
        $preview.Save($previewPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose()
        $preview.Dispose()
        $overlay.Dispose()
        $base.Dispose()
    }
    [pscustomobject]@{
        File = $name
        Base = "work/imageori/tilemaps/$name/$name.entry00.png"
        Overlay = "work/imageori/tilemaps/$name/$name.entry01.png"
        Preview = "work/imageori/previews/composites/$name/$name.preview.png"
    }
}
$composites | Export-Csv -LiteralPath (Join-Path $manifestDir 'composite-manifest.csv') -NoTypeInformation -Encoding UTF8
Write-Host "Tilemap layers: $($records.Count); composite previews: $(@($composites).Count); decode failures: $($failures.Count)"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required to export TITLE.PAC nested images.' }
& $python.Source (Join-Path $root 'tools\export-title-images.py') --root $root
if ($LASTEXITCODE -ne 0) { throw "export-title-images.py failed with exit code $LASTEXITCODE" }

& $python.Source (Join-Path $root 'tools\export-image-candidates.py') --root $root
if ($LASTEXITCODE -ne 0) { throw "export-image-candidates.py failed with exit code $LASTEXITCODE" }
