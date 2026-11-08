$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'fixed-label-arrays.ps1')

function Read-EventU32([byte[]]$data, [int]$offset) {
    if ($offset -lt 0 -or $offset + 4 -gt $data.Length) { throw "U32 outside event layer at $offset" }
    return (([int]$data[$offset]) -shl 24) -bor (([int]$data[$offset + 1]) -shl 16) -bor
        (([int]$data[$offset + 2]) -shl 8) -bor [int]$data[$offset + 3]
}

function Write-EventU32([byte[]]$data, [int]$offset, [int]$value) {
    if ($offset -lt 0 -or $offset + 4 -gt $data.Length -or $value -lt 0) { throw "Invalid event pointer at $offset" }
    for ($i = 0; $i -lt 4; $i++) { $data[$offset + $i] = [byte](($value -shr (24 - 8 * $i)) -band 0xFF) }
}

function Read-EventWords([byte[]]$data, [int]$offset, [int]$byteCount) {
    if ($offset -lt 0 -or $byteCount -lt 0 -or $byteCount % 2 -ne 0 -or $offset + $byteCount -gt $data.Length) {
        throw "Invalid event word span at $offset"
    }
    $words = [int[]]::new($byteCount / 2)
    for ($i = 0; $i -lt $words.Length; $i++) {
        $words[$i] = (([int]$data[$offset + 2 * $i]) -shl 8) -bor [int]$data[$offset + 2 * $i + 1]
    }
    return ,$words
}

function Encode-ImportedWords([object[]]$tokens) {
    $words = [int[]]::new($tokens.Count)
    for ($i = 0; $i -lt $tokens.Count; $i++) {
        if (-not $tokens[$i].code) { throw "No font code for '$($tokens[$i].character)'" }
        $word = [Convert]::ToInt32($tokens[$i].code, 16)
        $tokenType = if ($tokens[$i].PSObject.Properties['type']) { $tokens[$i].type } else { 'raw' }
        $previousType = if ($i -gt 0 -and $tokens[$i - 1].PSObject.Properties['type']) { $tokens[$i - 1].type } else { 'raw' }
        $isParameter = $tokenType -in @('P', 'raw') -and $i -gt 0 -and
            $previousType -in @('C', 'raw') -and
            [Convert]::ToInt32($tokens[$i - 1].code, 16) -in @(6, 7)
        if ($tokenType -eq 'P' -and -not $isParameter) {
            throw "cell $i is a parameter without a preceding 0006/0007 command"
        }
        # 0x0008-0x001F and 0x8000- are speaker/window cells.  Outside the
        # leading position the renderer looks them up as glyph cells, finds no
        # bitmap and draws garbage, so they must never reach the encoder here.
        if (-not $isParameter -and $i -ne 0 -and $word -gt 7 -and ($word -lt 0x20 -or $word -ge 0x8000)) {
            throw ("cell {0} encodes <{1:X4}>, an id/window code the renderer would draw as a glyph" -f $i, $word)
        }
        $words[$i] = $word
    }
    return ,$words
}

function Test-IdCell([int]$word) {
    # 0x0008-0x001F and 0x8000+ are speaker/window cells.  0x0000-0x0007 are
    # commands and never an id, even in the leading position.
    return (($word -gt 7 -and $word -le 0x1F) -or $word -ge 0x8000)
}

function Get-ControlAnchors([int[]]$words) {
    # Every control cell except a bare line break, with operands flagged so a
    # 0x0000 operand is not mistaken for a 0x0000 command.
    $values = [System.Collections.Generic.List[int]]::new()
    $operand = [System.Collections.Generic.List[bool]]::new()
    for ($i = 0; $i -lt $words.Length; $i++) {
        $word = $words[$i]
        if ($i -eq 0 -and (Test-IdCell $word)) {
            $values.Add($word); $operand.Add($false); continue
        }
        if ($word -gt 7 -or $word -eq 0) { continue }
        $values.Add($word); $operand.Add($false)
        if ($word -in @(6, 7) -and $i + 1 -lt $words.Length) {
            $values.Add($words[++$i]); $operand.Add($true)
        }
    }
    return ,@($values, $operand)
}

