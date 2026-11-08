param(
    [string]$BuildRoot,
    [string]$TempRoot,
    [switch]$SummaryOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'build-paths.ps1')
. (Join-Path $PSScriptRoot 'fixed-label-arrays.ps1')
$buildRoot = Resolve-TranslationBuildRoot $root $BuildRoot
$catalogRoot = Join-Path $root 'work\text\catalog\japan'
$translationRoot = Join-Path $root 'work\translation\events'
$fontMap = @{}
# Codes at or below the preserved prefix still point at the original Japanese
# bitmap when the Chinese font is built, so a raw <XXXX> in that range renders
# Japanese. Only the Han/kana entries are dangerous; punctuation and symbols
# read the same in both languages and stay usable. Both maps are merged because
# the Chinese map is a snapshot that can lag behind analysis/japan-extra-glyphs.csv,
# and a stale entry there would silently disable the check below.
$preservedHanCodes = @{}
# Codes that were given a new Chinese bitmap.  A raw or explicit glyph code in
# the preserved prefix renders the original Japanese bitmap unless the font
# build re-allocated that slot, so the two sets have to be kept apart.
$allocatedCodes = @{}
$fontMapPath = Join-Path $root 'work\fonts\chinese\font-map-chinese.csv'
if (-not (Test-Path -LiteralPath $fontMapPath)) {
    $fontMapPath = Join-Path $root 'analysis\decoded-samples\font-map.csv'
}
foreach ($row in (Import-Csv -LiteralPath $fontMapPath -Encoding UTF8)) {
    if (-not $row.Character) { continue }
    $char = [string]$row.Character
    $code = [Convert]::ToInt32($row.TextCode.Substring(2), 16)
    # A Japanese prefix row may contain the same Han character as a Chinese
    # translation. Prefer the newly allocated row so it cannot reuse the old
    # bitmap during text encoding.
    if ($row.Mapping -eq 'allocated' -or -not $fontMap.ContainsKey($char)) {
        $fontMap[$char] = $code
    }
    if ($row.Mapping -eq 'allocated') { $allocatedCodes[$row.TextCode.Substring(2)] = $true }
}
$preservedMapPaths = @($fontMapPath, (Join-Path $root 'analysis\decoded-samples\font-map.csv')) |
    Select-Object -Unique |
    Where-Object { Test-Path -LiteralPath $_ }
foreach ($mapPath in $preservedMapPaths) {
    foreach ($row in (Import-Csv -LiteralPath $mapPath -Encoding UTF8)) {
        if (-not $row.Character) { continue }
        $codeHex = $row.TextCode.Substring(2)
        $code = [Convert]::ToInt32($codeHex, 16)
        if ($code -lt 0x20 -or $code -gt 0x172) { continue }
        if ([string]$row.Character -match '[\u3005\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]') {
            $preservedHanCodes[$codeHex] = [string]$row.Character
        }
    }
}

