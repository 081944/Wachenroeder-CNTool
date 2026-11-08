param([switch]$OnlyMm)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sourceRoot = Join-Path $projectRoot 'unpacked\japan'
$decodedRoot = Join-Path $projectRoot 'work\extracted\japan\decoded'
$eventDecodedRoot = Join-Path $projectRoot 'analysis\decoded-samples\japan'
$catalogRoot = Join-Path $projectRoot 'work\text\catalog\japan'
$translationRoot = Join-Path $projectRoot 'work\translation\events'
[void](New-Item -ItemType Directory -Path $catalogRoot -Force)
[void](New-Item -ItemType Directory -Path $translationRoot -Force)

. (Join-Path $projectRoot 'analysis\decode-samples.ps1') | Out-Null
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Read-Be16([byte[]]$data, [int]$offset) {
    return (([int]$data[$offset]) -shl 8) -bor [int]$data[$offset + 1]
}

function Read-Be32([byte[]]$data, [int]$offset) {
    return (([int]$data[$offset]) -shl 24) -bor (([int]$data[$offset + 1]) -shl 16) -bor
        (([int]$data[$offset + 2]) -shl 8) -bor [int]$data[$offset + 3]
}

function Get-Layer([string]$file, [int]$entry) {
    $name = '{0}.entry{1:d2}.bin' -f $file, $entry
    $path = if ($file -like 'EVTTK*') { Join-Path $eventDecodedRoot $name } else { Join-Path $decodedRoot $name }
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing decoded layer $path" }
    return [IO.File]::ReadAllBytes($path)
}

function Get-Hash([byte[]]$data) {
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($data))
}

function Add-Record([hashtable]$groups, [object]$record) {
    if (-not $groups.ContainsKey($record.file)) {
        $groups[$record.file] = [System.Collections.Generic.List[object]]::new()
    }
    $groups[$record.file].Add($record)
}

function Find-UniqueBytes([byte[]]$data, [byte[]]$needle, [string]$label) {
    $found = -1
    for ($at = 0; $at + $needle.Length -le $data.Length; $at++) {
        if ($data[$at] -ne $needle[0]) { continue }
        $same = $true
        for ($i = 1; $i -lt $needle.Length; $i++) {
            if ($data[$at + $i] -ne $needle[$i]) { $same = $false; break }
        }
        if ($same) {
            if ($found -ge 0) { throw "$label appears more than once" }
            $found = $at
        }
    }
    if ($found -lt 0) { throw "$label was not found" }
    return $found
}

function Convert-NarrowCode([int]$code, [Text.Encoding]$shiftJis) {
    if ($code -eq 0x2F) { return '・' }
    if ($code -eq 0x30) { return 'ー' }
    if ($code -ge 1 -and $code -le 10) { return [string]($code - 1) }
    if ($code -ge 0x0B -and $code -le 0x24) { return [string][char](0x41 + $code - 0x0B) }
    if ($code -eq 0x89) { return '&' }
    if ($code -eq 0x8A) { return ' ' }
    if ($code -lt 0x33 -or $code -gt 0x88) { throw ('Unknown narrow-font code {0:X4}' -f $code) }
    $trail = 0x40 + $code - 0x33
    if ($trail -ge 0x7F) { $trail++ }
    return $shiftJis.GetString([byte[]]@(0x83, $trail))
}

function Find-NarrowFields([byte[]]$raw, [Text.Encoding]$shiftJis) {
    # The 8x16 counted fields use one of these four metadata prefixes.  The
    # character payload is validated through the same decoder used for export,
    # so arbitrary binary that merely contains Japanese-looking words is not
    # promoted into the formal catalog.
    $prefixes = @('00090002', '00190009', '0019000C', '0019000E')
    $fields = [System.Collections.Generic.List[object]]::new()
    for ($offset = 0; $offset + 8 -le $raw.Length; $offset += 2) {
        $count = Read-Be16 $raw $offset
        if ($count -lt 1 -or $count -gt 24) { continue }
        $prefix = [Convert]::ToHexString([byte[]]$raw[($offset + 2)..($offset + 5)])
        if ($prefix -notin $prefixes) { continue }
        if ($offset + 6 + 2 * $count -gt $raw.Length) { continue }
        $valid = $true
        for ($i = 0; $i -lt $count; $i++) {
            $code = Read-Be16 $raw ($offset + 6 + 2 * $i)
            try { [void](Convert-NarrowCode $code $shiftJis) }
            catch { $valid = $false; break }
        }
        if ($valid) {
            $fields.Add([pscustomobject]@{
                offset = $offset; count = $count; prefix = $prefix
            })
        }
    }
    return @($fields)
}