function Merge-SourceControlValues([int[]]$sourceWords, [int[]]$targetWords, [string]$label) {
    # The repacker rebuilds a block from the translation, which means every
    # control cell is re-encoded from its tag.  Copy the value from the source
    # stream instead: the translation only decides where a command sits, never
    # what it is, and an id cell can never turn into a glyph because the source
    # word is written back verbatim.
    #
    # Line breaks (0x0000) are the one cell the translation may drop or add.
    # Chinese is shorter than Japanese, and padding a sentence to the Japanese
    # row count is what makes a box read like broken speech.  Every other cell
    # -- speaker id, 0001-0007 and their operands -- must still match the
    # source one for one, so this compares the anchor sequences first and only
    # then lays the source values over the translation's layout.
    $sourceAnchor = Get-ControlAnchors $sourceWords
    $targetAnchor = Get-ControlAnchors $targetWords
    $srcVal = $sourceAnchor[0]
    $srcOp = $sourceAnchor[1]
    # Operand values are opaque IDs that the source always wins, so they stay
    # out of the comparison; only the commands themselves must line up.
    $srcPlain = [System.Collections.Generic.List[int]]::new()
    for ($k = 0; $k -lt $srcVal.Count; $k++) { if (-not $srcOp[$k]) { $srcPlain.Add($srcVal[$k]) } }
    $tgtPlain = [System.Collections.Generic.List[int]]::new()
    for ($k = 0; $k -lt $targetAnchor[0].Count; $k++) {
        if (-not $targetAnchor[1][$k]) { $tgtPlain.Add($targetAnchor[0][$k]) }
    }
    # When the translation opens the block with a glyph instead of the speaker
    # cell, the repacker still restores that cell, so the source's leading id
    # takes no part in the comparison.
    $srcStart = 0
    if ($srcPlain.Count -gt 0 -and (Test-IdCell $srcPlain[0])) {
        $targetHasId = ($tgtPlain.Count -gt 0 -and (Test-IdCell $tgtPlain[0]))
        if (-not $targetHasId) { $srcStart = 1 }
    }
    $srcPlain = $srcPlain.GetRange($srcStart, $srcPlain.Count - $srcStart)
    if (($tgtPlain -join ',') -cne ($srcPlain -join ',')) {
        throw ("$label control command sequence changed: source [$($srcPlain -join ' ')] " +
            "target [$($tgtPlain -join ' ')]")
    }
    $result = [int[]]$targetWords.Clone()
    $index = 0
    $count = $srcVal.Count
    for ($i = 0; $i -lt $result.Length; $i++) {
        $word = $result[$i]
        # The leading cell carries the speaker/window id even when the
        # translation replaced it with a glyph, so restore it from the source
        # before the generic control copy below.
        if ($i -eq 0 -and $index -lt $count -and -not $srcOp[$index] -and
            (Test-IdCell $srcVal[$index])) {
            $result[0] = $srcVal[$index]
            $index++
            continue
        }
        $isControl = ($i -eq 0 -and (Test-IdCell $word)) -or ($word -le 7)
        if (-not $isControl) { continue }
        if ($word -eq 0) { continue }
        if ($index -lt $count) { $result[$i] = $srcVal[$index] }
        $index++
        if ($word -in @(6, 7) -and $i + 1 -lt $result.Length) {
            $i++
            if ($index -lt $count) { $result[$i] = $srcVal[$index] }
            $index++
        }
    }
    return ,$result
}

function Convert-EventWordsToBytes([int[]]$words) {
    $bytes = [byte[]]::new($words.Length * 2)
    for ($i = 0; $i -lt $words.Length; $i++) {
        $bytes[2 * $i] = [byte](($words[$i] -shr 8) -band 0xFF)
        $bytes[2 * $i + 1] = [byte]($words[$i] -band 0xFF)
    }
    return ,$bytes
}

function Get-EventSignature([int[]]$words) {
    if ($words.Length -eq 0) { throw 'Empty event block' }
    $result = [System.Collections.Generic.List[string]]::new()
    $result.Add(('H:{0:X4}' -f $words[0]))
    for ($i = 1; $i -lt $words.Length; $i++) {
        $word = $words[$i]
        if ($word -le 7) {
            # 0x0000 only moves the cursor to the next row, so it is left out
            # of the signature: a shorter Chinese line may drop it.
            if ($word -ne 0) { $result.Add(('C:{0:X4}' -f $word)) }
            if ($word -in @(6, 7)) {
                if ($i + 1 -ge $words.Length) { throw 'Event marker lacks parameter' }
                $i++
                $result.Add(('P:{0:X4}' -f $words[$i]))
            }
        } elseif ($word -lt 0x20 -or $word -ge 0x8000) {
            throw ('Invalid event body word {0:X4}' -f $word)
        }
    }
    return ($result -join ' ')
}

