param(
    [string]$BuildRoot,
    [string]$TempRoot
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'event-repack-core.ps1')
. (Join-Path $PSScriptRoot 'build-paths.ps1')
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$buildRoot = Resolve-TranslationBuildRoot $root $BuildRoot
$tempRoot = Resolve-TranslationTempRoot $root $TempRoot
$importPath = Join-Path $tempRoot 'imported.json'
$payload = Get-Content -LiteralPath $importPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($payload.format -ne 'wachenroeder-translation-intermediate-v1') { throw 'Unsupported translation import format' }
$allowedFiles = $payload.records | Where-Object {
    $_.file -match '^(EVTTK\d{2}|VTKALL|BUP_DATA|OPMES|PSEL|SHOPDATA|STDAT|TITLE|TACTEVT|MEM\d{2})\.PAC$' -or
    $_.file -match '^MM\d{2}\.PS$' -or
    $_.file -eq 'MES_TLE.PS'
} | Select-Object -ExpandProperty file -Unique
$specialNarrow = @($payload.records | Where-Object {
    $_.storage_policy -eq 'counted-narrow-in-place' -and $_.file -in @('PSEL.PAC', 'STDAT.PAC')
})
$unsupported = @($payload.records | Where-Object {
    $_.file -notin $allowedFiles -or $_.storage_policy -eq 'unverified-original-span'
})
$supported = @($payload.records | Where-Object {
    $_.storage_policy -in @('pointer-repack', 'fixed-name-slot', 'fixed-grid-in-place') -and
    $_.file -in $allowedFiles
})
if ($supported.Count -eq 0) {
    Write-Host "No decoded-layer translations to repack; $($specialNarrow.Count) counted-narrow records are delegated to the PAC builder; $($unsupported.Count) records have no verified writeback policy."
    return
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required to decode translated source layers.' }
& $python.Source (Join-Path $PSScriptRoot 'ensure-decoded-layers.py') --root $root --temp-root $tempRoot
if ($LASTEXITCODE -ne 0) { throw "ensure-decoded-layers.py failed with exit code $LASTEXITCODE" }
$prepared = [System.Collections.Generic.List[object]]::new()
foreach ($group in ($supported | Group-Object file)) {
    $file = $group.Name
    if ($file -notmatch '^(EVTTK\d{2}|VTKALL|BUP_DATA|OPMES|PSEL|SHOPDATA|STDAT|TITLE|TACTEVT|MEM\d{2})\.PAC$' -and
        $file -notmatch '^MM\d{2}\.PS$' -and $file -ne 'MES_TLE.PS') {
        throw "Unsupported event file $file"
    }
    $catalogPath = Join-Path $root "work\text\catalog\japan\$file.json"
    $catalog = Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $sourcePath = Join-Path $root "unpacked\japan\$file"
    if ((Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash -ne $catalog.source_sha256) {
        throw "Source file changed: $file"
    }
    $catalogById = @{}
    foreach ($sourceRecord in $catalog.records) { $catalogById[$sourceRecord.id] = $sourceRecord }
    $byId = @{}
    foreach ($record in $group.Group) {
        if (-not $record.ready_for_encoding -or @($record.missing_font_characters).Count -gt 0) {
            throw "${file}:$($record.id) needs font glyphs before repacking"
        }
        if ($record.source_sha256 -cne $catalog.source_sha256 -or $byId.ContainsKey($record.id)) {
            throw "${file}:$($record.id) translation/source metadata differs"
        }
        if (-not $catalogById.ContainsKey($record.id)) { throw "${file}:$($record.id) is not in the catalog" }
        $sourceRecord = $catalogById[$record.id]
        $expectedPolicy = if ($sourceRecord.kind -in @('pointer-block', 'pointer-text-block')) { 'pointer-repack' }
            elseif ($sourceRecord.kind -in @('name-slot', 'structured-name-slot')) { 'fixed-name-slot' }
            elseif ($sourceRecord.kind -in @('fixed-grid-candidate', 'fixed-screen-field')) { 'fixed-grid-in-place' }
            else { 'unverified-original-span' }
        if ($expectedPolicy -ne $record.storage_policy -or $record.decoded_offset -ne $sourceRecord.decoded_offset -or
            $record.source_codes -cne $sourceRecord.source_codes) {
            throw "${file}:$($record.id) does not match its catalog record"
        }
        $byId[$record.id] = $record
    }
    foreach ($entryGroup in ($group.Group | Group-Object entry)) {
        $entry = [int]$entryGroup.Name
        $layerName = if ($entry -lt 0) { "$file.raw.bin" } else { "{0}.entry{1:d2}.bin" -f $file, $entry }
        $layerPath = Join-Path $tempRoot "source-layers\japan\$layerName"
        if (-not (Test-Path -LiteralPath $layerPath)) {
            throw "Missing decoded layer $layerPath. Run analysis\decode-samples.ps1 first."
        }
        $raw = [IO.File]::ReadAllBytes($layerPath)
        $sourceHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($raw))
        $entryCatalog = [pscustomobject]@{
            file = $catalog.file
            records = @($catalog.records | Where-Object entry -eq $entry)
        }
        $entryTranslations = @{}
        foreach ($record in $entryGroup.Group) { $entryTranslations[$record.id] = $record }
        foreach ($record in $entryGroup.Group) {
            if ($record.entry -ne $entry -or $record.decoded_sha256 -cne $sourceHash) {
                throw "${file}:$($record.id) decoded layer hash differs"
            }
        }
        $repacked = Repack-EventLayer $raw $entryCatalog $entryTranslations
        if ($repacked.blocks_changed + $repacked.names_changed + $repacked.fixed_fields_changed -ne $entryTranslations.Count) {
            throw "${file}:entry$entry did not apply every selected translation"
        }
        $prepared.Add([pscustomobject]@{
            file = $file; entry = $entry; source_hash = $sourceHash; source_bytes = $raw.Length
            result = $repacked; translated = $entryGroup.Group
        })
    }
}
$outDir = Join-Path $tempRoot 'repacked\japan'
[void](New-Item -ItemType Directory -Path $outDir -Force)
$manifest = foreach ($item in $prepared) {
    $outPath = Join-Path $outDir ("{0}.entry{1:d2}.bin" -f $item.file, $item.entry)
    [IO.File]::WriteAllBytes($outPath, [byte[]]$item.result.bytes)
    [pscustomobject]@{
        file = $item.file; entry = $item.entry; source_sha256 = $item.source_hash
        rebuilt_sha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([byte[]]$item.result.bytes))
        source_bytes = $item.source_bytes; rebuilt_bytes = $item.result.bytes.Length
        pointer_count = $item.result.pointer_count
        dialogue_blocks_changed = $item.result.blocks_changed
        name_slots_changed = $item.result.names_changed
        fixed_fields_changed = $item.result.fixed_fields_changed
    }
}
$manifest | Export-Csv -LiteralPath (Join-Path $outDir 'manifest.csv') -NoTypeInformation -Encoding UTF8
$recordManifest = foreach ($item in $prepared) {
    foreach ($record in $item.translated) {
        [pscustomobject]@{
            file = $item.file; id = $record.id; kind = $record.storage_policy
            source_offset = $record.decoded_offset
            rebuilt_offset = if ($record.storage_policy -eq 'pointer-repack') {
                $item.result.offsets[$record.id]
            } else { $record.decoded_offset }
            source_code_units = $record.source_code_units
            rebuilt_code_units = @($record.tokens).Count
        }
    }
}
$recordManifest | Export-Csv -LiteralPath (Join-Path $outDir 'record-manifest.csv') -NoTypeInformation -Encoding UTF8
(Get-FileHash -LiteralPath $importPath -Algorithm SHA256).Hash |
    Set-Content -LiteralPath (Join-Path $outDir 'imported.sha256') -Encoding UTF8
Write-Host "Repacked $($prepared.Count) decoded layers, including VTKALL; $($specialNarrow.Count) counted-narrow records are delegated to the PAC builder; $($unsupported.Count) records have no verified writeback policy. No PAC or disc image was built."
