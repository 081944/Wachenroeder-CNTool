$ErrorActionPreference = 'Stop'

function Assert-FixedLabelArray([string]$file, [object]$record, [int[]]$words) {
    if ($file -ne 'SHOPDATA.PAC' -or [int]$record.entry -ne 0 -or [int]$record.decoded_offset -ne 0x4BA) { return }
    if ($record.source_codes -cne '0000 04D1 037F 075B 0484 06B9 0A93 09A9 0626 0002') {
        throw 'SHOPDATA item-type source layout changed; recheck the four two-glyph labels'
    }
    if ($words.Count -ne 10 -or $words[0] -ne 0 -or $words[9] -ne 2) {
        throw 'SHOPDATA item-type labels require exactly four two-glyph groups (8 glyphs), surrounded by 0000/0002; do not insert separators'
    }
    foreach ($word in $words[1..8]) {
        if ($word -lt 0x20 -or $word -ge 0x8000) {
            throw 'SHOPDATA item-type labels must contain eight glyph cells without embedded controls'
        }
    }
}
