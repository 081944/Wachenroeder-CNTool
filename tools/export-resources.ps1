$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sampleRoot = Join-Path $root 'analysis\decoded-samples'
$workRoot = Join-Path $root 'work'

# The existing script defines the independently verified PAC/PS decoder and refreshes its sample evidence.
. (Join-Path $root 'analysis\decode-samples.ps1') | Out-Null
. (Join-Path $root 'analysis\extract-text-samples.ps1') | Out-Null
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Add-Type -AssemblyName System.Drawing

function Read-Be16([byte[]]$bytes, [int]$offset) {
    return (([int]$bytes[$offset]) -shl 8) -bor [int]$bytes[$offset + 1]
}

function Read-Be32([byte[]]$bytes, [int]$offset) {
    return (([int]$bytes[$offset]) -shl 24) -bor (([int]$bytes[$offset + 1]) -shl 16) -bor (([int]$bytes[$offset + 2]) -shl 8) -bor [int]$bytes[$offset + 3]
}

function Get-Sha256([byte[]]$bytes) {
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
}

function Get-TextWords([byte[]]$bytes, [int]$offset, [int]$cells, [hashtable]$map) {
    $rendered = [System.Collections.Generic.List[string]]::new()
    $codes = [System.Collections.Generic.List[string]]::new()
    $mapped = 0
    for ($i = 0; $i -lt $cells; $i++) {
        $word = Read-Be16 $bytes ($offset + 2 * $i)
        $codes.Add($word.ToString('X4'))
        if ($word -eq 0x20) { $rendered.Add(' ') }
        elseif ($word -ge 0x20 -and $word -lt 0xD73 -and $map.ContainsKey($word)) {
            $rendered.Add($map[$word]); $mapped++
        } else { $rendered.Add('<' + $word.ToString('X4') + '>') }
    }
    return [pscustomobject]@{ Text = ($rendered -join ''); Codes = ($codes -join ' '); Mapped = $mapped }
}

function Save-IndexedImage([byte[]]$raw, [string]$destination) {
    if ($raw.Length -lt 520) { throw 'Image header too short' }
    $width = Read-Be16 $raw 0
    $height = Read-Be16 $raw 2
    $pixelOffset = Read-Be32 $raw 4
    if ($width -lt 16 -or $width -gt 512 -or $height -ne 32 -or $pixelOffset -ne 520 -or
        $raw.Length -ne ($pixelOffset + $width * $height)) {
        throw "Unexpected WB image layout: $width x $height, offset $pixelOffset, bytes $($raw.Length)"
    }
    $bitmap = [System.Drawing.Bitmap]::new($width, $height)
    try {
        for ($y = 0; $y -lt $height; $y++) {
            for ($x = 0; $x -lt $width; $x++) {
                $index = [int]$raw[$pixelOffset + $y * $width + $x]
                $color = Read-Be16 $raw (8 + $index * 2)
                $r = [int](($color -band 31) * 255 / 31)
                $g = [int]((($color -shr 5) -band 31) * 255 / 31)
                $b = [int]((($color -shr 10) -band 31) * 255 / 31)
                $bitmap.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($r, $g, $b))
            }
        }
        $bitmap.Save($destination, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $bitmap.Dispose() }
    return [pscustomobject]@{ Width = $width; Height = $height; PixelOffset = $pixelOffset; PaletteColors = 256 }
}

$fontMap = @{}
foreach ($row in (Import-Csv -LiteralPath (Join-Path $sampleRoot 'font-map.csv') -Encoding UTF8)) {
    if ($row.Character) { $fontMap[[Convert]::ToInt32($row.TextCode.Substring(2), 16)] = $row.Character }
}
$manifest = [System.Collections.Generic.List[object]]::new()
$candidates = [System.Collections.Generic.List[object]]::new()
$images = [System.Collections.Generic.List[object]]::new()