function Find-StatusNarrowTail([byte[]]$raw) {
    # The final three STDAT labels are count + glyphs, without the six-byte
    # metadata used by the shared Japanese name fields.
    $expected = @('NO EQUIP', ' - NO EQUIP -', 'NOT EQUIP')
    $totalUnits = 3
    foreach ($label in $expected) { $totalUnits += $label.Length }
    $offset = $raw.Length - 2 - 2 * $totalUnits
    if ($offset -lt 0 -or $offset % 2 -ne 0 -or
        $raw[$raw.Length - 2] -ne 0xF6 -or $raw[$raw.Length - 1] -ne 0x0A) {
        throw 'STDAT narrow tail boundary or trailer changed'
    }
    $fields = [System.Collections.Generic.List[object]]::new()
    foreach ($label in $expected) {
        $fieldOffset = $offset
        $count = Read-Be16 $raw $offset
        if ($count -ne $label.Length) { throw "STDAT narrow tail count changed at 0x$($offset.ToString('X'))" }
        $offset += 2
        $codes = [System.Collections.Generic.List[string]]::new()
        $text = [System.Text.StringBuilder]::new()
        for ($i = 0; $i -lt $count; $i++) {
            $code = Read-Be16 $raw $offset
            $character = if ($code -eq 0) { ' ' }
                elseif ($code -eq 0x30) { '-' }
                elseif ($code -ge 0x0B -and $code -le 0x24) { [string][char](0x41 + $code - 0x0B) }
                else { throw ('Unknown STDAT narrow tail code {0:X4}' -f $code) }
            $codes.Add($code.ToString('X4'))
            [void]$text.Append($character)
            $offset += 2
        }
        if ($text.ToString() -cne $label) { throw "STDAT narrow tail text changed at 0x$($fieldOffset.ToString('X'))" }
        $fields.Add([pscustomobject]@{
            offset = $fieldOffset; count = $count; text = $text.ToString()
            codes = ($codes -join ' ')
        })
    }
    if ($offset -ne $raw.Length - 2) { throw 'STDAT narrow tail length mismatch' }
    return @($fields)
}

function Find-SharedNarrowItems([byte[]]$raw, [int]$start, [int]$end, [Text.Encoding]$shiftJis) {
    $fields = [System.Collections.Generic.List[object]]::new()
    $offset = $start
    while ($offset -lt $end) {
        $fieldOffset = $offset
        $count = Read-Be16 $raw $offset
        if ($count -lt 1 -or $count -gt 24 -or $offset + 2 + 2 * $count -gt $end) {
            throw "Shared narrow item boundary changed at 0x$($offset.ToString('X'))"
        }
        $offset += 2
        $codes = [System.Collections.Generic.List[string]]::new()
        $text = [System.Text.StringBuilder]::new()
        for ($i = 0; $i -lt $count; $i++) {
            $code = Read-Be16 $raw $offset
            $character = if ($code -eq 0) { ' ' }
                elseif ($code -ge 1 -and $code -le 10) { [string]($code - 1) }
                elseif ($code -ge 0x0B -and $code -le 0x24) { [string][char](0x41 + $code - 0x0B) }
                elseif ($code -eq 0x30) {
                    if ($text.Length -gt 0 -and $text[$text.Length - 1] -match '[A-Z0-9]') { '-' } else { 'ー' }
                }
                else { Convert-NarrowCode $code $shiftJis }
            $codes.Add($code.ToString('X4'))
            [void]$text.Append($character)
            $offset += 2
        }
        $fields.Add([pscustomobject]@{
            offset = $fieldOffset; count = $count; text = $text.ToString().TrimEnd()
            codes = ($codes -join ' ')
        })
    }
    if ($offset -ne $end -or $fields.Count -ne 38) { throw 'Shared narrow item table length changed' }
    return @($fields)
}

function Get-PointerLayout([byte[]]$raw) {
    if ($raw.Length -lt 12) { return $null }
    $header = Read-Be32 $raw 0
    foreach ($table in @($header, 0)) {
        if ($table -lt 0 -or $table + 4 -gt $raw.Length -or $table % 4 -ne 0) { continue }
        if ($table -ne 0 -and ($table -lt 4 -or ($table - 4) % 24 -ne 0 -or $table -gt 0x784)) { continue }
        $first = Read-Be32 $raw $table
        if ($first -le $table -or $first -gt $raw.Length -or ($first - $table) % 4 -ne 0) { continue }
        $count = [int](($first - $table) / 4)
        if ($count -lt 1 -or $count -gt 300) { continue }
        $pointers = [int[]]::new($count)
        $valid = $true
        for ($i = 0; $i -lt $count; $i++) {
            $pointers[$i] = Read-Be32 $raw ($table + 4 * $i)
            if ($pointers[$i] -lt $first -or $pointers[$i] -ge $raw.Length -or
                $pointers[$i] % 2 -ne 0 -or ($i -gt 0 -and $pointers[$i] -le $pointers[$i - 1])) {
                $valid = $false
                break
            }
        }
        if ($valid) { return [pscustomobject]@{ table = $table; pointers = $pointers } }
    }
    return $null
}

