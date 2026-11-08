$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Join-Path $PSScriptRoot 'decoded-samples'
[Text.Encoding]::RegisterProvider([Text.CodePagesEncodingProvider]::Instance)
$eucJp = [Text.Encoding]::GetEncoding('euc-jp', [Text.EncoderExceptionFallback]::new(), [Text.DecoderExceptionFallback]::new())
$glyphs = @{
    0 = ' '
    5 = '・'
    0x1B = 'ー'
}
foreach ($number in 0..9) { $glyphs[0x6C + $number] = [string]$number }
foreach ($number in 0..25) {
    $glyphs[0x76 + $number] = [string][char]([int][char]'A' + $number)
    $glyphs[0x90 + $number] = [string][char]([int][char]'a' + $number)
}

function Add-JisGlyphs {
    param([int]$StartIndex, [int[]]$Rows)
    $index = $StartIndex
    foreach ($row in $Rows) {
        foreach ($column in 0x21..0x7E) {
            try {
                $character = $eucJp.GetString([byte[]]@(($row -bor 0x80), ($column -bor 0x80)))
                if ($character.Length -eq 1) {
                    $glyphs[$index] = $character
                    $index++
                }
            } catch [Text.DecoderFallbackException] {
                continue
            }
        }
    }
}
Add-JisGlyphs -StartIndex 0xAA -Rows @(0x24, 0x25)
Add-JisGlyphs -StartIndex 0x153 -Rows @(0x30..0x4F)
$extraGlyphs = Import-Csv -LiteralPath (Join-Path $PSScriptRoot 'japan-extra-glyphs.csv') -Encoding UTF8
$extraByIndex = @{}
foreach ($row in $extraGlyphs) {
    $code = [Convert]::ToInt32($row.TextCode.Substring(2), 16)
    $index = $code - 0x20
    if ($index -lt 0 -or $index -gt 3410 -or $glyphs.ContainsKey($index)) { throw "Duplicate or invalid extra glyph $($row.TextCode)" }
    $glyphs[$index] = $row.Character
    $extraByIndex[$index] = $row
}
$fontRows = foreach ($glyphIndex in 0..3410) {
    $character = if ($glyphs.ContainsKey($glyphIndex)) { $glyphs[$glyphIndex] } else { '' }
    [pscustomobject]@{
        GlyphIndex = ('0x{0:X3}' -f $glyphIndex)
        TextCode = ('0x{0:X4}' -f ($glyphIndex + 0x20))
        Character = $character
        Mapping = if ($extraByIndex.ContainsKey($glyphIndex)) { $extraByIndex[$glyphIndex].Confidence } elseif ($character) { 'provisional' } else { 'unmapped' }
        Evidence = if ($extraByIndex.ContainsKey($glyphIndex)) { $extraByIndex[$glyphIndex].Evidence } else { '' }
    }
}
$fontRows | Export-Csv -LiteralPath (Join-Path $root 'font-map.csv') -NoTypeInformation -Encoding UTF8

function Read-Big16([byte[]]$Data, [int]$Offset) {
    return (([int]$Data[$Offset]) -shl 8) -bor ([int]$Data[$Offset + 1])
}

function Read-Big32([byte[]]$Data, [int]$Offset) {
    return (([int]$Data[$Offset]) -shl 24) -bor (([int]$Data[$Offset + 1]) -shl 16) -bor (([int]$Data[$Offset + 2]) -shl 8) -bor ([int]$Data[$Offset + 3])
}

function Render-Span {
    param([byte[]]$Data, [int]$Start, [int]$End)
    $parts = [System.Collections.Generic.List[string]]::new()
    $codes = [System.Collections.Generic.List[string]]::new()
    for ($offset = $Start; $offset + 1 -lt $End; $offset += 2) {
        $word = Read-Big16 $Data $offset
        $codes.Add($word.ToString('X4'))
        $index = $word - 0x20
        if ($word -eq 0) { $parts.Add('<PAD>') }
        elseif ($word -lt 0x20 -or ($word -ge 0x8000 -and $word -le 0x80FF)) {
            $parts.Add('<C:' + $word.ToString('X4') + '>')
        } elseif ($glyphs.ContainsKey($index)) {
            $parts.Add($glyphs[$index])
        } else {
            $parts.Add('<G:' + $word.ToString('X4') + '>')
        }
    }
    return [pscustomobject]@{
        Text = ($parts -join '')
        Codes = ($codes -join ' ')
    }
}

function Render-EventBlock {
    param([byte[]]$Data, [int]$Start, [int]$End)
    $parts = [System.Collections.Generic.List[string]]::new()
    $codes = [System.Collections.Generic.List[string]]::new()
    $index = 0
    $pendingParameter = $null
    for ($offset = $Start; $offset + 1 -lt $End; $offset += 2) {
        $word = Read-Big16 $Data $offset
        $codes.Add($word.ToString('X4'))
        if ($index -eq 0) {
            $parts.Add('<H:' + $word.ToString('X4') + '>')
        } elseif ($null -ne $pendingParameter) {
            $parts.Add('<P:' + $word.ToString('X4') + '>')
            $pendingParameter = $null
        } elseif ($word -le 7) {
            $parts.Add('<C:' + $word.ToString('X4') + '>')
            if ($word -in @(6, 7)) { $pendingParameter = $word }
        } elseif ($word -lt 0x20 -or $word -ge 0x8000) {
            throw "Invalid event body word 0x$($word.ToString('X4')) at 0x$($offset.ToString('X4'))"
        } else {
            $glyphIndex = $word - 0x20
            if ($glyphs.ContainsKey($glyphIndex)) { $parts.Add($glyphs[$glyphIndex]) }
            else { $parts.Add('<G:' + $word.ToString('X4') + '>') }
        }
        $index++
    }
    if ($null -ne $pendingParameter) { throw 'Event block ends with a control missing its parameter' }
    return [pscustomobject]@{ Text = ($parts -join ''); Codes = ($codes -join ' ') }
}