# Japanese disc only. The English patch is used only by the optional locator audits.
foreach ($version in @('japan')) {
    $sourceDir = Join-Path $root "unpacked\$version"
    if (-not (Test-Path -LiteralPath $sourceDir)) { throw "Missing extracted disc: $sourceDir" }
    $decodedDir = Join-Path $workRoot "extracted\$version\decoded"
    $textDir = Join-Path $workRoot "text\$version"
    $fontDir = Join-Path $workRoot "fonts\original\$version"
    foreach ($dir in @($decodedDir, $textDir, $fontDir)) {
        [void](New-Item -ItemType Directory -Path $dir -Force)
    }
    Copy-Item -LiteralPath (Join-Path $sampleRoot 'font-map.csv') -Destination (Join-Path $fontDir 'font-map-provisional.csv') -Force
    foreach ($entry in @(0, 1)) {
        $stem = 'ASCII.PAC.entry{0:d2}' -f $entry
        foreach ($extension in @('bin', 'png')) {
            Copy-Item -LiteralPath (Join-Path $sampleRoot "$version\$stem.$extension") -Destination (Join-Path $fontDir "$stem.$extension") -Force
        }
    }
    0..335 | ForEach-Object {
        [pscustomobject]@{
            GlyphIndex = ('0x{0:X3}' -f $_)
            ByteOffset = ('0x{0:X4}' -f ($_ * 16))
            Character = ''
            Mapping = 'unmapped'
        }
    } | Export-Csv -LiteralPath (Join-Path $fontDir 'small-font-index.csv') -NoTypeInformation -Encoding UTF8
    Import-Csv -LiteralPath (Join-Path $sampleRoot 'text-samples.csv') -Encoding UTF8 |
        Where-Object { $_.Version -eq $version -and $_.File -like 'EVTTK*' } |
        Export-Csv -LiteralPath (Join-Path $textDir 'events.csv') -NoTypeInformation -Encoding UTF8

    $fileNames = @(Get-ChildItem -LiteralPath $sourceDir -File | Where-Object {
        $_.Name -match '^(MM\d{2}\.PS|MEM\d{2}\.PAC|TACTEVT\.PAC|WB_\d{2}\.PAC)$'
    } | Sort-Object Name)
    foreach ($file in $fileNames) {
        $entries = [WachenroederResources]::SplitPac([IO.File]::ReadAllBytes($file.FullName))
        $isImage = $file.Name -like 'WB_*'
        $isTactical = $file.Name -eq 'TACTEVT.PAC'
        $selected = if ($isImage) { @(1) } elseif ($isTactical) {
            @(1..($entries.Length - 1) | Where-Object { $_ % 2 -eq 1 })
        } elseif ($file.Name -like 'MEM*') { @(2, 5, 9, 10, 11) } else { @(2, 5, 9, 10) }
        foreach ($entry in $selected) {
            if ($entry -ge $entries.Length -or $entries[$entry].Length -eq 0) { continue }
            $raw = [WachenroederResources]::Decode($entries[$entry])
            $stem = '{0}.entry{1:d2}' -f $file.Name, $entry
            $sha = Get-Sha256 $raw
            $destination = Join-Path $decodedDir "$stem.bin"
            [IO.File]::WriteAllBytes($destination, $raw)
            $manifest.Add([pscustomobject]@{
                Version = $version; File = $file.Name; Entry = $entry
                DecodedBytes = $raw.Length; SHA256 = $sha; RelativePath = "work/extracted/$version/decoded/$stem.bin"
            })
            if ($isImage) {
                $imageSourceDir = Join-Path (Join-Path (Join-Path $workRoot 'imageori') 'wb-banners') $file.Name
                [void](New-Item -ItemType Directory -Path $imageSourceDir -Force)
                $pngPath = Join-Path $imageSourceDir "$stem.png"
                $shape = Save-IndexedImage $raw $pngPath
                0..255 | ForEach-Object {
                    $color = Read-Be16 $raw (8 + $_ * 2)
                    [pscustomobject]@{
                        Index = $_
                        BGR555 = ('0x{0:X4}' -f $color)
                        R = [int](($color -band 31) * 255 / 31)
                        G = [int]((($color -shr 5) -band 31) * 255 / 31)
                        B = [int]((($color -shr 10) -band 31) * 255 / 31)
                    }
                } | Export-Csv -LiteralPath (Join-Path $imageSourceDir "$stem.palette.csv") -NoTypeInformation -Encoding UTF8
                $images.Add([pscustomobject]@{
                    Version = $version; File = $file.Name; Entry = $entry; Width = $shape.Width
                    Height = $shape.Height; PixelOffset = $shape.PixelOffset; PaletteColors = $shape.PaletteColors
                    DecodedSHA256 = $sha; Png = "work/imageori/wb-banners/$($file.Name)/$stem.png"
                    Palette = "work/imageori/wb-banners/$($file.Name)/$stem.palette.csv"
                })
                continue
            }
            $stride = if ($isTactical) { 0 } elseif ($entry -eq 9) { 10 }
                elseif ($entry -eq 10 -or ($file.Name -like 'MEM*' -and $entry -eq 11)) { 12 } else { 0 }
            if ($stride -gt 0) {
                for ($offset = 6; $offset + $stride * 2 -le $raw.Length; $offset += $stride * 2) {
                    $rendered = Get-TextWords $raw $offset $stride $fontMap
                    if ($rendered.Mapped -lt 2 -or $rendered.Text.Trim() -eq '') { continue }
                    $candidates.Add([pscustomobject]@{
                        Version = $version; File = $file.Name; Entry = $entry; Kind = 'fixed-grid-candidate'
                        Confidence = 'candidate'; Offset = ('0x{0:X6}' -f $offset); Cells = $stride
                        Text = $rendered.Text; Codes = $rendered.Codes; DecodedSHA256 = $sha
                    })
                }
                continue
            }
            # These layers mix text, commands, and image data. Contiguous drawable runs are review candidates only.
            $start = -1
            for ($offset = 0; $offset + 1 -lt $raw.Length; $offset += 2) {
                $word = Read-Be16 $raw $offset
                $drawable = $fontMap.ContainsKey($word) -and $word -ge 0x20
                if ($drawable -and $start -lt 0) { $start = $offset }
                if ((-not $drawable -or $offset + 2 -ge $raw.Length) -and $start -ge 0) {
                    $end = if ($drawable) { $offset + 2 } else { $offset }
                    $cells = [int](($end - $start) / 2)
                    if ($cells -ge 3 -and $cells -le 80) {
                        $rendered = Get-TextWords $raw $start $cells $fontMap
                        if ($rendered.Text -match '[\p{L}\p{N}]') {
                            $candidates.Add([pscustomobject]@{
                                Version = $version; File = $file.Name; Entry = $entry; Kind = 'drawable-run-candidate'
                                Confidence = 'low'; Offset = ('0x{0:X6}' -f $start); Cells = $cells
                                Text = $rendered.Text; Codes = $rendered.Codes; DecodedSHA256 = $sha
                            })
                        }
                    }
                    $start = -1
                }
            }
        }
    }
    $candidates | Where-Object { $_.Version -eq $version } |
        Export-Csv -LiteralPath (Join-Path $textDir 'battle-and-tactical-candidates.csv') -NoTypeInformation -Encoding UTF8
}
$manifest | Export-Csv -LiteralPath (Join-Path $workRoot 'extracted\decoded-manifest.csv') -NoTypeInformation -Encoding UTF8
$imageManifestDir = Join-Path $workRoot 'imageori\manifests'
[void](New-Item -ItemType Directory -Path $imageManifestDir -Force)
$images | Export-Csv -LiteralPath (Join-Path $imageManifestDir 'image-manifest.csv') -NoTypeInformation -Encoding UTF8
Write-Host "Decoded layers: $($manifest.Count); text candidates: $($candidates.Count); verified images: $($images.Count)"