function Get-NonEventControlSignature(
    [int[]]$words,
    [bool]$MmPointerText = $false,
    [object[]]$MarkerRoles = $null
) {
    $result = [System.Collections.Generic.List[string]]::new()
    $markerOrdinal = 0
    for ($i = 0; $i -lt $words.Length; $i++) {
        $word = $words[$i]
        if ($i -eq 0 -and $null -ne $MarkerRoles) {
            $result.Add(('H:{0:X4}' -f $word)); continue
        }
        if ($word -eq 0 -and $null -ne $MarkerRoles) { continue }
        if ($word -le 7) {
            $result.Add(('C:{0:X4}' -f $word))
            $consumesParameter = -not $MmPointerText
            if ($word -in @(6, 7)) {
                if ($null -ne $MarkerRoles) {
                    if ($markerOrdinal -ge $MarkerRoles.Count) { throw 'Pointer text marker role count differs' }
                    $consumesParameter = [bool]$MarkerRoles[$markerOrdinal].consumes_parameter
                    $markerOrdinal++
                }
            }
            if ($word -in @(6, 7) -and $consumesParameter) {
                if ($i + 1 -ge $words.Length) { throw 'Pointer text marker lacks parameter' }
                $i++
                $result.Add(('P:{0:X4}' -f $words[$i]))
            }
        } elseif ($word -lt 0x20 -or $word -ge 0x8000) {
            $result.Add(('R:{0:X4}' -f $word))
        } elseif ($i -eq 0 -and $word -ne 0x20) {
            # Pointer-text layers commonly begin with a speaker/name-table
            # index that looks like an ordinary glyph but is consumed as data.
            $result.Add(('H:{0:X4}' -f $word))
        }
    }
    return ($result -join ' ')
}

. (Join-Path $PSScriptRoot 'tactical-text.ps1')

function Assert-SourceWords([int[]]$words, [string]$sourceCodes, [string]$label) {
    $expected = @($sourceCodes -split ' ')
    if ($words.Length -ne $expected.Count) { throw "$label source cell count changed" }
    for ($i = 0; $i -lt $words.Length; $i++) {
        if ($words[$i].ToString('X4') -cne $expected[$i]) { throw "$label source word $i changed" }
    }
}

function Get-FixedFieldLayout([object]$record) {
    $codes = @($record.source_codes -split ' ')
    $capacity = [int]$record.code_units
    if ($codes.Count -ne $capacity) {
        throw "$($record.file):$($record.id) fixed field source cell count differs from capacity"
    }
    $hasLeadingMetadata = $record.PSObject.Properties.Name -contains 'source_leading_cells'
    $hasTrailingMetadata = $record.PSObject.Properties.Name -contains 'source_trailing_cells'
    $leading = if ($hasLeadingMetadata -and $null -ne $record.source_leading_cells) {
        [int]$record.source_leading_cells
    } else {
        $count = 0
        while ($count -lt $codes.Count -and $codes[$count] -eq '0020') { $count++ }
        $count
    }
    $trailing = if ($hasTrailingMetadata -and $null -ne $record.source_trailing_cells) {
        [int]$record.source_trailing_cells
    } else {
        $count = 0
        while ($count -lt ($codes.Count - $leading) -and $codes[$codes.Count - 1 - $count] -eq '0020') { $count++ }
        $count
    }
    if ($leading -lt 0 -or $trailing -lt 0 -or $leading + $trailing -gt $capacity) {
        throw "$($record.file):$($record.id) invalid fixed field padding metadata"
    }
    $mode = if ($leading -gt 0 -and $trailing -gt 0) { 'center' }
        elseif ($leading -gt 0) { 'right' }
        else { 'left' }
    return [pscustomobject]@{
        mode = $mode
        leading = $leading
        trailing = $trailing
        capacity = $capacity
    }
}

function Get-FixedFieldWords([object]$record, [object[]]$targetTokens) {
    $layout = Get-FixedFieldLayout $record
    $content = @($targetTokens)
    if ($content.Count -gt $layout.capacity) {
        throw "$($record.file):$($record.id) exceeds fixed field capacity $($layout.capacity)"
    }
    $remaining = $layout.capacity - $content.Count
    $left = switch ($layout.mode) {
        'center' { [int][Math]::Floor($remaining / 2) }
        'right' { $remaining }
        default { 0 }
    }
    $words = [int[]]::new($layout.capacity)
    for ($i = 0; $i -lt $layout.capacity; $i++) { $words[$i] = 0x20 }
    $encoded = Encode-ImportedWords $content
    [Array]::Copy($encoded, 0, $words, $left, $encoded.Length)
    return ,$words
}