function Add-StructuredLayer([string]$file, [int]$entry, [byte[]]$raw, [hashtable]$groups) {
    $layout = Get-PointerLayout $raw
    if ($null -eq $layout) { throw "Missing validated pointer table: ${file}:$entry" }
    $hash = Get-Hash $raw
    if ($layout.table -gt 0) {
        $slotCount = [int](($layout.table - 4) / 24)
        for ($slot = 0; $slot -lt $slotCount; $slot++) {
            $offset = 4 + 24 * $slot
            $codes = [System.Collections.Generic.List[string]]::new()
            $display = [System.Text.StringBuilder]::new()
            for ($i = 0; $i -lt 10; $i++) {
                $code = Read-Be16 $raw ($offset + 2 * $i)
                if ($code -eq 0) { break }
                $codes.Add($code.ToString('X4'))
                if ($code -ge 0x20 -and $code -le 0xD72 -and $glyphs.ContainsKey($code)) {
                    [void]$display.Append($glyphs[$code])
                } else { [void]$display.Append(('<{0:X4}>' -f $code)) }
            }
            # A one-cell name is valid. TACTEVT entry 01 uses this slot for
            # 妹, so filtering it out shifts every following record id.
            if ($codes.Count -lt 1) { continue }
            Add-Record $groups ([pscustomobject]@{
                file = $file; entry = $entry; decoded_offset = $offset
                code_units = $codes.Count; kind = 'structured-name-slot'; status = 'structured'
                source_text = $display.ToString(); source_codes = ($codes -join ' ')
                decoded_sha256 = $hash; pointer_table_offset = $layout.table
                event_header = $null; event_controls = @()
            })
        }
    }
    $pointers = [int[]]$layout.pointers
    for ($index = 0; $index -lt $pointers.Length; $index++) {
        $offset = $pointers[$index]
        $end = if ($index + 1 -lt $pointers.Length) { $pointers[$index + 1] } else { $raw.Length - $raw.Length % 2 }
        $codes = [System.Collections.Generic.List[string]]::new()
        $display = [System.Text.StringBuilder]::new()
        $glyphCount = 0
        # MM subtitles use a separate path. TACTEVT's first word is always
        # speaker data; every body 0006/0007 consumes an opaque operand.
        $paramIsOpaque = $file -notmatch '^MM\d{2}\.PS$'
        $markerPending = $false
        for ($at = $offset; $at + 1 -lt $end; $at += 2) {
            $code = Read-Be16 $raw $at
            $codes.Add($code.ToString('X4'))
            # 字形计数只看码值是否落在已映射区间，与下面的显示方式无关，
            # 否则参数改为原始码显示后会让 glyphCount 变小，触发
            # $glyphCount -lt 2 的过滤而改变记录总数。
            $isGlyph = $code -ge 0x20 -and $code -le 0xD72 -and $glyphs.ContainsKey($code)
            if ($isGlyph) { $glyphCount++ }
            if ($file -eq 'TACTEVT.PAC' -and $at -eq $offset) {
                [void]$display.Append(('<H:{0:X4}>' -f $code))
                continue
            }
            if ($paramIsOpaque -and $markerPending) {
                if ($file -eq 'TACTEVT.PAC') { [void]$display.Append(('<P:{0:X4}>' -f $code)) }
                else { [void]$display.Append(('<{0:X4}>' -f $code)) }
                $markerPending = $false
                continue
            }
            if ($code -in @(6, 7)) { $markerPending = $true }
            if ($file -eq 'TACTEVT.PAC' -and $code -le 7) {
                [void]$display.Append(('<C:{0:X4}>' -f $code))
                continue
            }
            if ($isGlyph) {
                [void]$display.Append($glyphs[$code])
            } else { [void]$display.Append(('<{0:X4}>' -f $code)) }
        }
        if ($glyphCount -lt 2) { continue }
        Add-Record $groups ([pscustomobject]@{
            file = $file; entry = $entry; decoded_offset = $offset
            code_units = $codes.Count; kind = 'pointer-text-block'; status = 'structured'
            source_text = $display.ToString(); source_codes = ($codes -join ' ')
            decoded_sha256 = $hash; pointer_table_offset = $layout.table
            event_header = $null; event_controls = @()
        })
    }
}

