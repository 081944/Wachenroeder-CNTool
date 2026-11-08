$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$outputRoot = Join-Path $PSScriptRoot 'decoded-samples'

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;

public static class WachenroederResources {
    static int Read32(byte[] data, int pos) =>
        (data[pos] << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3];

    public static byte[][] SplitPac(byte[] data) {
        if (data.Length < 8) throw new FormatException("PAC is too short");
        int tableBytes = Read32(data, 0);
        if (tableBytes < 4 || tableBytes > data.Length || (tableBytes & 3) != 0)
            throw new FormatException("Invalid PAC offset table");
        int count = tableBytes / 4;
        var offsets = new int[count + 1];
        for (int i = 0; i < count; i++) offsets[i] = Read32(data, i * 4);
        offsets[count] = data.Length;
        if (offsets[0] != tableBytes) throw new FormatException("PAC data does not follow its table");
        var result = new byte[count][];
        for (int i = 0; i < count; i++) {
            if (offsets[i] < tableBytes || offsets[i + 1] < offsets[i] || offsets[i + 1] > data.Length)
                throw new FormatException("PAC offset outside file");
            result[i] = new byte[offsets[i + 1] - offsets[i]];
            Buffer.BlockCopy(data, offsets[i], result[i], 0, result[i].Length);
        }
        return result;
    }

    public static byte[] Decode(byte[] data) {
        if (data.Length < 3) throw new FormatException("Compressed stream is too short");
        int end = 3 + (data[0] << 16) + (data[1] << 8) + data[2];
        if (end > data.Length) throw new FormatException("Compressed length exceeds entry");
        int pos = 3;
        var output = new List<byte>();
        var left = new byte[256];
        var right = new byte[256];
        var stack = new byte[32];
        while (pos < end) {
            for (int i = 0; i < 256; i++) { left[i] = (byte)i; right[i] = 0; }
            if (pos >= end) throw new FormatException("Truncated dictionary control");
            int control = data[pos++];
            int symbol = control >= 128 ? control - 127 : 0;
            int runLeft = control >= 128 ? 1 : control + 1;
            while (symbol < 256) {
                if (pos >= end) throw new FormatException("Truncated dictionary entry");
                byte value = data[pos++];
                left[symbol] = value;
                if (value != symbol) {
                    if (pos >= end) throw new FormatException("Truncated dictionary pair");
                    right[symbol] = data[pos++];
                }
                symbol++;
                runLeft--;
                if (runLeft == 0 && symbol < 256) {
                    if (pos >= end) throw new FormatException("Truncated dictionary control");
                    control = data[pos++];
                    if (control >= 128) {
                        symbol += control - 127;
                        runLeft = 1;
                    } else {
                        runLeft = control + 1;
                    }
                    if (symbol > 256) throw new FormatException("Dictionary skip overflow");
                }
            }
            if (pos + 2 > end) throw new FormatException("Truncated block length");
            int count = (data[pos++] << 8) | data[pos++];
            for (int i = 0; i < count; i++) {
                if (pos >= end) throw new FormatException("Truncated block data");
                int depth = 1;
                stack[0] = data[pos++];
                while (depth != 0) {
                    byte value = stack[--depth];
                    if (left[value] == value) {
                        output.Add(value);
                    } else {
                        if (depth + 2 > stack.Length) throw new FormatException("Expansion stack exceeds SH-2 limit");
                        stack[depth++] = right[value];
                        stack[depth++] = left[value];
                    }
                    if (output.Count > 16 * 1024 * 1024) throw new FormatException("Decoded entry exceeds 16 MiB");
                }
            }
        }
        if (pos != end) throw new FormatException("Compressed entry length mismatch");
        return output.ToArray();
    }
}
'@

Add-Type -AssemblyName System.Drawing