function Convert-Target([string]$value, [string]$label) {
    $tokens = [System.Collections.Generic.List[object]]::new()
    $controls = [System.Collections.Generic.List[string]]::new()
    $rawWords = [System.Collections.Generic.List[string]]::new()
    $missing = [System.Collections.Generic.List[string]]::new()
    foreach ($match in [regex]::Matches($value, '<(?:H|I|C|P|G):[0-9A-F]{4}>|<[0-9A-F]{4}>|[\s\S]')) {
        $part = $match.Value
        if ($part -match '^<([HICPG]):([0-9A-F]{4})>$') {
            $kind = $Matches[1]; $hex = $Matches[2]
            $code = [Convert]::ToInt32($hex, 16)
            if ($kind -eq 'G' -and ($code -lt 0x20 -or $code -gt 0xD72)) { throw "$label invalid glyph code $part" }
            if ($kind -ne 'G') {
                $normalised = if ($kind -eq 'H' -or $kind -eq 'I') { 'I' } else { $kind }
                $controls.Add(('{0}:{1}' -f $normalised, $hex))
            }
            $tokens.Add([pscustomobject]@{ type = $kind; code = $hex; character = $null })
        } elseif ($part -match '^<([0-9A-F]{4})>$') {
            $hex = $Matches[1]
            $rawWords.Add($part)
            $tokens.Add([pscustomobject]@{ type = 'raw'; code = $hex; character = $null })
        } else {
            if ($part -eq '<' -or $part -eq '>') { throw "$label malformed tag or literal angle bracket" }
            if ($part -eq '\' -or $part -eq '|') { throw "$label reserved character must use a glyph code" }
            if ($fontMap.ContainsKey($part)) {
                $tokens.Add([pscustomobject]@{ type = 'glyph'; code = $fontMap[$part].ToString('X4'); character = $part })
            } else {
                $tokens.Add([pscustomobject]@{ type = 'needs-font'; code = $null; character = $part })
                if (-not $missing.Contains($part)) { $missing.Add($part) }
            }
        }
    }
    return [pscustomobject]@{
        tokens = $tokens.ToArray(); controls = $controls.ToArray()
        raw_words = $rawWords.ToArray(); missing = $missing.ToArray()
    }
}

function Assert-PreservedGlyphWords([object[]]$tokens, [string]$label) {
    # A raw <XXXX> in the preserved prefix survives writeback as a Japanese
    # bitmap. Three positions legitimately carry such a word: the leading
    # speaker/name-table index, a 0006/0007 operand, and punctuation that is
    # identical in both languages. Anything else must be translated.
    for ($i = 0; $i -lt $tokens.Count; $i++) {
        $token = $tokens[$i]
        if ($token.type -notin @('raw', 'G')) { continue }
        if (-not $preservedHanCodes.ContainsKey($token.code)) { continue }
        # An explicit <G:XXXX> is just as visible as a bare <XXXX>; the letter
        # only says who wrote the role down.  A slot that the font build
        # re-allocated to a Chinese bitmap is safe, one that kept its Japanese
        # bitmap is not.
        if ($token.type -eq 'G' -and $allocatedCodes.ContainsKey($token.code)) { continue }
        if ($i -eq 0) { continue }
        $previous = $tokens[$i - 1].code
        if ($previous -eq '0006' -or $previous -eq '0007') { continue }
        throw ("$label raw <{0}> ({1}) at visible position {2} would render the preserved Japanese glyph" -f $token.code, $preservedHanCodes[$token.code], ($i + 1))
    }
}

function Assert-MarkerOperand([string]$sourceText, [object[]]$tokens, [string]$label) {
    # Every other check treats the word after 0006/0007 as opaque -- consumed by
    # the renderer, never compared -- which is right for "is this a command or
    # the first visible glyph" but blind to *which* operand it is.  Four VTKALL
    # records shipped as <0006>A, <0006>5, <0006>6 and <0006>9: the operand had
    # been replaced by the ASCII glyph that happened to sit at that code, so the
    # line drew a stray letter and the icon got an ASCII id.  Both sides decoded
    # to "one opaque word after a marker", so nothing complained.
    #
    # Compare the actual operand values instead.  A source marker followed by a
    # literal glyph is a message's first character and contributes nothing to
    # the expected list, so a translation that starts a message with an ASCII
    # letter is still accepted.
    $parts = @([regex]::Matches($sourceText, '<[0-9A-F]{4}>|[\s\S]') | ForEach-Object Value)
    $expected = [System.Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt $parts.Count - 1; $i++) {
        if ($parts[$i] -notmatch '^<000[67]>$') { continue }
        if ($parts[$i + 1] -match '^<([0-9A-F]{4})>$') { $expected.Add($Matches[1].ToUpperInvariant()) }
    }
    if ($expected.Count -eq 0) { return }
    $actual = [System.Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt $tokens.Count - 1; $i++) {
        if ($tokens[$i].type -notin @('raw', 'C')) { continue }
        if ($tokens[$i].code -ne '0006' -and $tokens[$i].code -ne '0007') { continue }
        if ($tokens[$i + 1].type -in @('raw', 'C', 'P')) { $actual.Add(([string]$tokens[$i + 1].code).ToUpperInvariant()) }
    }
    if (($expected -join ',') -cne ($actual -join ',')) {
        throw ("$label 0006/0007 operands changed: source has [{0}], translation has [{1}]" -f ($expected -join ','), ($actual -join ','))
    }
}

function Get-OpaqueWordSignature {
    param(
        [int[]]$words,
        [bool]$MmPointerText = $false,
        [object[]]$MarkerRoles = $null
    )
    $result = [System.Collections.Generic.List[string]]::new()
    $markerOrdinal = 0
    for ($i = 0; $i -lt $words.Length; $i++) {
        $code = $words[$i]
        if ($i -eq 0 -and $null -ne $MarkerRoles) {
            $result.Add(('H:{0:X4}' -f $code)); continue
        }
        if ($code -eq 0 -and $null -ne $MarkerRoles) { continue }
        # Low controls, high runtime words, the leading speaker/index cell,
        # and (for non-MM pointer text) a 0006/0007 operand are consumed by
        # the renderer rather than being ordinary visible text. MM*.PS
        # pointer text renders the word after 0006/0007 as its first glyph.
        if ($code -le 7) {
            $result.Add(('C:{0:X4}' -f $code))
            $consumesParameter = -not $MmPointerText
            if ($code -in @(6, 7)) {
                if ($null -ne $MarkerRoles) {
                    if ($markerOrdinal -ge $MarkerRoles.Count) { throw 'Pointer text marker role count differs' }
                    $consumesParameter = [bool]$MarkerRoles[$markerOrdinal].consumes_parameter
                    $markerOrdinal++
                }
            }
            if ($code -in @(6, 7) -and $consumesParameter) {
                if ($i + 1 -ge $words.Length) { throw 'Opaque marker lacks parameter' }
                $i++
                $result.Add(('P:{0:X4}' -f $words[$i]))
            }
        } elseif ($code -lt 0x20 -or $code -ge 0x8000 -or ($i -eq 0 -and $code -ne 0x20)) {
            $kind = if ($i -eq 0 -and $code -ge 0x20 -and $code -ne 0x20) { 'H' } else { 'R' }
            $result.Add(('{0}:{1:X4}' -f $kind, $code))
        }
    }
    return $result.ToArray()
}

. (Join-Path $PSScriptRoot 'tactical-text.ps1')

function Assert-EventLayout([object[]]$tokens, [string]$label) {
    if ($tokens.Count -eq 0 -or $tokens[0].type -ne 'H') { throw "$label must begin with its event header" }
    $lineCells = 0
    $needsParameter = $false
    for ($i = 1; $i -lt $tokens.Count; $i++) {
        $token = $tokens[$i]
        if ($needsParameter) {
            if ($token.type -ne 'P') { throw "$label command 6/7 must be followed by its parameter" }
            $needsParameter = $false
            continue
        }
        if ($token.type -eq 'C') {
            $command = [Convert]::ToInt32($token.code, 16)
            if ($command -gt 7) { throw "$label invalid event command $($token.code)" }
            if ($command -in @(0, 1)) { $lineCells = 0 }
            elseif ($command -in @(2, 3, 4, 5) -and $i -ne $tokens.Count - 1) {
                throw "$label has text after a terminal event command"
            }
            elseif ($command -in @(6, 7)) { $needsParameter = $true }
            continue
        }
        if ($token.type -notin @('glyph', 'needs-font', 'G')) { throw "$label invalid token in event body: $($token.type)" }
        $lineCells++
        if ($lineCells -gt 18) { throw "$label exceeds 18 visible cells on one line" }
    }
    if ($needsParameter) { throw "$label ends with a command missing its parameter" }
}

function Assert-BodyCells([object[]]$tokens, [string]$label) {
    # Runtime rule taken from the Korean patch's wachenroder_event_format.py:
    # an event body holds only commands 0..7 (6 and 7 consume one operand) and
    # glyph cells 0x0020..0x7FFF. 0x0008..0x001F and 0x8000.. are speaker or
    # window cells, legal only as the leading cell, which the engine consumes
    # instead of drawing. If such a cell drifts into the body the renderer
    # draws a code that has no glyph slot, which shows up as a stray symbol.
    $needsParameter = $false
    for ($i = 0; $i -lt $tokens.Count; $i++) {
        $token = $tokens[$i]
        if ($null -eq $token.code) { continue }
        $code = [Convert]::ToInt32($token.code, 16)
        if ($needsParameter) { $needsParameter = $false; continue }
        if ($i -eq 0) { continue }
        if ($code -in @(6, 7)) { $needsParameter = $true; continue }
        if ($code -lt 8) { continue }
        if ($code -ge 0x20 -and $code -lt 0x8000) { continue }
        throw ("$label body cell <{0:X4}> at position {1} is an id/window code; only the leading cell or a 6/7 operand may hold it" -f $code, ($i + 1))
    }
}

function Get-SourceControlSequence([string]$sourceText) {
    # Same roles as tools/event_cells.py, applied to the exported source text so
    # a row whose source still spells its tags as bare <XXXX> compares equal to
    # a translation that now spells them as <C:XXXX>/<G:XXXX>.
    $result = [System.Collections.Generic.List[string]]::new()
    $parts = @([regex]::Matches($sourceText, '<(?:[HICPG]):[0-9A-F]{4}>|<[0-9A-F]{4}>|[\s\S]') | ForEach-Object Value)
    $needParameter = $false
    for ($i = 0; $i -lt $parts.Count; $i++) {
        $part = $parts[$i]
        if ($needParameter) {
            if ($part -match '^<([0-9A-F]{4})>$') {
                $needParameter = $false
                $result.Add(('P:{0}' -f $Matches[1]))
                continue
            }
            $needParameter = $false
        }
        if ($part -match '^<([HICPG]):([0-9A-F]{4})>$') {
            $kind = $Matches[1]
            $hex = $Matches[2]
            if ($kind -eq 'G') { continue }
            $normalised = if ($kind -eq 'H' -or $kind -eq 'I') { 'I' } else { $kind }
            $result.Add(('{0}:{1}' -f $normalised, $hex))
            if ($normalised -eq 'C' -and ($hex -eq '0006' -or $hex -eq '0007')) { $needParameter = $true }
            continue
        }
        if ($part -match '^<([0-9A-F]{4})>$') {
            $hex = $Matches[1]
            $code = [Convert]::ToInt32($hex, 16)
            if ($code -le 7) {
                $result.Add(('C:{0}' -f $hex))
                if ($code -eq 6 -or $code -eq 7) { $needParameter = $true }
                continue
            }
            if ($i -eq 0 -and ($code -le 0x1F -or $code -ge 0x8000)) {
                $result.Add(('I:{0}' -f $hex))
            }
        }
    }
    return $result.ToArray()
}

function Get-NameSlotCapacity([string]$file, [int]$entry) {
    $path = Join-Path $root ("analysis\decoded-samples\japan\{0}.entry{1:d2}.bin" -f $file, $entry)
    if (-not (Test-Path -LiteralPath $path)) { return 9 }
    $raw = [IO.File]::ReadAllBytes($path)
    $table = (([int]$raw[0]) -shl 24) -bor (([int]$raw[1]) -shl 16) -bor
        (([int]$raw[2]) -shl 8) -bor [int]$raw[3]
    $slots = if (($table - 4) % 24 -eq 0) { [int](($table - 4) / 24) } else { 0 }
    if ($slots -le 0) { throw "Unexpected name table layout in $file" }
    $stride = [int](($table - 4) / $slots)
    if ($stride -notin @(0x18, 0x20) -or $table -ne 4 + $slots * $stride) { throw "Unexpected name table layout in $file" }
    return [int](($stride - 4) / 2) - 1
}

$checked = 0
$translated = 0
$pending = [System.Collections.Generic.List[object]]::new()
$nameCapacity = @{}
$files = @(Get-ChildItem -LiteralPath $translationRoot -Filter '*.txt' -File)
foreach ($template in ($files | Sort-Object FullName)) {
    $catalogPath = Join-Path $catalogRoot "$($template.BaseName).json"
    if (-not (Test-Path -LiteralPath $catalogPath)) { throw "Missing source catalog: $catalogPath" }
    $catalog = Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $sourcePath = Join-Path $root "unpacked\japan\$($catalog.file)"
    if ((Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash -ne $catalog.source_sha256) { throw "Source file changed: $sourcePath" }
    $lines = @(Get-Content -LiteralPath $template.FullName -Encoding UTF8)
    $header = "# $($catalog.file) | source_sha256=$($catalog.source_sha256)"
    if ($lines.Count -eq 0 -or -not $lines[0].StartsWith($header, [StringComparison]::Ordinal)) {
        throw "$($template.Name): template header or source hash changed"
    }
    $expected = @($catalog.records)
    $actual = @($lines | Where-Object { $_ -and -not $_.StartsWith('#') })
    if ($actual.Count -ne $expected.Count) { throw "$($template.Name): expected $($expected.Count) rows, got $($actual.Count)" }
    for ($i = 0; $i -lt $expected.Count; $i++) {
        $record = $expected[$i]
        $entryText = if ($record.entry -lt 0) { 'raw' } else { '{0:d2}' -f [int]$record.entry }
        $prefix = '[{0}] @0x{1:X6} len={2} entry={3}' -f $record.id, [int]$record.decoded_offset, [int]$record.code_units, $entryText
        $display = $record.source_text.Replace('\', '\\').Replace('|', '\|')
        $fixed = "$prefix | $display | "
        $label = "$($template.Name) line $($i + 2)"
        if (-not $actual[$i].StartsWith($fixed, [StringComparison]::Ordinal)) { throw "$label selector, offset, length or source text changed" }
        $target = $actual[$i].Substring($fixed.Length)
        if ($target.Length -gt 0) {
            $converted = Convert-Target $target $label
            $labelWords = @($converted.tokens | ForEach-Object {
                if ($null -eq $_.code) { 0x20 } else { [Convert]::ToInt32($_.code, 16) }
            })
            Assert-FixedLabelArray $catalog.file $record $labelWords
            Assert-PreservedGlyphWords $converted.tokens $label
            $tactical = $catalog.file -eq 'TACTEVT.PAC' -and $record.kind -eq 'pointer-text-block'
            if (-not $tactical) { Assert-MarkerOperand $record.source_text $converted.tokens $label }
            Assert-BodyCells $converted.tokens $label
            $sourceControls = @(Get-SourceControlSequence $record.source_text)
            $targetControls = @($converted.controls)
            if ($record.kind -eq 'pointer-block') {
                # Dialogue reflow may change bare line breaks. Preserve all
                # headers, page/end commands and marker operands in order,
                # matching Merge-SourceControlValues in event-repack-core.ps1.
                $sourceControls = @($sourceControls | Where-Object { $_ -cne 'C:0000' })
                $targetControls = @($targetControls | Where-Object { $_ -cne 'C:0000' })
            }
            if (-not $tactical -and ($sourceControls -join ' ') -cne ($targetControls -join ' ')) { throw "$label event header/control sequence changed: source=[$($sourceControls -join ' ')] target=[$($targetControls -join ' ')]" }
            if ($record.kind -eq 'pointer-text-block') {
                $sourceWords = @($record.source_codes -split ' ' | ForEach-Object { [Convert]::ToInt32($_, 16) })
                $targetWords = [System.Collections.Generic.List[int]]::new()
                foreach ($token in $converted.tokens) {
                    if ($null -eq $token.code) { $targetWords.Add(0x20) }
                    else { $targetWords.Add([Convert]::ToInt32($token.code, 16)) }
                }
                $mmPointerText = $catalog.file -match '^MM\d{2}\.PS$'
                $markerRoles = $null
                if ($catalog.file -eq 'TACTEVT.PAC') {
                    $markerRoles = @(Get-PointerTextMarkerRoles $record.source_text $sourceWords $label)
                    Assert-PointerTextMarkerTranslation $record.source_text $sourceWords $converted.tokens $label
                }
                $sourceOpaque = Get-OpaqueWordSignature $sourceWords $mmPointerText $markerRoles
                $targetOpaque = Get-OpaqueWordSignature $targetWords.ToArray() $mmPointerText $markerRoles
                if (($sourceOpaque -join ' ') -cne ($targetOpaque -join ' ')) { throw "$label opaque raw word sequence changed" }
            }
            $policy = if ($record.kind -in @('pointer-block', 'pointer-text-block')) { 'pointer-repack' }
                elseif ($record.kind -in @('name-slot', 'structured-name-slot')) { 'fixed-name-slot' }
                elseif ($record.kind -in @('fixed-grid-candidate', 'fixed-screen-field')) { 'fixed-grid-in-place' }
                elseif ($record.kind -eq 'counted-narrow-u16') { 'counted-narrow-in-place' }
                else { 'unverified-original-span' }
            $maxCells = $null
            if ($policy -eq 'pointer-repack') {
                    if ($record.kind -eq 'pointer-block') {
                        Assert-EventLayout $converted.tokens $label
                    }
            } elseif ($policy -eq 'fixed-name-slot') {
                $capacityKey = "$($catalog.file):$($record.entry)"
                if (-not $nameCapacity.ContainsKey($capacityKey)) {
                    $nameCapacity[$capacityKey] = Get-NameSlotCapacity $catalog.file ([int]$record.entry)
                }
                $maxCells = $nameCapacity[$capacityKey]
                if (@($converted.tokens | Where-Object { $_.type -notin @('glyph', 'needs-font', 'G') }).Count -gt 0) {
                    throw "$label name slot cannot contain controls or raw words"
                }
            } elseif ($policy -eq 'fixed-grid-in-place') {
                $maxCells = [int]$record.code_units
                if (@($converted.tokens | Where-Object { $_.type -notin @('glyph', 'needs-font') }).Count -gt 0) {
                    throw "$label fixed field cannot contain controls or raw words"
                }
                # These reviewed fields use spaces for layout. Remove only
                # padding spaces when the translated visible text is one cell
                # longer; never delete a visible character.
                $fit = [System.Collections.Generic.List[object]]::new()
                foreach ($token in $converted.tokens) { $fit.Add($token) }
                while ($fit.Count -gt $maxCells) {
                    $removed = $false
                    for ($tokenIndex = $fit.Count - 1; $tokenIndex -ge 0; $tokenIndex--) {
                        if ($fit[$tokenIndex].type -eq 'glyph' -and $fit[$tokenIndex].character -eq ' ') {
                            $fit.RemoveAt($tokenIndex)
                            $removed = $true
                            break
                        }
                    }
                    if (-not $removed) { break }
                }
                $converted.tokens = $fit.ToArray()
                if ($converted.tokens.Count -gt $maxCells) {
                    throw "$label uses $($converted.tokens.Count) visible cells, capacity is $maxCells"
                }
            } elseif ($policy -eq 'counted-narrow-in-place') {
                $maxCells = [int]$record.code_units
                if (@($converted.tokens | Where-Object { $_.type -notin @('glyph', 'needs-font') }).Count -gt 0) {
                    throw "$label narrow field cannot contain controls or raw words"
                }
                if ($converted.tokens.Count -gt $maxCells) {
                    throw "$label uses $($converted.tokens.Count) cells, narrow capacity is $maxCells"
                }
            } else {
                $maxCells = [int]$record.code_units
            }
            $capacityOverflow = $false
            if ($policy -eq 'unverified-original-span' -and $null -ne $maxCells -and $converted.tokens.Count -gt $maxCells) {
                Write-Warning "$label uses $($converted.tokens.Count) cells, capacity is $maxCells; no verified writeback policy exists"
                $capacityOverflow = $true
            }
            $pending.Add([pscustomobject]@{
                file = $catalog.file; id = $record.id; entry = $record.entry
                decoded_offset = $record.decoded_offset; source_code_units = $record.code_units
                capacity = $maxCells; storage_policy = $policy
                status = $record.status; source_sha256 = $catalog.source_sha256
                decoded_sha256 = $record.decoded_sha256; source_codes = $record.source_codes
                target_text = $target; tokens = $converted.tokens
                event_header = $record.event_header; event_controls = $record.event_controls
                missing_font_characters = $converted.missing
                capacity_overflow = $capacityOverflow
                ready_for_encoding = ($policy -ne 'unverified-original-span' -and -not $capacityOverflow -and $converted.missing.Count -eq 0)
            })
            $translated++
        }
        $checked++
    }
}
& python (Join-Path $PSScriptRoot 'narrow_text_writeback.py') --root $root | Out-Host
if ($LASTEXITCODE -ne 0) { throw '8x16 narrow text validation failed' }
# The runtime cell rules live in tools/event_cells.py so extraction, this gate
# and the repacker read one definition.  It also reports anchor drift: how far
# each control cell moved relative to the visible cells before it.
& python (Join-Path $PSScriptRoot 'check-event-cells.py') --root $root --quiet --drift-limit 0 --line-width | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'text cell validation failed' }
Write-Host "Validated $checked editable rows; $translated translated rows; $(@($pending | Where-Object { $_.missing_font_characters.Count -gt 0 }).Count) rows need font glyphs. No game data was compiled."
if (-not $SummaryOnly) { $pending.ToArray() }