function Add-MmScreenLayer([string]$file, [byte[]]$raw, [hashtable]$groups) {
    $layout = Get-PointerLayout $raw
    if ($null -eq $layout -or $layout.table -ne 0) { throw "Unexpected MM screen pointer table: $file" }
    $hash = Get-Hash $raw
    $pointers = [int[]]$layout.pointers
    for ($block = 0; $block -lt $pointers.Length; $block++) {
        $start = $pointers[$block]
        $end = if ($block + 1 -lt $pointers.Length) { $pointers[$block + 1] } else { $raw.Length }
        $cells = [int](($end - $start) / 2)
        if (($end - $start) % 2 -ne 0 -or $cells -lt 18 -or ($cells - 2) % 16 -ne 0 -or
            (Read-Be16 $raw $start) -ne 0 -or (Read-Be16 $raw ($end - 2)) -ne 4) {
            throw "Unexpected MM screen block: $file at $start"
        }
        $bodyCells = $cells - 2
        $firstText = [System.Text.StringBuilder]::new()
        for ($i = 0; $i -lt 16; $i++) {
            $code = Read-Be16 $raw ($start + 2 + 2 * $i)
            if (-not $glyphs.ContainsKey($code)) { throw "Unknown MM screen glyph $file at $start" }
            [void]$firstText.Append($glyphs[$code])
        }
        $widths = if ($bodyCells -eq 112 -and $firstText.ToString().Trim().StartsWith('現在タ')) {
            @(16, 16, 16, 32, 32)
        } else {
            @(1..($bodyCells / 16) | ForEach-Object { 16 })
        }
        $slot = 0
        foreach ($width in $widths) {
            $codes = [System.Collections.Generic.List[string]]::new()
            $display = [System.Text.StringBuilder]::new()
            for ($i = 0; $i -lt $width; $i++) {
                $code = Read-Be16 $raw ($start + 2 + 2 * ($slot + $i))
                if (-not $glyphs.ContainsKey($code)) { throw "Unknown MM screen glyph $file at $start" }
                $codes.Add($code.ToString('X4'))
                [void]$display.Append($glyphs[$code])
            }
            $text = $display.ToString().Trim()
            if ($text) {
                $leadingCells = 0
                while ($leadingCells -lt $codes.Count -and $codes[$leadingCells] -eq '0020') { $leadingCells++ }
                $trailingCells = 0
                while ($trailingCells -lt ($codes.Count - $leadingCells) -and
                    $codes[$codes.Count - 1 - $trailingCells] -eq '0020') { $trailingCells++ }
                Add-Record $groups ([pscustomobject]@{
                    file = $file; entry = 2; decoded_offset = $start + 2 + 2 * $slot
                    code_units = $width; kind = 'fixed-screen-field'; status = 'structured'
                    source_text = $text; source_codes = ($codes -join ' ')
                    decoded_sha256 = $hash; pointer_table_offset = $layout.table
                    screen_block_offset = $start; screen_slot_index = [int]($slot / 16)
                    source_leading_cells = $leadingCells; source_trailing_cells = $trailingCells
                    event_header = $null; event_controls = @()
                })
            }
            $slot += $width
        }
        if ($slot -ne $bodyCells) { throw "MM screen width mismatch: $file at $start" }
    }
}

$glyphs = @{}
foreach ($row in (Import-Csv -LiteralPath (Join-Path $projectRoot 'analysis\decoded-samples\font-map.csv') -Encoding UTF8)) {
    if ($row.Character) { $glyphs[[Convert]::ToInt32($row.TextCode.Substring(2), 16)] = $row.Character }
}
foreach ($row in (Import-Csv -LiteralPath (Join-Path $projectRoot 'analysis\japan-extra-glyphs.csv') -Encoding UTF8)) {
    if ($row.Character) { $glyphs[[Convert]::ToInt32($row.TextCode.Substring(2), 16)] = $row.Character }
}
$controlMap = @{}
foreach ($row in (Import-Csv -LiteralPath (Join-Path $projectRoot 'analysis\event-control-map.csv') -Encoding UTF8)) {
    $controlMap[$row.Code] = $row
}
$groups = @{}
$layerCache = @{}
$layerHashes = @{}
$knownRows = @(
    Import-Csv -LiteralPath (Join-Path $projectRoot 'work\text\japan\events.csv') -Encoding UTF8
) + @(
    Import-Csv -LiteralPath (Join-Path $projectRoot 'work\text\japan\battle-and-tactical-candidates.csv') -Encoding UTF8
)
$nameLookup = @{}
foreach ($row in ($knownRows | Where-Object Kind -eq 'name-slot')) {
    $nameLookup['{0}|{1}|{2}' -f $row.File, $row.Entry, $row.Offset] = $row.Text
}
foreach ($row in $knownRows) {
    if ($OnlyMm -and $row.File -notmatch '^MM\d{2}\.PS$') { continue }
    if ($row.Kind -eq 'drawable-run-candidate') { continue }
    $entry = [int]$row.Entry
    $key = "$($row.File):$entry"
    if (-not $layerCache.ContainsKey($key)) { $layerCache[$key] = Get-Layer $row.File $entry }
    $raw = [byte[]]$layerCache[$key]
    $offset = [Convert]::ToInt32($row.Offset.Substring(2), 16)
    $cells = [int]$row.Cells
    if ($offset -lt 0 -or $offset + $cells * 2 -gt $raw.Length) { throw "Bad span $key $($row.Offset)" }
    $codes = [System.Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt $cells; $i++) { $codes.Add((Read-Be16 $raw ($offset + 2 * $i)).ToString('X4')) }
    if (($codes -join ' ') -ne $row.Codes) { throw "Source code words differ: $key $($row.Offset)" }
    if (-not $layerHashes.ContainsKey($key)) { $layerHashes[$key] = Get-Hash $raw }
    $layerHash = $layerHashes[$key]
    if ($row.PSObject.Properties.Name -contains 'DecodedSHA256' -and
        $row.DecodedSHA256 -and $row.DecodedSHA256 -ne $layerHash) { throw "Source layer hash differs: $key" }
    $kind = [string]$row.Kind
    $status = if ($kind -in @('pointer-block', 'name-slot')) { 'structured' } else { 'candidate' }
    $header = $null
    $controls = @()
    if ($kind -eq 'pointer-block') {
        $headWord = [Convert]::ToInt32($codes[0], 16)
        $speakerId = $headWord -band 0xFF
        $nameCount = if ($row.File -eq 'EVTTK99.PAC') { 10 } else { 80 }
        $nameStride = [int](((Read-Be32 $raw 0) - 4) / $nameCount)
        $nameOffset = 4 + ($speakerId - 1) * $nameStride
        $nameKey = '{0}|{1}|0x{2:X4}' -f $row.File, $entry, $nameOffset
        $speakerName = if ($speakerId -gt 0 -and $nameLookup.ContainsKey($nameKey)) { $nameLookup[$nameKey] } else { $null }
        $header = [pscustomobject]@{
            raw = $codes[0]; speaker_id = $speakerId; speaker_name = $speakerName
            window_bit = (($headWord -band 0x8000) -ne 0)
        }
        $commandList = [System.Collections.Generic.List[object]]::new()
        for ($wordIndex = 1; $wordIndex -lt $codes.Count; $wordIndex++) {
            $code = $codes[$wordIndex]
            if (-not $controlMap.ContainsKey($code)) { continue }
            $definition = $controlMap[$code]
            $parameter = $null
            if ($code -in @('0006', '0007')) {
                if ($wordIndex + 1 -ge $codes.Count) { throw "Truncated command $code in $key" }
                $parameter = $codes[$wordIndex + 1]
                $wordIndex++
            }
            $commandList.Add([pscustomobject]@{
                word_index = if ($parameter) { $wordIndex - 1 } else { $wordIndex }
                code = $code; role = $definition.Role; state_bits = $definition.StateBits
                confidence = $definition.Confidence; parameter = $parameter
            })
        }
        $controls = $commandList.ToArray()
    }
    Add-Record $groups ([pscustomobject]@{
        file = $row.File; entry = $entry; decoded_offset = $offset; code_units = $cells
        kind = $kind; status = $status; source_text = [string]$row.Text
        source_codes = $row.Codes; decoded_sha256 = $layerHash
        event_header = $header; event_controls = $controls
    })
}

