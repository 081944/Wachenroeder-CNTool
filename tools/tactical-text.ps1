# TACTEVT pointer blocks always start with one speaker word (even 0000..0007).
# In the body, 0006/0007 always consume one opaque word. Display spelling is
# deliberately irrelevant: legacy catalogs rendered some operands as Japanese.
function Get-PointerTextMarkerRoles([string]$sourceText, [int[]]$sourceWords, [string]$label) {
    $roles = [System.Collections.Generic.List[object]]::new()
    for ($i = 1; $i -lt $sourceWords.Length; $i++) {
        if ($sourceWords[$i] -notin @(6, 7)) { continue }
        if ($i + 1 -ge $sourceWords.Length) { throw "$label marker lacks operand" }
        $roles.Add([pscustomobject]@{
            marker = $sourceWords[$i]; consumes_parameter = $true
            parameter = $sourceWords[++$i]
        })
    }
    return $roles.ToArray()
}

function Assert-PointerTextMarkerTranslation(
    [string]$sourceText, [int[]]$sourceWords, [object[]]$targetTokens, [string]$label
) {
    if ($targetTokens.Count -eq 0 -or -not $targetTokens[0].code -or
        [Convert]::ToInt32($targetTokens[0].code, 16) -ne $sourceWords[0]) {
        throw "$label speaker word changed or missing"
    }
    $roles = @(Get-PointerTextMarkerRoles $sourceText $sourceWords $label)
    $ordinal = 0
    for ($i = 1; $i -lt $targetTokens.Count; $i++) {
        $token = $targetTokens[$i]
        if ($token.type -notin @('C', 'raw') -or $token.code -notin @('0006', '0007')) { continue }
        if ($ordinal -ge $roles.Count) { throw "$label unexpected marker" }
        $role = $roles[$ordinal++]
        if ($i + 1 -ge $targetTokens.Count -or $targetTokens[$i + 1].type -notin @('raw', 'P') -or
            [Convert]::ToInt32($targetTokens[$i + 1].code, 16) -ne $role.parameter) {
            throw ("$label marker must preserve explicit operand <P:{0:X4}> before visible text" -f $role.parameter)
        }
        $i++
    }
    if ($ordinal -ne $roles.Count) { throw "$label marker count changed" }
}