$records = [System.Collections.Generic.List[object]]::new()
# Japanese disc only. The English patch is used only by the optional locator audits.
foreach ($version in @('japan')) {
    $dir = Join-Path $root $version
    foreach ($eventFile in Get-ChildItem -LiteralPath $dir -Filter 'EVTTK*.PAC.entry01.bin') {
        $eventName = $eventFile.Name -replace '\.entry01\.bin$', ''
        $event = [IO.File]::ReadAllBytes($eventFile.FullName)
        $table = Read-Big32 $event 0
        $nameCount = if ($eventName -eq 'EVTTK99.PAC') { 10 } else { 80 }
        if (($table - 4) % $nameCount -ne 0) { throw "Unexpected $eventName name table end: 0x$($table.ToString('X'))" }
        $nameStride = [int](($table - 4) / $nameCount)
        if ($nameStride -notin @(0x18, 0x20)) { throw "Unexpected $eventName name stride: $nameStride" }
        for ($slot = 0; $slot -lt $nameCount; $slot++) {
            $start = 4 + $slot * $nameStride
            $end = ($slot + 1) * $nameStride
            while ($end -gt $start -and (Read-Big16 $event ($end - 2)) -eq 0) { $end -= 2 }
            if ($end -le $start) { continue }
            $rendered = Render-Span $event $start $end
            $records.Add([pscustomobject]@{
                Version = $version; File = $eventName; Entry = 1; Kind = 'name-slot'
                Number = $slot; Offset = ('0x{0:X4}' -f $start); Cells = ($end - $start) / 2
                Text = $rendered.Text; Codes = $rendered.Codes
            })
        }
        if ($event.Length -eq $table + 2 -and (Read-Big16 $event $table) -eq 0) { continue }
        $first = Read-Big32 $event $table
        if ($first -le $table -or ($first - $table) % 4 -ne 0) { throw "Invalid $eventName pointer table" }
        $pointers = [System.Collections.Generic.List[int]]::new()
        for ($offset = $table; $offset -lt $first; $offset += 4) {
            $pointer = Read-Big32 $event $offset
            if ($pointer % 2 -ne 0 -or $pointer -lt $first -or $pointer -ge $event.Length) {
                throw "Invalid $eventName text pointer: 0x$($pointer.ToString('X'))"
            }
            if ($pointers.Count -gt 0 -and $pointer -le $pointers[$pointers.Count - 1]) {
                throw "Unordered $eventName text pointer: 0x$($pointer.ToString('X'))"
            }
            $pointers.Add($pointer)
        }
        for ($number = 0; $number -lt $pointers.Count; $number++) {
            $start = $pointers[$number]
            $end = if ($number + 1 -lt $pointers.Count) { $pointers[$number + 1] } else { $event.Length }
            $rendered = Render-EventBlock $event $start $end
            $records.Add([pscustomobject]@{
                Version = $version; File = $eventName; Entry = 1; Kind = 'pointer-block'
                Number = $number; Offset = ('0x{0:X4}' -f $start); Cells = ($end - $start) / 2
                Text = $rendered.Text; Codes = $rendered.Codes
            })
        }
    }
    foreach ($entry in @(9, 10)) {
        $name = 'MM01.PS.entry{0:d2}.bin' -f $entry
        $raw = [IO.File]::ReadAllBytes((Join-Path $dir $name))
        $stride = if ($entry -eq 9) { 10 } else { 12 }
        for ($start = 4; $start + $stride * 2 -le $raw.Length; $start += $stride * 2) {
            $rendered = Render-Span $raw $start ($start + $stride * 2)
            if ($rendered.Text -notmatch '[\p{L}\p{N}]') { continue }
            $records.Add([pscustomobject]@{
                Version = $version; File = 'MM01.PS'; Entry = $entry; Kind = 'fixed-grid-candidate'
                Number = [int](($start - 4) / ($stride * 2)); Offset = ('0x{0:X4}' -f $start)
                Cells = $stride; Text = $rendered.Text; Codes = $rendered.Codes
            })
        }
    }
}

$records | Export-Csv -LiteralPath (Join-Path $root 'text-samples.csv') -NoTypeInformation -Encoding UTF8
$fontRows | Group-Object Mapping | Select-Object Name,Count | Format-Table -AutoSize
$records | Group-Object Version,Kind | Select-Object Name,Count | Format-Table -AutoSize
$records | Where-Object { $_.Version -eq 'japan' -and $_.File -eq 'EVTTK01.PAC' -and $_.Kind -eq 'pointer-block' } |
    Select-Object -First 6 Offset,Text | Format-Table -Wrap