foreach ($file in (Get-ChildItem -LiteralPath $sourceRoot -File | Where-Object {
    ($_.Name -match '^(MM\d{2}\.PS|MEM\d{2}\.PAC|TACTEVT\.PAC)$') -and
        (-not $OnlyMm -or $_.Name -match '^MM\d{2}\.PS$')
})) {
    $entries = if ($file.Name -eq 'TACTEVT.PAC') {
        @(1..117 | Where-Object { $_ % 2 -eq 1 })
    } else { @(2, 5) }
    foreach ($entry in $entries) {
        $raw = Get-Layer $file.Name $entry
        if ($file.Name -match '^MM\d{2}\.PS$' -and $entry -eq 2) {
            Add-MmScreenLayer $file.Name $raw $groups
        } else {
            Add-StructuredLayer $file.Name $entry $raw $groups
        }
    }
}

$additionalNames = @(
    Get-ChildItem -LiteralPath $sourceRoot -File | Where-Object {
        -not $OnlyMm -and (
        $_.Name -match '^(B3OPN|BUP_DATA|OPMES|PSEL|SHOPDATA|STDAT|TITLE|VTKALL)\.PAC$' -or
        $_.Name -eq 'MES_TLE.PS')
    } | Sort-Object Name
)
[Text.Encoding]::RegisterProvider([Text.CodePagesEncodingProvider]::Instance)
$shiftJis = [Text.Encoding]::GetEncoding(932)
$statusEntries = [WachenroederResources]::SplitPac([IO.File]::ReadAllBytes((Join-Path $sourceRoot 'STDAT.PAC')))
$statusLayer = [WachenroederResources]::Decode($statusEntries[1])
$statusFields = @(Find-NarrowFields $statusLayer $shiftJis)
if ($statusFields.Count -ne 56) {
    throw "Expected 56 validated STDAT narrow fields, found $($statusFields.Count)"
}
$shopEntries = [WachenroederResources]::SplitPac([IO.File]::ReadAllBytes((Join-Path $sourceRoot 'SHOPDATA.PAC')))
$shopLayer = [WachenroederResources]::Decode($shopEntries[2])
$itemStart = 0xA8
$itemEnd = 0x486
$sharedItems = @(Find-SharedNarrowItems $shopLayer $itemStart $itemEnd $shiftJis)
$sharedItemBytes = [byte[]]$shopLayer[$itemStart..($itemEnd - 1)]
$sharedItemOffset = Find-UniqueBytes $statusLayer $sharedItemBytes 'STDAT shared counted item table'
$scanAudit = [System.Collections.Generic.List[object]]::new()
foreach ($file in $additionalNames) {
    $fileBytes = [IO.File]::ReadAllBytes($file.FullName)
    $layers = [System.Collections.Generic.List[object]]::new()
    if ($file.Extension -eq '.PAC') {
        $entries = [WachenroederResources]::SplitPac($fileBytes)
        for ($entry = 0; $entry -lt $entries.Length; $entry++) {
            if ($entries[$entry].Length -lt 3) { continue }
            try { $raw = [WachenroederResources]::Decode($entries[$entry]) }
            catch { continue }
            $layers.Add([pscustomobject]@{ Entry = $entry; Raw = $raw })
        }
    } else {
        try { $raw = [WachenroederResources]::Decode($fileBytes) }
        catch { $raw = $null }
        if ($null -ne $raw) { $layers.Add([pscustomobject]@{ Entry = -1; Raw = $raw }) }
    }
    $before = if ($groups.ContainsKey($file.Name)) { $groups[$file.Name].Count } else { 0 }
    foreach ($layer in $layers) {
        $structured = switch ($file.Name) {
            'SHOPDATA.PAC' { $layer.Entry -eq 0 }
            'BUP_DATA.PAC' { $layer.Entry -eq 4 }
            'VTKALL.PAC' { $layer.Entry -ge 0 -and $layer.Entry -le 17 }
            'TITLE.PAC' { $layer.Entry -eq 3 }
            'OPMES.PAC' { $layer.Entry -ge 0 -and $layer.Entry -le 11 }
            'STDAT.PAC' { $layer.Entry -eq 0 }
            'MES_TLE.PS' { $layer.Entry -eq -1 }
            default { $false }
        }
        if ($structured) { Add-StructuredLayer $file.Name $layer.Entry ([byte[]]$layer.Raw) $groups }
    }
    if ($file.Name -in @('PSEL.PAC', 'STDAT.PAC')) {
        $entry = if ($file.Name -eq 'PSEL.PAC') { 6 } else { 1 }
        $matches = @($layers | Where-Object Entry -eq $entry)
        if ($matches.Count -ne 1) { throw "$($file.Name) narrow text layer is missing" }
        $raw = [byte[]]$matches[0].Raw
        $hash = Get-Hash $raw
        foreach ($statusField in $statusFields) {
            $sourceOffset = [int]$statusField.offset
            $count = [int]$statusField.count
            $byteCount = 6 + 2 * $count
            if ($sourceOffset + $byteCount -gt $statusLayer.Length) { throw 'STDAT field exceeds its layer' }
            $needle = [byte[]]::new($byteCount)
            [Array]::Copy($statusLayer, $sourceOffset, $needle, 0, $byteCount)
            $fieldOffset = if ($file.Name -eq 'STDAT.PAC') { $sourceOffset } else {
                Find-UniqueBytes $raw $needle ('PSEL shared field {0:X4}' -f $sourceOffset)
            }
            if ($fieldOffset + $byteCount -gt $raw.Length) { throw 'Narrow field exceeds its layer' }
            for ($i = 0; $i -lt $byteCount; $i++) {
                if ($raw[$fieldOffset + $i] -ne $needle[$i]) { throw 'Shared narrow field differs' }
            }
            $codes = [System.Collections.Generic.List[string]]::new()
            $text = [System.Text.StringBuilder]::new()
            for ($i = 0; $i -lt $count; $i++) {
                $code = Read-Be16 $raw ($fieldOffset + 6 + 2 * $i)
                $codes.Add($code.ToString('X4'))
                [void]$text.Append((Convert-NarrowCode $code $shiftJis))
            }
            Add-Record $groups ([pscustomobject]@{
                file = $file.Name; entry = $entry; decoded_offset = $fieldOffset + 6
                code_units = $count; kind = 'counted-narrow-u16'; status = 'structured'
                source_text = $text.ToString(); source_codes = ($codes -join ' ')
                decoded_sha256 = $hash; encoding = 'narrow-font-u16be'
                counted_field_offset = $fieldOffset; shared_stdat_offset = $sourceOffset
                event_header = $null; event_controls = @()
            })
        }
    }
    if ($file.Name -eq 'STDAT.PAC') {
        $raw = [byte[]]($layers | Where-Object Entry -eq 1 | Select-Object -First 1).Raw
        $hash = Get-Hash $raw
        foreach ($tailField in (Find-StatusNarrowTail $raw)) {
            Add-Record $groups ([pscustomobject]@{
                file = $file.Name; entry = 1; decoded_offset = $tailField.offset + 2
                code_units = $tailField.count; kind = 'counted-narrow-u16'; status = 'structured'
                source_text = $tailField.text; source_codes = $tailField.codes
                decoded_sha256 = $hash; encoding = 'narrow-font-u16be'
                counted_field_offset = $tailField.offset; shared_stdat_offset = $null
                event_header = $null; event_controls = @()
            })
        }
    }
    if ($file.Name -in @('STDAT.PAC', 'SHOPDATA.PAC')) {
        $entry = if ($file.Name -eq 'STDAT.PAC') { 1 } else { 2 }
        $raw = [byte[]]($layers | Where-Object Entry -eq $entry | Select-Object -First 1).Raw
        $blockOffset = Find-UniqueBytes $raw $sharedItemBytes "$($file.Name) shared counted item table"
        foreach ($item in $sharedItems) {
            $fieldOffset = $blockOffset + $item.offset - $itemStart
            Add-Record $groups ([pscustomobject]@{
                file = $file.Name; entry = $entry; decoded_offset = $fieldOffset + 2
                code_units = $item.count; kind = 'counted-narrow-u16'; status = 'structured'
                source_text = $item.text; source_codes = $item.codes
                decoded_sha256 = (Get-Hash $raw); encoding = 'narrow-font-u16be'
                counted_field_offset = $fieldOffset
                shared_stdat_offset = $sharedItemOffset + $item.offset - $itemStart
                event_header = $null; event_controls = @()
            })
        }
    }
    $after = if ($groups.ContainsKey($file.Name)) { $groups[$file.Name].Count } else { 0 }
    $scanAudit.Add([pscustomobject]@{
        file = $file.Name; decoded_layers = $layers.Count; review_candidates = $after - $before
        file_sha256 = Get-Hash $fileBytes
    })
}