function Write-GlyphSheet {
    param([byte[]]$Data, [string]$Path, [int]$Columns)
    if ($Data.Length % 32 -ne 0) { throw "Font length is not divisible by 32: $($Data.Length)" }
    $glyphCount = [int]($Data.Length / 32)
    $scale = 2
    $pitch = 16 * $scale + 1
    $rows = [int][Math]::Ceiling($glyphCount / $Columns)
    $bitmap = [System.Drawing.Bitmap]::new(($Columns * $pitch - 1), ($rows * $pitch - 1))
    try {
        $white = [System.Drawing.Color]::White
        $black = [System.Drawing.Color]::Black
        for ($glyph = 0; $glyph -lt $glyphCount; $glyph++) {
            $cellX = ($glyph % $Columns) * $pitch
            $cellY = [int][Math]::Floor($glyph / $Columns) * $pitch
            for ($y = 0; $y -lt 16; $y++) {
                $bits = (([int]$Data[$glyph * 32 + $y * 2]) -shl 8) -bor [int]$Data[$glyph * 32 + $y * 2 + 1]
                for ($x = 0; $x -lt 16; $x++) {
                    $color = if (($bits -band (0x8000 -shr $x)) -ne 0) { $white } else { $black }
                    for ($dy = 0; $dy -lt $scale; $dy++) {
                        for ($dx = 0; $dx -lt $scale; $dx++) {
                            $bitmap.SetPixel($cellX + $x * $scale + $dx, $cellY + $y * $scale + $dy, $color)
                        }
                    }
                }
            }
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $bitmap.Dispose()
    }
}

function Write-SmallGlyphSheet {
    param([byte[]]$Data, [string]$Path)
    if ($Data.Length % 16 -ne 0) { throw "Small font length is not divisible by 16: $($Data.Length)" }
    $columns = 16
    $count = [int]($Data.Length / 16)
    $rows = [int][Math]::Ceiling($count / $columns)
    $pitchX = 32
    $pitchY = 48
    $bitmap = [System.Drawing.Bitmap]::new($columns * $pitchX, $rows * $pitchY)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $font = [System.Drawing.Font]::new('Consolas', 8)
    try {
        for ($glyph = 0; $glyph -lt $count; $glyph++) {
            $left = ($glyph % $columns) * $pitchX + 8
            $top = [int][Math]::Floor($glyph / $columns) * $pitchY
            for ($y = 0; $y -lt 16; $y++) {
                $bits = [int]$Data[$glyph * 16 + $y]
                for ($x = 0; $x -lt 8; $x++) {
                    if (($bits -band (0x80 -shr $x)) -ne 0) {
                        for ($dy = 0; $dy -lt 2; $dy++) {
                            for ($dx = 0; $dx -lt 2; $dx++) {
                                $bitmap.SetPixel($left + $x * 2 + $dx, $top + $y * 2 + $dy, [System.Drawing.Color]::White)
                            }
                        }
                    }
                }
            }
            $graphics.DrawString(('{0:X3}' -f $glyph), $font,
                [System.Drawing.Brushes]::LightGray, ($glyph % $columns) * $pitchX,
                $top + 32)
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $font.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

$records = [System.Collections.Generic.List[object]]::new()
# Japanese disc only. The English patch is used only by the optional locator audits.
foreach ($version in @('japan')) {
    $inputDir = Join-Path $projectRoot "unpacked\$version"
    $outputDir = Join-Path $outputRoot $version
    [void](New-Item -ItemType Directory -Path $outputDir -Force)
    foreach ($name in @('ASCII.PAC', 'EVTTK01.PAC', 'MM01.PS')) {
        $source = Join-Path $inputDir $name
        $compressed = [IO.File]::ReadAllBytes($source)
        $entries = [WachenroederResources]::SplitPac($compressed)
        $selected = if ($name -eq 'ASCII.PAC') { @(0, 1) } elseif ($name -eq 'EVTTK01.PAC') { @(1) } else { @(2, 5, 9, 10) }
        foreach ($entry in $selected) {
            if ($entry -ge $entries.Length) { throw "Missing $name entry $entry" }
            $decoded = [WachenroederResources]::Decode($entries[$entry])
            $stem = '{0}.entry{1:d2}' -f $name, $entry
            $destination = Join-Path $outputDir "$stem.bin"
            [IO.File]::WriteAllBytes($destination, $decoded)
            $sha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($decoded))
            $records.Add([pscustomobject]@{
                Version = $version
                File = $name
                Entry = $entry
                CompressedBytes = $entries[$entry].Length
                DecodedBytes = $decoded.Length
                SHA256 = $sha256
            })
            if ($name -eq 'ASCII.PAC') {
                $columns = if ($entry -eq 0) { 64 } else { 24 }
                if ($entry -eq 1) {
                    Write-SmallGlyphSheet -Data $decoded -Path (Join-Path $outputDir "$stem.png")
                } else {
                    Write-GlyphSheet -Data $decoded -Path (Join-Path $outputDir "$stem.png") -Columns $columns
                }
            }
        }
    }
    foreach ($eventFile in Get-ChildItem -LiteralPath $inputDir -Filter 'EVTTK*.PAC') {
        if ($eventFile.Name -eq 'EVTTK01.PAC') { continue }
        $entries = [WachenroederResources]::SplitPac([IO.File]::ReadAllBytes($eventFile.FullName))
        if ($entries.Length -lt 2) { throw "Missing event layer in $($eventFile.Name)" }
        $decoded = [WachenroederResources]::Decode($entries[1])
        [IO.File]::WriteAllBytes((Join-Path $outputDir "$($eventFile.Name).entry01.bin"), $decoded)
        $records.Add([pscustomobject]@{
            Version = $version
            File = $eventFile.Name
            Entry = 1
            CompressedBytes = $entries[1].Length
            DecodedBytes = $decoded.Length
            SHA256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($decoded))
        })
    }
    if ($version -eq 'japan') {
        $vtkall = Join-Path $inputDir 'VTKALL.PAC'
        if (Test-Path -LiteralPath $vtkall) {
            $entries = [WachenroederResources]::SplitPac([IO.File]::ReadAllBytes($vtkall))
            for ($entry = 0; $entry -lt [Math]::Min(18, $entries.Length); $entry++) {
                $decoded = [WachenroederResources]::Decode($entries[$entry])
                [IO.File]::WriteAllBytes((Join-Path $outputDir ("VTKALL.PAC.entry{0:d2}.bin" -f $entry)), $decoded)
                $records.Add([pscustomobject]@{
                    Version = $version; File = 'VTKALL.PAC'; Entry = $entry
                    CompressedBytes = $entries[$entry].Length; DecodedBytes = $decoded.Length
                    SHA256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($decoded))
                })
            }
        }
    }
}
$records | Export-Csv -LiteralPath (Join-Path $outputRoot 'manifest.csv') -NoTypeInformation -Encoding UTF8
$records | Format-Table -AutoSize