function Repack-EventLayer([byte[]]$raw, [object]$catalog, [hashtable]$translations) {
    $nameRecords = @($catalog.records | Where-Object kind -in @('name-slot', 'structured-name-slot'))
    $pointerRecords = @($catalog.records | Where-Object kind -in @('pointer-block', 'pointer-text-block') | Sort-Object decoded_offset)
    $fixedRecords = @($catalog.records | Where-Object kind -in @('fixed-grid-candidate', 'fixed-screen-field'))
    $table = 0
    $fieldBytes = 0
    $nameCapacity = 0
    if ($nameRecords.Count -gt 0) {
        $table = Read-EventU32 $raw 0
        if ($table -lt 4 -or ($table - 4) % 24 -ne 0) { throw "Unexpected name layout in $($catalog.file)" }
        $slots = [int](($table - 4) / 24)
        $stride = [int](($table - 4) / $slots)
        if ($stride -ne 0x18 -or $table -ne 4 + $slots * $stride) { throw "Unexpected name layout in $($catalog.file)" }
        $fieldBytes = $stride - 4
        $nameCapacity = [int]($fieldBytes / 2) - 1
    }
    $working = [byte[]]$raw.Clone()
    $namesChanged = 0
    foreach ($record in $nameRecords) {
        if (-not $translations.ContainsKey($record.id)) { continue }
        $target = $translations[$record.id]
        $offset = [int]$record.decoded_offset
        if (($offset - 4) % $stride -ne 0 -or $offset + $fieldBytes -gt $table) {
            throw "$($catalog.file):$($record.id) name slot is outside its table"
        }
        Assert-SourceWords (Read-EventWords $raw $offset ([int]$record.code_units * 2)) $record.source_codes "$($catalog.file):$($record.id)"
        $words = Encode-ImportedWords $target.tokens
        if ($words.Length -gt $nameCapacity) { throw "$($catalog.file):$($record.id) exceeds name capacity $nameCapacity" }
        for ($i = 0; $i -lt $fieldBytes; $i++) { $working[$offset + $i] = 0 }
        $encoded = Convert-EventWordsToBytes $words
        [Array]::Copy($encoded, 0, $working, $offset, $encoded.Length)
        $namesChanged++
    }

    $fixedChanged = 0
    foreach ($record in $fixedRecords) {
        if (-not $translations.ContainsKey($record.id)) { continue }
        $offset = [int]$record.decoded_offset
        $capacity = [int]$record.code_units
        Assert-SourceWords (Read-EventWords $raw $offset ($capacity * 2)) $record.source_codes "$($catalog.file):$($record.id)"
        $targetTokens = @($translations[$record.id].tokens)
        if (@($targetTokens | Where-Object { $_.type -notin @('glyph', 'needs-font') }).Count -gt 0) {
            throw "$($catalog.file):$($record.id) fixed field contains controls or raw words"
        }
        $words = Get-FixedFieldWords $record $targetTokens
        $bytes = Convert-EventWordsToBytes $words
        [Array]::Copy($bytes, 0, $working, $offset, $bytes.Length)
        $fixedChanged++
    }

    if ($pointerRecords.Count -eq 0) {
        return [pscustomobject]@{ bytes = $working; pointer_count = 0; blocks_changed = 0; names_changed = $namesChanged; fixed_fields_changed = $fixedChanged; offsets = @{} }
    }
    if ($nameRecords.Count -eq 0) {
        # MM/MEM pointer-only layers begin directly with their pointer table.
        $table = Read-EventU32 $raw 0
    }
    $pointerTable = $table
    if ($nameRecords.Count -eq 0 -and $pointerRecords[0].pointer_table_offset -eq 0) {
        # MEM entry 2 stores its offset table directly at byte zero. In this
        # layout the first offset is also the table byte length.
        $pointerTable = 0
    }
    $first = Read-EventU32 $raw $pointerTable
    $emptyLayer = ($nameRecords.Count -gt 0 -and $raw.Length -eq $table + 2 -and $raw[$table] -eq 0 -and $raw[$table + 1] -eq 0)
    $records = $pointerRecords
    if ($emptyLayer) {
        if ($records.Count -ne 0) { throw "$($catalog.file) has records but no pointer table" }
        return [pscustomobject]@{ bytes = $working; pointer_count = 0; blocks_changed = 0; names_changed = $namesChanged; fixed_fields_changed = $fixedChanged; offsets = @{} }
    }
    if ($pointerTable -eq 0) {
        if ($first -lt 8 -or $first % 4 -ne 0) { throw "$($catalog.file) invalid direct pointer table" }
        $pointerCount = [int]($first / 4)
    } else {
        if ($first -le $pointerTable -or ($first - $pointerTable) % 4 -ne 0) { throw "$($catalog.file) invalid pointer table" }
        $pointerCount = [int](($first - $pointerTable) / 4)
    }
    # Some resources contain non-text pointer entries which the catalog
    # intentionally omits.  Drive the rebuild from the real pointer table and
    # replace only cataloged offsets; omitted blocks must remain byte-for-byte.
    $recordByOffset = @{}
    foreach ($catalogRecord in $records) {
        $recordByOffset[[int]$catalogRecord.decoded_offset] = $catalogRecord
    }
    $starts = [int[]]::new($pointerCount)
    for ($i = 0; $i -lt $pointerCount; $i++) {
        $starts[$i] = Read-EventU32 $raw ($pointerTable + 4 * $i)
        if ($starts[$i] % 2 -ne 0 -or $starts[$i] -lt $first -or $starts[$i] -ge $raw.Length -or
            ($i -gt 0 -and $starts[$i] -le $starts[$i - 1])) {
            throw "$($catalog.file) invalid pointer $i"
        }
    }
    $rebuilt = [System.Collections.Generic.List[byte]]::new()
    $rebuilt.AddRange([byte[]]$working[0..($first - 1)])
    $newOffsets = @{}
    $newPointerOffsets = @{}
    $blocksChanged = 0
    for ($i = 0; $i -lt $pointerCount; $i++) {
        $start = $starts[$i]
        $end = if ($i + 1 -lt $pointerCount) { $starts[$i + 1] } else { $raw.Length }
        $record = $null
        if ($recordByOffset.ContainsKey($start)) { $record = $recordByOffset[$start] }
        $oldWords = Read-EventWords $raw $start ($end - $start)
        $newPointerOffsets[$start] = $rebuilt.Count
        if ($null -ne $record) {
            Assert-SourceWords $oldWords $record.source_codes "$($catalog.file):$($record.id)"
            $newOffsets[$record.id] = $rebuilt.Count
        }
        if ($null -ne $record -and $translations.ContainsKey($record.id)) {
            $newWords = Encode-ImportedWords $translations[$record.id].tokens
            Assert-FixedLabelArray $catalog.file $record $newWords
            if ($record.kind -eq 'pointer-block') {
                if ((Get-EventSignature $oldWords) -cne (Get-EventSignature $newWords)) {
                    throw "$($catalog.file):$($record.id) event command signature changed"
                }
                $newWords = Merge-SourceControlValues $oldWords $newWords "$($catalog.file):$($record.id)"
            } else {
                $mmPointerText = $catalog.file -match '^MM\d{2}\.PS$'
                $markerRoles = $null
                if ($catalog.file -eq 'TACTEVT.PAC') {
                    $markerRoles = @(Get-PointerTextMarkerRoles $record.source_text $oldWords "$($catalog.file):$($record.id)")
                    Assert-PointerTextMarkerTranslation $record.source_text $oldWords $translations[$record.id].tokens "$($catalog.file):$($record.id)"
                }
                if ((Get-NonEventControlSignature $oldWords $mmPointerText $markerRoles) -cne
                    (Get-NonEventControlSignature $newWords $mmPointerText $markerRoles)) {
                    throw "$($catalog.file):$($record.id) pointer control signature changed"
                }
            }
            $block = Convert-EventWordsToBytes $newWords
            $blocksChanged++
        } else {
            $block = [byte[]]::new($end - $start)
            [Array]::Copy($raw, $start, $block, 0, $block.Length)
        }
        $rebuilt.AddRange($block)
    }
    $result = $rebuilt.ToArray()
    if ($result.Length -gt 16 * 1024 * 1024) { throw "$($catalog.file) decoded layer exceeds decoder limit" }
    for ($i = 0; $i -lt $pointerCount; $i++) {
        Write-EventU32 $result ($pointerTable + 4 * $i) ([int]$newPointerOffsets[$starts[$i]])
    }
    return [pscustomobject]@{
        bytes = $result; pointer_count = $pointerCount
        blocks_changed = $blocksChanged; names_changed = $namesChanged; fixed_fields_changed = $fixedChanged
        offsets = $newOffsets
    }
}