$summary = [System.Collections.Generic.List[object]]::new()
$headerAudit = [System.Collections.Generic.List[object]]::new()
$controlAudit = [System.Collections.Generic.List[object]]::new()
foreach ($file in @($groups.Keys | Sort-Object)) {
    $sourcePath = Join-Path $sourceRoot $file
    if (-not (Test-Path -LiteralPath $sourcePath)) { throw "Missing source file $sourcePath" }
    $sourceHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash
    $records = @($groups[$file] | Sort-Object entry,decoded_offset,kind)
    $catalogRows = [System.Collections.Generic.List[object]]::new()
    $sourceLines = [System.Collections.Generic.List[string]]::new()
    $translationLines = [System.Collections.Generic.List[string]]::new()
    $sourceLines.Add("# $file | source_sha256=$sourceHash")
    $translationLines.Add("# $file | source_sha256=$sourceHash | preserve source, offsets, lengths, and control tags; record kind/status are in the catalog")
    for ($i = 0; $i -lt $records.Count; $i++) {
        $row = $records[$i]
        $id = '{0:d4}' -f $i
        $entryText = if ($row.entry -lt 0) { 'raw' } else { '{0:d2}' -f $row.entry }
        $prefix = '[{0}] @0x{1:X6} len={2} entry={3}' -f $id, $row.decoded_offset, $row.code_units, $entryText
        $display = $row.source_text.Replace('\', '\\').Replace('|', '\|')
        $sourceLines.Add("$prefix | $display")
        # All verified text records use one editable input stream.  The
        # catalog kind/status remains the source of truth for the writeback
        # policy; a directory name must not change how a row is interpreted.
        $defaultTarget = if ($file -eq 'TACTEVT.PAC' -and $row.kind -eq 'structured-name-slot' -and $row.source_text -eq '妹') {
            '妹'
        } else { '' }
        $translationLines.Add("$prefix | $display | $defaultTarget")
        $catalogRow = [ordered]@{
            id = $id; entry = $row.entry; decoded_offset = $row.decoded_offset
            code_units = $row.code_units; kind = $row.kind; status = $row.status
            source_text = $row.source_text; source_codes = $row.source_codes
            decoded_sha256 = $row.decoded_sha256
            event_header = $row.event_header; event_controls = $row.event_controls
        }
        if ($row.PSObject.Properties.Name -contains 'encoding') {
            $catalogRow.encoding = $row.encoding
            $catalogRow.counted_field_offset = $row.counted_field_offset
            $catalogRow.shared_stdat_offset = $row.shared_stdat_offset
        }
        if ($row.PSObject.Properties.Name -contains 'pointer_table_offset') {
            $catalogRow.pointer_table_offset = $row.pointer_table_offset
        }
        if ($row.PSObject.Properties.Name -contains 'screen_block_offset') {
            $catalogRow.screen_block_offset = $row.screen_block_offset
            $catalogRow.screen_slot_index = $row.screen_slot_index
        }
        if ($row.kind -in @('fixed-grid-candidate', 'fixed-screen-field')) {
            $codesForLayout = @($row.source_codes -split ' ')
            $leadingForLayout = if ($row.PSObject.Properties.Name -contains 'source_leading_cells') {
                [int]$row.source_leading_cells
            } else {
                $count = 0
                while ($count -lt $codesForLayout.Count -and $codesForLayout[$count] -eq '0020') { $count++ }
                $count
            }
            $trailingForLayout = if ($row.PSObject.Properties.Name -contains 'source_trailing_cells') {
                [int]$row.source_trailing_cells
            } else {
                $count = 0
                while ($count -lt ($codesForLayout.Count - $leadingForLayout) -and
                    $codesForLayout[$codesForLayout.Count - 1 - $count] -eq '0020') { $count++ }
                $count
            }
            $catalogRow.source_leading_cells = $leadingForLayout
            $catalogRow.source_trailing_cells = $trailingForLayout
        }
        $catalogRows.Add([pscustomobject]$catalogRow)
        if ($row.event_header) {
            $headerAudit.Add([pscustomobject]@{
                file = $file; id = $id; raw = $row.event_header.raw
                speaker_id = $row.event_header.speaker_id
                speaker_name = $row.event_header.speaker_name
                window_bit = $row.event_header.window_bit
            })
            foreach ($command in $row.event_controls) {
                $controlAudit.Add([pscustomobject]@{
                    file = $file; id = $id; word_index = $command.word_index
                    code = $command.code; role = $command.role
                    state_bits = $command.state_bits; parameter = $command.parameter
                    confidence = $command.confidence
                })
            }
        }
    }
    $catalog = [ordered]@{
        format = 'wachenroeder-text-catalog-v1'
        file = $file; source_sha256 = $sourceHash
        encoding = 'font-index-u16be'; code_formula = 'glyph_index + 0x20'
        records = $catalogRows.ToArray()
    }
    $json = ConvertTo-Json -InputObject $catalog -Depth 6
    Set-Content -LiteralPath (Join-Path $catalogRoot "$file.json") -Value $json -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $catalogRoot "$file.txt") -Value $sourceLines -Encoding UTF8
    if ($translationLines.Count -gt 1) {
        $translationPath = Join-Path $translationRoot "$file.txt"
        # IDs are generated from record order and can change when a skipped
        # valid slot is restored. Match existing work by stable source data.
        $existingByKey = @{}
        if (Test-Path -LiteralPath $translationPath) {
            foreach ($existingLine in (Get-Content -LiteralPath $translationPath -Encoding UTF8)) {
                $existingSeparator = $existingLine.LastIndexOf(' | ')
                if ($existingLine -match '^\[\d{4}\] ' -and $existingSeparator -ge 7) {
                    $existingKey = $existingLine.Substring(7, $existingSeparator - 7)
                    $existingByKey[$existingKey] = $existingLine
                }
            }
        }
        $mergedLines = [System.Collections.Generic.List[string]]::new()
        $mergedLines.Add($translationLines[0])
        foreach ($generatedLine in $translationLines | Select-Object -Skip 1) {
            $generatedSeparator = $generatedLine.LastIndexOf(' | ')
            $generatedKey = if ($generatedLine -match '^\[\d{4}\] ' -and $generatedSeparator -ge 7) {
                $generatedLine.Substring(7, $generatedSeparator - 7)
            } else { $null }
            if ($null -ne $generatedKey -and $existingByKey.ContainsKey($generatedKey)) {
                $existingLine = $existingByKey[$generatedKey]
                $separator = ' | '
                $generatedTargetStart = $generatedLine.LastIndexOf($separator)
                $existingTargetStart = $existingLine.LastIndexOf($separator)
                if ($generatedTargetStart -ge 0 -and $existingTargetStart -ge 0) {
                    $generatedFixed = $generatedLine.Substring(0, $generatedTargetStart + $separator.Length)
                    $existingFixed = $existingLine.Substring(0, $existingTargetStart + $separator.Length)
                    if ($generatedFixed.Substring(7) -cne $existingFixed.Substring(7)) {
                        throw "Existing translation row at $generatedKey in $file no longer matches the source catalog"
                    }
                    $existingTarget = $existingLine.Substring($existingTargetStart + $separator.Length)
                    $generatedTarget = $generatedLine.Substring($generatedTargetStart + $separator.Length)
                    $mergedTarget = if ([string]::IsNullOrEmpty($existingTarget) -and $generatedTarget.Length -gt 0) {
                        $generatedTarget
                    } else { $existingTarget }
                    $mergedLines.Add($generatedFixed + $mergedTarget)
                    continue
                }
            }
            $mergedLines.Add($generatedLine)
        }
        Set-Content -LiteralPath $translationPath -Value $mergedLines -Encoding UTF8
    }
    $summary.Add([pscustomobject]@{
        file = $file; total = $records.Count
        structured = @($records | Where-Object status -eq 'structured').Count
        editable = $records.Count
    })
}
if ($OnlyMm) {
    $previous = Import-Csv -LiteralPath (Join-Path $catalogRoot 'catalog-summary.csv') -Encoding UTF8 |
        Where-Object { $_.file -notmatch '^MM\d{2}\.PS$' }
    @($previous) + @($summary) | Sort-Object file |
        Export-Csv -LiteralPath (Join-Path $catalogRoot 'catalog-summary.csv') -NoTypeInformation -Encoding UTF8
} else {
    $summary | Export-Csv -LiteralPath (Join-Path $catalogRoot 'catalog-summary.csv') -NoTypeInformation -Encoding UTF8
    $scanAudit | Export-Csv -LiteralPath (Join-Path $catalogRoot 'additional-scan-audit.csv') -NoTypeInformation -Encoding UTF8
    $headerAudit | Export-Csv -LiteralPath (Join-Path $catalogRoot 'event-header-audit.csv') -NoTypeInformation -Encoding UTF8
    $controlAudit | Export-Csv -LiteralPath (Join-Path $catalogRoot 'event-control-audit.csv') -NoTypeInformation -Encoding UTF8
}
if (-not $OnlyMm) {
    & python (Join-Path $PSScriptRoot 'export-executable-narrow-text.py') --root $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Executable narrow menu export failed' }
    & python (Join-Path $PSScriptRoot 'export-executable-battle-text.py') --root $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Executable battle HUD export failed' }
}
& python (Join-Path $PSScriptRoot 'export-mm-narrow-byte-text.py') --root $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'MM/MEM narrow byte export failed' }
Write-Host "Catalog files: $($summary.Count); records: $(($summary | Measure-Object total -Sum).Sum); editable: $(($summary | Measure-Object editable -Sum).Sum)"
