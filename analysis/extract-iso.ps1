param(
    [Parameter(Mandatory = $true)]
    [string]$TrackPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PhysicalSectorSize = 2352
$UserDataOffset = 16
$UserSectorSize = 2048

function Read-UserBytes {
    param(
        [IO.FileStream]$Stream,
        [int]$StartSector,
        [int]$Length
    )

    $result = New-Object byte[] $Length
    $done = 0
    while ($done -lt $Length) {
        $sector = $StartSector + [int][Math]::Floor($done / $UserSectorSize)
        $insideSector = $done % $UserSectorSize
        $count = [Math]::Min($UserSectorSize - $insideSector, $Length - $done)
        [void]$Stream.Seek(($sector * $PhysicalSectorSize) + $UserDataOffset + $insideSector, [IO.SeekOrigin]::Begin)
        $read = $Stream.Read($result, $done, $count)
        if ($read -ne $count) {
            throw "Unexpected end of track while reading sector $sector."
        }
        $done += $read
    }
    return $result
}

function Read-LeUInt32 {
    param([byte[]]$Bytes, [int]$Offset)
    return [uint32]$Bytes[$Offset] -bor
        ([uint32]$Bytes[$Offset + 1] -shl 8) -bor
        ([uint32]$Bytes[$Offset + 2] -shl 16) -bor
        ([uint32]$Bytes[$Offset + 3] -shl 24)
}

function Get-DirectoryEntries {
    param(
        [IO.FileStream]$Stream,
        [int]$Extent,
        [int]$Length,
        [string]$RelativePath
    )

    $bytes = Read-UserBytes $Stream $Extent $Length
    $position = 0
    while ($position -lt $bytes.Length) {
        $recordLength = $bytes[$position]
        if ($recordLength -eq 0) {
            $position = (($position - ($position % $UserSectorSize)) + $UserSectorSize)
            continue
        }
        if ($recordLength -lt 34 -or $position + $recordLength -gt $bytes.Length) {
            throw "Invalid directory record at offset $position."
        }

        $record = $bytes[$position..($position + $recordLength - 1)]
        $nameLength = $record[32]
        if ($nameLength -gt 0 -and 33 + $nameLength -le $record.Length) {
            $rawName = [Text.Encoding]::ASCII.GetString($record[33..(32 + $nameLength)])
            if ($rawName -ne ([char]0) -and $rawName -ne ([char]1)) {
                $name = $rawName -replace ';[0-9]+$', ''
                $flags = $record[25]
                [pscustomobject]@{
                    Name = $name
                    RelativePath = if ($RelativePath) { Join-Path $RelativePath $name } else { $name }
                    Extent = [int](Read-LeUInt32 $record 2)
                    Length = [int](Read-LeUInt32 $record 10)
                    IsDirectory = (($flags -band 2) -ne 0)
                }
            }
        }
        $position += $recordLength
    }
}

function Write-UserFile {
    param(
        [IO.FileStream]$Stream,
        [int]$StartSector,
        [int]$Length,
        [string]$Path
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $output = [IO.File]::Open($Path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $remaining = $Length
        $sector = $StartSector
        $buffer = New-Object byte[] $UserSectorSize
        while ($remaining -gt 0) {
            [void]$Stream.Seek(($sector * $PhysicalSectorSize) + $UserDataOffset, [IO.SeekOrigin]::Begin)
            $read = $Stream.Read($buffer, 0, $UserSectorSize)
            if ($read -ne $UserSectorSize) {
                throw "Unexpected end of track while extracting sector $sector."
            }
            $write = [Math]::Min($remaining, $UserSectorSize)
            $output.Write($buffer, 0, $write)
            $remaining -= $write
            $sector++
        }
    }
    finally {
        $output.Dispose()
    }
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$stream = [IO.File]::OpenRead($TrackPath)
try {
    $pvd = Read-UserBytes $stream 16 $UserSectorSize
    $identifier = [Text.Encoding]::ASCII.GetString($pvd[1..5])
    if ($pvd[0] -ne 1 -or $identifier -ne 'CD001') {
        throw "The data track does not contain an ISO9660 primary volume descriptor."
    }

    $rootRecord = $pvd[156..189]
    $rootExtent = [int](Read-LeUInt32 $rootRecord 2)
    $rootLength = [int](Read-LeUInt32 $rootRecord 10)
    $pending = @(Get-DirectoryEntries $stream $rootExtent $rootLength '')
    $files = @()
    $skipped = @()
    $trackLength = $stream.Length
    while ($pending.Count -gt 0) {
        $entry = $pending[0]
        if ($pending.Count -eq 1) { $pending = @() } else { $pending = @($pending[1..($pending.Count - 1)]) }
        if ($entry.IsDirectory) {
            $pending += @(Get-DirectoryEntries $stream $entry.Extent $entry.Length $entry.RelativePath)
        } else {
            $lastSector = $entry.Extent + [int][Math]::Ceiling($entry.Length / [double]$UserSectorSize) - 1
            $lastByte = ($lastSector * $PhysicalSectorSize) + $UserDataOffset + $UserSectorSize
            if ($lastByte -gt $trackLength) {
                $skipped += $entry
                continue
            }
            $target = Join-Path $OutputDirectory $entry.RelativePath
            Write-UserFile $stream $entry.Extent $entry.Length $target
            $files += $entry
        }
    }
    $files | Sort-Object RelativePath | Export-Csv (Join-Path $OutputDirectory '_iso-files.csv') -NoTypeInformation -Encoding UTF8
    $skipped | Sort-Object RelativePath | Export-Csv (Join-Path $OutputDirectory '_iso-skipped.csv') -NoTypeInformation -Encoding UTF8
    "Extracted $($files.Count) files, $((($files | Measure-Object Length -Sum).Sum)) bytes to $OutputDirectory"
    "Skipped $($skipped.Count) out-of-track entries: $(@($skipped | ForEach-Object { $_.RelativePath }) -join ', ')"
}
finally {
    $stream.Dispose()
}
