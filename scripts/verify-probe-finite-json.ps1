param(
    [string]$AssemblyPath = "",
    [string]$ONIManagedDir = "C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $AssemblyPath) {
    $AssemblyPath = Join-Path $ProjectRoot "mod\bin\Release\OniAiProbeMod.dll"
}
$AssemblyPath = (Resolve-Path -LiteralPath $AssemblyPath).Path
$AssemblyItem = Get-Item -LiteralPath $AssemblyPath
$NewerInputs = @(
    Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "mod\Source") -Recurse -File -Filter "*.cs" |
        Where-Object { $_.LastWriteTimeUtc -gt $AssemblyItem.LastWriteTimeUtc }
)
if ((Get-Item -LiteralPath (Join-Path $ProjectRoot "mod\OniAiProbeMod.csproj")).LastWriteTimeUtc -gt
    $AssemblyItem.LastWriteTimeUtc) {
    $NewerInputs += Get-Item -LiteralPath (Join-Path $ProjectRoot "mod\OniAiProbeMod.csproj")
}
if ($NewerInputs.Count -gt 0) {
    throw "Release assembly is stale; rebuild before running this verification."
}

$RequiredRuntimeAssemblies = @(
    "UnityEngine.CoreModule.dll",
    "0Harmony.dll",
    "Newtonsoft.Json.dll",
    "Assembly-CSharp-firstpass.dll",
    "Assembly-CSharp.dll"
)
foreach ($FileName in $RequiredRuntimeAssemblies) {
    $DependencyPath = Join-Path $ONIManagedDir $FileName
    if (-not (Test-Path -LiteralPath $DependencyPath)) {
        throw "Required ONI runtime assembly is missing: $DependencyPath"
    }
    [void][System.Reflection.Assembly]::LoadFrom($DependencyPath)
}

$Assembly = [System.Reflection.Assembly]::LoadFrom($AssemblyPath)
$StaticNonPublic = [System.Reflection.BindingFlags]"Static,NonPublic"
$AssemblyCSharp = [System.Reflection.Assembly]::LoadFrom(
    (Join-Path $ONIManagedDir "Assembly-CSharp.dll")
)
$AssemblyCSharpFirstpass = [System.Reflection.Assembly]::LoadFrom(
    (Join-Path $ONIManagedDir "Assembly-CSharp-firstpass.dll")
)
$ClusterUtilType = $AssemblyCSharp.GetType("ClusterUtil", $true)
$KMonoBehaviourType = $AssemblyCSharpFirstpass.GetType("KMonoBehaviour", $true)
$PowerWorldIdMethods = @(
    $ClusterUtilType.GetMethods(
        [System.Reflection.BindingFlags]"Static,Public,NonPublic"
    ) |
        Where-Object {
            $_.Name -eq "GetMyWorldId" -and
            $_.ReturnType -eq [int] -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType -eq $KMonoBehaviourType
        }
)
if ($PowerWorldIdMethods.Count -ne 1) {
    throw "Assembly-CSharp does not expose exactly one ClusterUtil.GetMyWorldId(KMonoBehaviour)."
}

$ProbeWriterType = $Assembly.GetType(
    "OniAiProbeMod.Probe.ProbeWriter",
    $true
)
$JsonWriterType = $Assembly.GetType(
    "OniAiProbeMod.Probe.ProbeWriter+JsonLineWriter",
    $true
)
$JsonWriteMethod = $JsonWriterType.GetMethod("Write", $StaticNonPublic)
$NonFiniteCountField = $ProbeWriterType.GetField(
    "nonFiniteNumericCount",
    $StaticNonPublic
)

function Invoke-JsonWrite {
    param([object]$Value)

    $Writer = [System.IO.StringWriter]::new(
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    try {
        [void]$JsonWriteMethod.Invoke($null, @($Writer, $Value))
        return $Writer.ToString()
    }
    finally {
        $Writer.Dispose()
    }
}

$NonFiniteCases = @(
    [double]::PositiveInfinity,
    [double]::NegativeInfinity,
    [double]::NaN,
    [single]::PositiveInfinity,
    [single]::NegativeInfinity,
    [single]::NaN
)
$FiniteCases = @(
    [double]1.25,
    [single]-2.5,
    [double]::MinValue,
    [double]::MaxValue,
    [decimal]12.34,
    [long]9223372036854775807
)

$BeforeCount = [long]$NonFiniteCountField.GetValue($null)
$Serialized = [System.Collections.Generic.List[string]]::new()
foreach ($Value in $NonFiniteCases) {
    $Json = Invoke-JsonWrite -Value $Value
    if ($Json -ne "null") {
        throw "Non-finite $($Value.GetType().Name) serialized as '$Json' instead of null."
    }
    $Serialized.Add($Json)
}
$AfterCount = [long]$NonFiniteCountField.GetValue($null)
if (($AfterCount - $BeforeCount) -ne $NonFiniteCases.Count) {
    throw "Writer non-finite counter did not increase once per rejected value."
}

foreach ($Value in $FiniteCases) {
    $Json = Invoke-JsonWrite -Value $Value
    if ($Json -eq "null" -or $Json -match "NaN|Infinity") {
        throw "Finite $($Value.GetType().Name) serialized as '$Json'."
    }
    $Serialized.Add($Json)
}

$EnumJson = Invoke-JsonWrite -Value ([System.DayOfWeek]::Monday)
if ($EnumJson -ne '"Monday"') {
    throw "Enum serialized as '$EnumJson' instead of a JSON string."
}
$Serialized.Add($EnumJson)

$StrictJsonPayload = "[" + ($Serialized -join ",") + "]"
$StrictJsonPayload | & python -c 'import json, sys; json.loads(sys.stdin.read(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))'
if ($LASTEXITCODE -ne 0) {
    throw "Serialized numeric fixture failed strict Python JSON parsing."
}

$SurvivalReaderType = $Assembly.GetType(
    "OniAiProbeMod.Probe.SurvivalTelemetryReader",
    $true
)
$ProbeStateReaderType = $Assembly.GetType(
    "OniAiProbeMod.Probe.ProbeStateReader",
    $true
)
$DuplicantWorldBindingMethod = $ProbeStateReaderType.GetMethod(
    "DuplicantWorldBindingMatches",
    $StaticNonPublic
)
if ($null -eq $DuplicantWorldBindingMethod) {
    throw "Duplicant active-world reference binding helper is missing from the Release assembly."
}
if ([bool]$DuplicantWorldBindingMethod.Invoke($null, [object[]]@($null, $null, -1))) {
    throw "Duplicant active-world reference binding accepted null manager/world identities."
}
$DuplicantPriorityBindingMethod = $ProbeStateReaderType.GetMethod(
    "DuplicantPriorityBindingsMatch",
    $StaticNonPublic
)
if ($null -eq $DuplicantPriorityBindingMethod) {
    throw "Duplicant priority DB binding helper is missing from the Release assembly."
}
if ([bool]$DuplicantPriorityBindingMethod.Invoke($null, [object[]]@($null, $null, $null))) {
    throw "Duplicant priority DB binding accepted null collection identities."
}
$ObservationScopeWorldBindingMethod = $SurvivalReaderType.GetMethod(
    "ObservationScopeWorldBindingMatches",
    $StaticNonPublic
)
$ObservationScopeLimitField = $SurvivalReaderType.GetField(
    "ObservationScopeIdentityEnumerationLimit",
    $StaticNonPublic
)
if ($null -eq $ObservationScopeWorldBindingMethod -or $null -eq $ObservationScopeLimitField) {
    throw "Observation-scope integrity helpers are missing from the Release assembly."
}
if ([bool]$ObservationScopeWorldBindingMethod.Invoke($null, [object[]]@($null, $null, -1))) {
    throw "Observation scope accepted null manager/world identities."
}
$ObservationScopeIdentityLimit = [int]$ObservationScopeLimitField.GetRawConstantValue()
if ($ObservationScopeIdentityLimit -le 0 -or $ObservationScopeIdentityLimit -gt 10000) {
    throw "Observation-scope identity limit is outside the accepted bounded range: $ObservationScopeIdentityLimit"
}
$AccumulateMethod = $SurvivalReaderType.GetMethod(
    "TryAccumulateNonNegativeFinite",
    $StaticNonPublic
)
$AccumulateCapacityMethod = $SurvivalReaderType.GetMethod(
    "AccumulateCapacity",
    $StaticNonPublic
)
$RegisterPowerIdentityMethod = $SurvivalReaderType.GetMethod(
    "TryRegisterPowerComponentIdentity",
    $StaticNonPublic
)
$PowerRegistryLimitField = $SurvivalReaderType.GetField(
    "PowerRegistryEnumerationLimit",
    $StaticNonPublic
)
if ($null -eq $RegisterPowerIdentityMethod -or $null -eq $PowerRegistryLimitField) {
    throw "Power registry integrity helpers are missing from the Release assembly."
}
$PowerRegistryLimit = [int]$PowerRegistryLimitField.GetRawConstantValue()
if ($PowerRegistryLimit -le 0 -or $PowerRegistryLimit -gt 500000) {
    throw "Power registry enumeration limit is outside the accepted bounded range: $PowerRegistryLimit"
}

function Assert-PowerIdentityRegistration {
    param(
        [string]$Name,
        [AllowNull()][System.Collections.Generic.HashSet[int]]$InstanceIds,
        [int]$InstanceId,
        [int]$EnumerationLimit,
        [bool]$ExpectedAccepted,
        [AllowNull()][string]$ExpectedReason,
        [int]$ExpectedCount
    )

    $Arguments = [object[]]@(
        $InstanceId,
        $InstanceIds,
        $EnumerationLimit,
        $null
    )
    $Accepted = [bool]$RegisterPowerIdentityMethod.Invoke($null, $Arguments)
    $ActualReason = if ($null -eq $Arguments[3]) {
        $null
    }
    else {
        [string]$Arguments[3]
    }
    $NormalizedExpectedReason = if ([string]::IsNullOrEmpty($ExpectedReason)) {
        $null
    }
    else {
        $ExpectedReason
    }
    $ActualCount = if ($null -eq $InstanceIds) { 0 } else { $InstanceIds.Count }
    if ($Accepted -ne $ExpectedAccepted -or
        $ActualReason -ne $NormalizedExpectedReason -or
        $ActualCount -ne $ExpectedCount) {
        throw "Power identity case '$Name' mismatch: accepted=$Accepted reason='$ActualReason' count=$ActualCount"
    }
}

$PowerIdentitySet = [System.Collections.Generic.HashSet[int]]::new()
Assert-PowerIdentityRegistration -Name "positive_id" -InstanceIds $PowerIdentitySet `
    -InstanceId 101 -EnumerationLimit 2 -ExpectedAccepted $true -ExpectedReason $null -ExpectedCount 1
Assert-PowerIdentityRegistration -Name "duplicate_id" -InstanceIds $PowerIdentitySet `
    -InstanceId 101 -EnumerationLimit 2 -ExpectedAccepted $false `
    -ExpectedReason "component_duplicate" -ExpectedCount 1
Assert-PowerIdentityRegistration -Name "negative_unity_id" -InstanceIds $PowerIdentitySet `
    -InstanceId -202 -EnumerationLimit 2 -ExpectedAccepted $true -ExpectedReason $null -ExpectedCount 2
Assert-PowerIdentityRegistration -Name "limit_exceeded" -InstanceIds $PowerIdentitySet `
    -InstanceId 303 -EnumerationLimit 2 -ExpectedAccepted $false `
    -ExpectedReason "component_limit_exceeded" -ExpectedCount 2
$InvalidPowerIdentitySet = [System.Collections.Generic.HashSet[int]]::new()
Assert-PowerIdentityRegistration -Name "zero_id" -InstanceIds $InvalidPowerIdentitySet `
    -InstanceId 0 -EnumerationLimit 2 -ExpectedAccepted $false `
    -ExpectedReason "component_identity_invalid" -ExpectedCount 0
Assert-PowerIdentityRegistration -Name "null_set" -InstanceIds $null `
    -InstanceId 404 -EnumerationLimit 2 -ExpectedAccepted $false `
    -ExpectedReason "component_identity_invalid" -ExpectedCount 0
Assert-PowerIdentityRegistration -Name "invalid_limit" -InstanceIds $InvalidPowerIdentitySet `
    -InstanceId 404 -EnumerationLimit 0 -ExpectedAccepted $false `
    -ExpectedReason "component_identity_invalid" -ExpectedCount 0

$SurvivalReaderSourcePath = Join-Path $ProjectRoot "mod\Source\Probe\SurvivalTelemetryReader.cs"
$SurvivalReaderSource = Get-Content -LiteralPath $SurvivalReaderSourcePath -Raw
$ProbeStateReaderSourcePath = Join-Path $ProjectRoot "mod\Source\Probe\ProbeStateReader.cs"
$ProbeStateReaderSource = Get-Content -LiteralPath $ProbeStateReaderSourcePath -Raw
$ObservationScopeShapeChecks = [ordered]@{
    enumeration_is_bounded = [regex]::IsMatch(
        $SurvivalReaderSource,
        'enumeratedIdentityCount >= ObservationScopeIdentityEnumerationLimit'
    )
    null_or_destroyed_identity_is_rejected = [regex]::IsMatch(
        $SurvivalReaderSource,
        'foreach \(MinionIdentity identity in identities\)[\s\S]*?if \(identity == null\)'
    )
    negative_world_id_is_rejected = [regex]::IsMatch(
        $SurvivalReaderSource,
        'int worldId = identity\.GetMyWorldId\(\);[\s\S]*?if \(worldId < 0\)'
    )
    world_ids_are_unique = [regex]::IsMatch(
        $SurvivalReaderSource,
        'HashSet<int> populatedWorldIdSet[\s\S]*?populatedWorldIdSet\.Add\(worldId\)'
    )
    reference_binding_checked_before_and_after = (
        [regex]::Matches(
            $SurvivalReaderSource,
            'ObservationScopeWorldBindingMatches\(manager, activeWorld, activeWorldId\)'
        ).Count -ge 2
    )
    covered_world_is_exact_active_world = [regex]::IsMatch(
        $SurvivalReaderSource,
        'coveredJson\.Add\(activeWorldId\);[\s\S]*?coverageComplete = populatedWorldIds\.Count == 1 &&[\s\S]*?populatedWorldIds\[0\] == activeWorldId;'
    )
}
$FailedObservationScopeShapeChecks = @(
    $ObservationScopeShapeChecks.GetEnumerator() |
        Where-Object { -not [bool]$_.Value } |
        ForEach-Object { $_.Key }
)
if ($FailedObservationScopeShapeChecks.Count -gt 0) {
    throw "Observation-scope production shape checks failed: $($FailedObservationScopeShapeChecks -join ', ')"
}
$DuplicantPriorityShapeChecks = [ordered]@{
    group_enumeration_is_bounded = [regex]::IsMatch(
        $ProbeStateReaderSource,
        'enumeratedGroupCount >= DuplicantPriorityGroupLimit'
    )
    invalid_or_duplicate_group_id_is_rejected = [regex]::IsMatch(
        $ProbeStateReaderSource,
        'group == null \|\| string\.IsNullOrWhiteSpace\(group\.Id\) \|\|[\s\S]*?!choreGroupIds\.Add\(group\.Id\)'
    )
    db_collection_reference_is_bound = [regex]::IsMatch(
        $ProbeStateReaderSource,
        'ReferenceEquals\(Db\.Get\(\), database\)[\s\S]*?ReferenceEquals\(database\.ChoreGroups\.resources, choreGroupResources\)'
    )
    priorities_use_validated_group_set = [regex]::IsMatch(
        $ProbeStateReaderSource,
        'foreach \(ChoreGroup group in priorityGroups\)[\s\S]*?priorities\.Add\(group\.Id, consumer\.GetPersonalPriority\(group\)\);'
    )
}
$FailedDuplicantPriorityShapeChecks = @(
    $DuplicantPriorityShapeChecks.GetEnumerator() |
        Where-Object { -not [bool]$_.Value } |
        ForEach-Object { $_.Key }
)
if ($FailedDuplicantPriorityShapeChecks.Count -gt 0) {
    throw "Duplicant priority production shape checks failed: $($FailedDuplicantPriorityShapeChecks -join ', ')"
}
$PowerProductionShapeChecks = [ordered]@{
    world_binding_checked_before_and_after_registries = (
        [regex]::Matches(
            $SurvivalReaderSource,
            'PowerWorldBindingMatches\(manager, activeWorld, activeWorldId\)'
        ).Count -ge 4
    )
    cluster_manager_instance_bound = [regex]::IsMatch(
        $SurvivalReaderSource,
        'ReferenceEquals\(ClusterManager\.Instance, manager\)'
    )
    active_world_object_bound = [regex]::IsMatch(
        $SurvivalReaderSource,
        'ReferenceEquals\(manager\.activeWorld, activeWorld\)'
    )
    active_world_id_bound = [regex]::IsMatch(
        $SurvivalReaderSource,
        'manager\.activeWorldId == activeWorldId'
    )
    battery_destroyed_or_null_rejected = [regex]::IsMatch(
        $SurvivalReaderSource,
        'if \(battery == null\)'
    )
    consumer_destroyed_or_null_rejected = [regex]::IsMatch(
        $SurvivalReaderSource,
        'if \(consumer == null\)'
    )
    generator_destroyed_or_null_rejected = [regex]::IsMatch(
        $SurvivalReaderSource,
        'if \(generator == null\)'
    )
    battery_world_id_bound = [regex]::IsMatch(
        $SurvivalReaderSource,
        'battery\.GetMyWorldId\(\) != activeWorldId'
    )
    consumer_world_id_bound = [regex]::IsMatch(
        $SurvivalReaderSource,
        'consumer\.GetMyWorldId\(\) != activeWorldId'
    )
    generator_world_id_bound = [regex]::IsMatch(
        $SurvivalReaderSource,
        'generator\.GetMyWorldId\(\) != activeWorldId'
    )
    component_world_ids_rechecked_before_publish = [regex]::IsMatch(
        $SurvivalReaderSource,
        'foreach \(KMonoBehaviour component in boundPowerComponents\)[\s\S]*?component == null \|\| component\.GetMyWorldId\(\) != activeWorldId[\s\S]*?active_world_changed_during_component_recheck'
    )
    battery_identity_registered = [regex]::IsMatch(
        $SurvivalReaderSource,
        'TryRegisterPowerComponentIdentity\(\s*battery\.GetInstanceID\(\)'
    )
    consumer_identity_registered = [regex]::IsMatch(
        $SurvivalReaderSource,
        'TryRegisterPowerComponentIdentity\(\s*consumer\.GetInstanceID\(\)'
    )
    generator_identity_registered = [regex]::IsMatch(
        $SurvivalReaderSource,
        'TryRegisterPowerComponentIdentity\(\s*generator\.GetInstanceID\(\)'
    )
    bounded_over_capacity_rejected = [regex]::IsMatch(
        $SurvivalReaderSource,
        'storedJoules > capacityJoules'
    )
    percent_uses_published_gauges = [regex]::IsMatch(
        $SurvivalReaderSource,
        'batteryPercent = publishedStoredJoules / publishedCapacityJoules \* 100\.0;'
    )
    quantized_zero_capacity_is_not_divided = [regex]::IsMatch(
        $SurvivalReaderSource,
        'publishedCapacityJoules > 0\.0[\s\S]*?publishedCapacityJoules <= 0\.0'
    )
}
$FailedPowerShapeChecks = @(
    $PowerProductionShapeChecks.GetEnumerator() |
        Where-Object { -not [bool]$_.Value } |
        ForEach-Object { $_.Key }
)
if ($FailedPowerShapeChecks.Count -gt 0) {
    throw "Power production shape checks failed: $($FailedPowerShapeChecks -join ', ')"
}

function Assert-Accumulate {
    param(
        [double]$Initial,
        [double]$Value,
        [bool]$ExpectedAccepted,
        [double]$ExpectedTotal
    )

    $Arguments = [object[]]@($Initial, $Value)
    $Accepted = [bool]$AccumulateMethod.Invoke($null, $Arguments)
    $ActualTotal = [double]$Arguments[0]
    if ($Accepted -ne $ExpectedAccepted -or $ActualTotal -ne $ExpectedTotal) {
        throw "Accumulator mismatch: initial=$Initial value=$Value accepted=$Accepted total=$ActualTotal"
    }
}

Assert-Accumulate -Initial 1.0 -Value ([double]::PositiveInfinity) -ExpectedAccepted $false -ExpectedTotal 1.0
Assert-Accumulate -Initial 1.0 -Value ([double]::NegativeInfinity) -ExpectedAccepted $false -ExpectedTotal 1.0
Assert-Accumulate -Initial 1.0 -Value ([double]::NaN) -ExpectedAccepted $false -ExpectedTotal 1.0
Assert-Accumulate -Initial 1.0 -Value 5.5 -ExpectedAccepted $true -ExpectedTotal 6.5
Assert-Accumulate -Initial 1.0 -Value -2.0 -ExpectedAccepted $false -ExpectedTotal 1.0
Assert-Accumulate -Initial ([double]::MaxValue) -Value ([double]::MaxValue) -ExpectedAccepted $false -ExpectedTotal ([double]::MaxValue)

$SurvivalResultType = $Assembly.GetType(
    "OniAiProbeMod.Probe.SurvivalTelemetryReadResult",
    $true
)
$PublishBatteryMethod = $SurvivalReaderType.GetMethod(
    "PublishBatteryTelemetry",
    $StaticNonPublic
)
$InstanceNonPublic = [System.Reflection.BindingFlags]"Instance,NonPublic"
$PowerProperty = $SurvivalResultType.GetProperty("Power", $InstanceNonPublic)
$QualityProperty = $SurvivalResultType.GetProperty("Quality", $InstanceNonPublic)

function Assert-BatteryValue {
    param(
        [System.Collections.Generic.Dictionary[string, object]]$Power,
        [string]$Field,
        [AllowNull()][object]$Expected,
        [string]$CaseName
    )

    if (-not $Power.ContainsKey($Field)) {
        throw "Battery case '$CaseName' did not publish the '$Field' key."
    }
    $Actual = $Power[$Field]
    if ($null -eq $Expected) {
        if ($null -ne $Actual) {
            throw "Battery case '$CaseName' published $Field=$Actual instead of null."
        }
        return
    }
    if ($null -eq $Actual -or [double]$Actual -ne [double]$Expected) {
        throw "Battery case '$CaseName' published $Field=$Actual instead of $Expected."
    }
}

function Assert-BatteryMode {
    param(
        [System.Collections.Generic.Dictionary[string, object]]$Power,
        [string]$Expected,
        [string]$CaseName
    )

    if (-not $Power.ContainsKey("storage_mode")) {
        throw "Battery case '$CaseName' did not publish the 'storage_mode' key."
    }
    $Actual = [string]$Power["storage_mode"]
    if ($Actual -ne $Expected) {
        throw "Battery case '$CaseName' published storage_mode='$Actual' instead of '$Expected'."
    }
}

function Assert-BatteryQuality {
    param(
        [System.Collections.Generic.Dictionary[string, object]]$Quality,
        [string]$Field,
        [string]$ExpectedStatus,
        [string]$ExpectedCompleteness,
        [string]$CaseName
    )

    $QualityKey = "power.$Field"
    if (-not $Quality.ContainsKey($QualityKey)) {
        throw "Battery case '$CaseName' did not publish quality for '$QualityKey'."
    }
    $ActualStatus = [string]$Quality[$QualityKey]["status"]
    if ($ActualStatus -ne $ExpectedStatus) {
        throw "Battery case '$CaseName' published $QualityKey status '$ActualStatus' instead of '$ExpectedStatus'."
    }
    $ActualCompleteness = [string]$Quality[$QualityKey]["completeness"]
    if ($ActualCompleteness -ne $ExpectedCompleteness) {
        throw "Battery case '$CaseName' published $QualityKey completeness '$ActualCompleteness' instead of '$ExpectedCompleteness'."
    }
}

function Invoke-BatteryTelemetryCase {
    param(
        [string]$Name,
        [double[]]$StoredValues,
        [double[]]$CapacityValues,
        [AllowNull()][object]$ExpectedStored,
        [AllowNull()][object]$ExpectedCapacity,
        [AllowNull()][object]$ExpectedPercent,
        [string]$ExpectedStorageMode,
        [string]$ExpectedStorageModeStatus,
        [string]$ExpectedStorageModeCompleteness,
        [string]$ExpectedStoredStatus,
        [string]$ExpectedStoredCompleteness,
        [string]$ExpectedCapacityStatus,
        [string]$ExpectedCapacityCompleteness,
        [string]$ExpectedPercentStatus,
        [string]$ExpectedPercentCompleteness
    )

    $StoredTotal = [double]0.0
    $StoredFinite = $true
    foreach ($Value in $StoredValues) {
        $Arguments = [object[]]@($StoredTotal, $Value)
        if (-not [bool]$AccumulateMethod.Invoke($null, $Arguments)) {
            $StoredFinite = $false
        }
        $StoredTotal = [double]$Arguments[0]
    }

    $CapacityTotal = [double]0.0
    $CapacityUnbounded = $false
    $CapacityInvalid = $false
    foreach ($Value in $CapacityValues) {
        $Arguments = [object[]]@(
            $CapacityTotal,
            $Value,
            $CapacityUnbounded,
            $CapacityInvalid
        )
        [void]$AccumulateCapacityMethod.Invoke($null, $Arguments)
        $CapacityTotal = [double]$Arguments[0]
        $CapacityUnbounded = [bool]$Arguments[2]
        $CapacityInvalid = [bool]$Arguments[3]
    }
    $StorageMode = if ($CapacityInvalid) {
        "invalid"
    }
    elseif ($CapacityUnbounded) {
        "unbounded"
    }
    else {
        "bounded"
    }

    $Result = [System.Activator]::CreateInstance($SurvivalResultType, $true)
    [void]$PublishBatteryMethod.Invoke(
        $null,
        [object[]]@(
            $Result,
            "offline_battery_fixture",
            $StoredTotal,
            $StoredFinite,
            $CapacityTotal,
            $StorageMode
        )
    )
    $Power = $PowerProperty.GetValue($Result)
    $Quality = $QualityProperty.GetValue($Result)

    Assert-BatteryMode $Power $ExpectedStorageMode $Name
    Assert-BatteryValue $Power "stored_joules" $ExpectedStored $Name
    Assert-BatteryValue $Power "capacity_joules" $ExpectedCapacity $Name
    Assert-BatteryValue $Power "battery_percent" $ExpectedPercent $Name
    Assert-BatteryQuality $Quality "storage_mode" $ExpectedStorageModeStatus $ExpectedStorageModeCompleteness $Name
    Assert-BatteryQuality $Quality "stored_joules" $ExpectedStoredStatus $ExpectedStoredCompleteness $Name
    Assert-BatteryQuality $Quality "capacity_joules" $ExpectedCapacityStatus $ExpectedCapacityCompleteness $Name
    Assert-BatteryQuality $Quality "battery_percent" $ExpectedPercentStatus $ExpectedPercentCompleteness $Name
}

$BatteryTelemetryCases = @(
    @{
        Name = "bounded"
        StoredValues = [double[]]@(10.0, 15.0)
        CapacityValues = [double[]]@(40.0, 60.0)
        ExpectedStored = [double]25.0
        ExpectedCapacity = [double]100.0
        ExpectedPercent = [double]25.0
        ExpectedStorageMode = "bounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "observed"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "observed"
        ExpectedPercentCompleteness = "complete"
    },
    @{
        Name = "published_rounding_seam"
        StoredValues = [double[]]@(1.0004)
        CapacityValues = [double[]]@(3.0004)
        ExpectedStored = [double]1.0
        ExpectedCapacity = [double]3.0
        ExpectedPercent = [double]33.333
        ExpectedStorageMode = "bounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "observed"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "observed"
        ExpectedPercentCompleteness = "complete"
    },
    @{
        Name = "capacity_rounds_to_zero"
        StoredValues = [double[]]@(0.0004)
        CapacityValues = [double[]]@(0.0004)
        ExpectedStored = [double]0.0
        ExpectedCapacity = [double]0.0
        ExpectedPercent = $null
        ExpectedStorageMode = "bounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "observed"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "unsupported"
        ExpectedPercentCompleteness = "complete"
    },
    @{
        Name = "bounded_stored_exceeds_capacity_is_rejected"
        StoredValues = [double[]]@(60.0, 90.0)
        CapacityValues = [double[]]@(40.0, 60.0)
        ExpectedStored = $null
        ExpectedCapacity = [double]100.0
        ExpectedPercent = $null
        ExpectedStorageMode = "bounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "error"
        ExpectedStoredCompleteness = "unknown"
        ExpectedCapacityStatus = "observed"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "error"
        ExpectedPercentCompleteness = "unknown"
    },
    @{
        Name = "zero_capacity"
        StoredValues = [double[]]@(0.0)
        CapacityValues = [double[]]@(0.0)
        ExpectedStored = [double]0.0
        ExpectedCapacity = [double]0.0
        ExpectedPercent = $null
        ExpectedStorageMode = "bounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "observed"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "unsupported"
        ExpectedPercentCompleteness = "complete"
    },
    @{
        Name = "positive_infinity_capacity"
        StoredValues = [double[]]@(10.0, 15.0)
        CapacityValues = [double[]]@([double]::PositiveInfinity)
        ExpectedStored = [double]25.0
        ExpectedCapacity = $null
        ExpectedPercent = $null
        ExpectedStorageMode = "unbounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "unsupported"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "unsupported"
        ExpectedPercentCompleteness = "complete"
    },
    @{
        Name = "mixed_finite_positive_infinity_capacity"
        StoredValues = [double[]]@(10.0, 15.0)
        CapacityValues = [double[]]@(40.0, [double]::PositiveInfinity)
        ExpectedStored = [double]25.0
        ExpectedCapacity = $null
        ExpectedPercent = $null
        ExpectedStorageMode = "unbounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "unsupported"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "unsupported"
        ExpectedPercentCompleteness = "complete"
    },
    @{
        Name = "nan_capacity"
        StoredValues = [double[]]@(25.0)
        CapacityValues = [double[]]@(40.0, [double]::NaN)
        ExpectedStored = [double]25.0
        ExpectedCapacity = $null
        ExpectedPercent = $null
        ExpectedStorageMode = "invalid"
        ExpectedStorageModeStatus = "error"
        ExpectedStorageModeCompleteness = "unknown"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "error"
        ExpectedCapacityCompleteness = "unknown"
        ExpectedPercentStatus = "error"
        ExpectedPercentCompleteness = "unknown"
    },
    @{
        Name = "negative_infinity_capacity"
        StoredValues = [double[]]@(25.0)
        CapacityValues = [double[]]@(40.0, [double]::NegativeInfinity)
        ExpectedStored = [double]25.0
        ExpectedCapacity = $null
        ExpectedPercent = $null
        ExpectedStorageMode = "invalid"
        ExpectedStorageModeStatus = "error"
        ExpectedStorageModeCompleteness = "unknown"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "error"
        ExpectedCapacityCompleteness = "unknown"
        ExpectedPercentStatus = "error"
        ExpectedPercentCompleteness = "unknown"
    },
    @{
        Name = "positive_infinity_with_nan_is_invalid"
        StoredValues = [double[]]@(25.0)
        CapacityValues = [double[]]@([double]::PositiveInfinity, [double]::NaN)
        ExpectedStored = [double]25.0
        ExpectedCapacity = $null
        ExpectedPercent = $null
        ExpectedStorageMode = "invalid"
        ExpectedStorageModeStatus = "error"
        ExpectedStorageModeCompleteness = "unknown"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "error"
        ExpectedCapacityCompleteness = "unknown"
        ExpectedPercentStatus = "error"
        ExpectedPercentCompleteness = "unknown"
    },
    @{
        Name = "finite_capacity_accumulation_overflow"
        StoredValues = [double[]]@(50.0)
        CapacityValues = [double[]]@([double]::MaxValue, [double]::MaxValue)
        ExpectedStored = [double]50.0
        ExpectedCapacity = $null
        ExpectedPercent = $null
        ExpectedStorageMode = "invalid"
        ExpectedStorageModeStatus = "error"
        ExpectedStorageModeCompleteness = "unknown"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "error"
        ExpectedCapacityCompleteness = "unknown"
        ExpectedPercentStatus = "error"
        ExpectedPercentCompleteness = "unknown"
    },
    @{
        Name = "negative_finite_capacity"
        StoredValues = [double[]]@(25.0)
        CapacityValues = [double[]]@(40.0, -1.0)
        ExpectedStored = [double]25.0
        ExpectedCapacity = $null
        ExpectedPercent = $null
        ExpectedStorageMode = "invalid"
        ExpectedStorageModeStatus = "error"
        ExpectedStorageModeCompleteness = "unknown"
        ExpectedStoredStatus = "observed"
        ExpectedStoredCompleteness = "complete"
        ExpectedCapacityStatus = "error"
        ExpectedCapacityCompleteness = "unknown"
        ExpectedPercentStatus = "error"
        ExpectedPercentCompleteness = "unknown"
    },
    @{
        Name = "stored_invalid_capacity_bounded"
        StoredValues = [double[]]@(10.0, -1.0)
        CapacityValues = [double[]]@(40.0, 60.0)
        ExpectedStored = $null
        ExpectedCapacity = [double]100.0
        ExpectedPercent = $null
        ExpectedStorageMode = "bounded"
        ExpectedStorageModeStatus = "observed"
        ExpectedStorageModeCompleteness = "complete"
        ExpectedStoredStatus = "error"
        ExpectedStoredCompleteness = "unknown"
        ExpectedCapacityStatus = "observed"
        ExpectedCapacityCompleteness = "complete"
        ExpectedPercentStatus = "error"
        ExpectedPercentCompleteness = "unknown"
    }
)
foreach ($Case in $BatteryTelemetryCases) {
    Invoke-BatteryTelemetryCase @Case
}

function New-TestRecord {
    param(
        [string]$Schema,
        [string]$Event,
        [string]$SessionId,
        [long]$Sequence,
        [long]$SnapshotSequence = 0
    )

    $Record = [System.Collections.Generic.Dictionary[string, object]]::new()
    $Record["schema"] = $Schema
    $Record["event"] = $Event
    $Record["session_id"] = $SessionId
    $Record["seq"] = $Sequence
    $Record["ts"] = [System.DateTimeOffset]::Now.ToString(
        "o",
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    $Record["game_build"] = "cl-test"
    $Record["mod_version"] = "test"
    $Record["save_id_hash"] = "sha256:" + ("0" * 64)
    $Record["save_dirty"] = $false
    $Record["world_id"] = 0
    if ($Event -eq "state_snapshot") {
        $Record["snapshot_seq"] = $SnapshotSequence
        $Record["snapshot_id"] = "$SessionId`:$Sequence"
        $Record["quality"] = [System.Collections.Generic.Dictionary[string, object]]::new()
        $ObservationScope = [System.Collections.Generic.Dictionary[string, object]]::new()
        $ObservationScope["active_world_id"] = 0
        $ObservationScope["populated_world_ids"] = [int[]]@(0)
        $ObservationScope["covered_world_ids"] = [int[]]@(0)
        $ObservationScope["complete_for_populated_worlds"] = $true
        $Record["observation_scope"] = $ObservationScope
        $Sample = [System.Collections.Generic.Dictionary[string, object]]::new()
        $Sample["interval_seconds"] = [double]1.0
        $Record["sample"] = $Sample
        $WriterStatus = [System.Collections.Generic.Dictionary[string, object]]::new()
        $WriterStatus["queue_depth"] = 0
        $WriterStatus["dropped_snapshot_count"] = [long]0
        $Record["writer"] = $WriterStatus
    }
    return $Record
}

$ProbeWriterFlags = [System.Reflection.BindingFlags]"Static,Public,NonPublic"
$StateLogProperty = $ProbeWriterType.GetProperty("StateLogPath", $ProbeWriterFlags)
$StateLogSetter = $StateLogProperty.GetSetMethod($true)
$PendingQueue = $ProbeWriterType.GetField(
    "PendingRecords",
    $StaticNonPublic
).GetValue($null)
$FlushMethod = $ProbeWriterType.GetMethod("FlushPendingRecords", $StaticNonPublic)
if ($PendingQueue.Count -ne 0) {
    throw "Probe writer queue was not empty before the isolated pipeline test."
}

$TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$PipelineLog = [System.IO.Path]::Combine(
    $TempRoot,
    "oni-probe-writer-" + [System.Guid]::NewGuid().ToString("N") + ".jsonl"
)
try {
    [void]$StateLogSetter.Invoke($null, @($PipelineLog))

    $SessionId = "session-writer-test"
    $SessionStart = New-TestRecord "oni.event.v2" "session_started" $SessionId 1
    $SessionStart["enum_fixture"] = [System.DayOfWeek]::Monday
    $PendingQueue.Enqueue($SessionStart)

    $BadSnapshot = New-TestRecord "oni.state.v2" "state_snapshot" $SessionId 2 1
    $NestedBadValue = [System.Collections.Generic.Dictionary[string, object]]::new()
    $NestedBadValue["value"] = [double]::PositiveInfinity
    $BadSnapshot["nested"] = $NestedBadValue
    $PendingQueue.Enqueue($BadSnapshot)

    $PendingQueue.Enqueue((New-TestRecord "oni.state.v2" "state_snapshot" $SessionId 3 2))

    $CycleEvent = New-TestRecord "oni.event.v2" "diagnostic" $SessionId 4
    $CyclePayload = [System.Collections.Generic.Dictionary[string, object]]::new()
    $CyclePayload["self"] = $CyclePayload
    $CycleEvent["payload"] = $CyclePayload
    $PendingQueue.Enqueue($CycleEvent)

    $DeepEvent = New-TestRecord "oni.event.v2" "diagnostic" $SessionId 5
    $DeepPayload = [System.Collections.Generic.Dictionary[string, object]]::new()
    $Cursor = $DeepPayload
    foreach ($Index in 1..70) {
        $Next = [System.Collections.Generic.Dictionary[string, object]]::new()
        $Cursor["next"] = $Next
        $Cursor = $Next
    }
    $DeepEvent["payload"] = $DeepPayload
    $PendingQueue.Enqueue($DeepEvent)

    $NodeEvent = New-TestRecord "oni.event.v2" "diagnostic" $SessionId 6
    $NodeEvent["payload"] = [System.Linq.Enumerable]::Range(0, 1000001)
    $PendingQueue.Enqueue($NodeEvent)

    $PendingQueue.Enqueue((New-TestRecord "oni.state.v2" "state_snapshot" $SessionId 7 3))

    $NextSessionId = "session-writer-test-reset"
    $PendingQueue.Enqueue((New-TestRecord "oni.event.v2" "session_started" $NextSessionId 1))
    $PendingQueue.Enqueue((New-TestRecord "oni.state.v2" "state_snapshot" $NextSessionId 2 1))

    [void]$FlushMethod.Invoke($null, @())
    if ($PendingQueue.Count -ne 0) {
        throw "Probe writer queue did not drain during the isolated pipeline test."
    }

    $Lines = @(Get-Content -Encoding UTF8 -LiteralPath $PipelineLog)
    if ($Lines.Count -ne 9) {
        throw "Expected 9 complete JSONL records, observed $($Lines.Count)."
    }
    $PipelinePayload = ($Lines -join [System.Environment]::NewLine) + [System.Environment]::NewLine
    $PipelinePayload | & python -c 'import json, sys; [json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))) for line in sys.stdin if line.strip()]'
    if ($LASTEXITCODE -ne 0) {
        throw "Writer pipeline output failed strict JSONL parsing."
    }

    $Records = @($Lines | ForEach-Object { $_ | ConvertFrom-Json })
    if ($Records[0].enum_fixture -ne "Monday") {
        throw "Enum did not survive the complete writer pipeline as a JSON string."
    }
    if ($Records[1].event -ne "probe_error" -or
        $Records[1].stage -ne "writer_validation" -or
        $Records[1].reason -ne "non_finite_number" -or
        [long]$Records[1].seq -ne 2) {
        throw "Nested non-finite snapshot was not replaced in its original sequence slot."
    }
    if ($Records[2].writer.fault -ne "record_validation_failed" -or
        [long]$Records[2].writer.fault_count -ne 1 -or
        [long]$Records[2].writer.non_finite_numeric_count -ne 1) {
        throw "The first post-fault snapshot did not carry same-session sticky diagnostics."
    }
    if ($Records[3].reason -ne "reference_cycle" -or
        $Records[4].reason -ne "depth_limit_exceeded" -or
        $Records[5].reason -ne "node_limit_exceeded") {
        throw "Cycle/depth/node guards did not produce fixed writer validation events."
    }
    if ($Records[6].writer.fault -ne "record_validation_failed" -or
        [long]$Records[6].writer.fault_count -ne 4) {
        throw "Writer faults did not remain sticky through the original session."
    }
    if (($Records[8].writer.PSObject.Properties.Name -contains "fault") -or
        [long]$Records[8].writer.fault_count -ne 0 -or
        [long]$Records[8].writer.non_finite_numeric_count -ne 0) {
        throw "A new session_started record did not reset writer fault diagnostics."
    }

    $BridgeAuditCode = @'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
log_path = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root))
from bridge import oni_bridge

def reject_constant(value):
    raise ValueError(value)

records = [
    json.loads(line, parse_constant=reject_constant)
    for line in log_path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
event_only = oni_bridge.ReplayState()
event_only.process_many(records[:2])
event_health = event_only.health(event_only.effective_latest(), {}, replay_mode=False)
assert "writer_fault" in event_health["trust_blockers"]

snapshot_only_record = dict(records[2])
snapshot_only_record["seq"] = 2
snapshot_only_record["snapshot_seq"] = 1
snapshot_only_record["snapshot_id"] = f'{snapshot_only_record["session_id"]}:2'
snapshot_only = oni_bridge.ReplayState()
snapshot_only.process(records[0])
snapshot_only.process(snapshot_only_record)
snapshot_health = snapshot_only.health(snapshot_only.effective_latest(), {}, replay_mode=False)
assert "writer_fault" in snapshot_health["trust_blockers"]

first = oni_bridge.ReplayState()
first.process_many(records[:7])
first_health = first.health(first.effective_latest(), {}, replay_mode=False)
assert first_health["invalid_json_count"] == 0
assert first_health["seq_gap_count"] == 0
assert first_health["snapshot_seq_gap_count"] == 1
assert first_health["latest_seq"] == 7
assert "writer_fault" in first_health["trust_blockers"]
assert "snapshot_sequence_gap" in first_health["trust_blockers"]

first.process_many(records[7:])
second_health = first.health(first.effective_latest(), {}, replay_mode=False)
assert second_health["status"] == "ok"
assert second_health["trust_ready"] is True
assert "writer_fault" not in second_health["trust_blockers"]
print(json.dumps({
    "first_session_invalid_json_count": first_health["invalid_json_count"],
    "first_session_seq_gap_count": first_health["seq_gap_count"],
    "first_session_snapshot_seq_gap_count": first_health["snapshot_seq_gap_count"],
    "probe_error_writer_fault": "writer_fault" in event_health["trust_blockers"],
    "snapshot_writer_fault": "writer_fault" in snapshot_health["trust_blockers"],
    "next_session_trust_ready": second_health["trust_ready"],
}, sort_keys=True))
'@
    $BridgeAuditJson = & python -c $BridgeAuditCode $ProjectRoot $PipelineLog
    if ($LASTEXITCODE -ne 0) {
        throw "Bridge did not preserve the writer fault and snapshot gap semantics."
    }
}
finally {
    [void]$StateLogSetter.Invoke($null, @($null))
    $ResolvedPipelineLog = [System.IO.Path]::GetFullPath($PipelineLog)
    if (-not $ResolvedPipelineLog.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Temporary writer log escaped the system temp directory."
    }
    if (Test-Path -LiteralPath $ResolvedPipelineLog) {
        Remove-Item -LiteralPath $ResolvedPipelineLog -Force
    }
}

[pscustomobject]@{
    status = "passed"
    assembly = $AssemblyPath
    non_finite_cases = $NonFiniteCases.Count
    finite_cases = $FiniteCases.Count
    enum_cases = 1
    accumulator_cases = 6
    duplicant_world_binding_null_cases = 1
    duplicant_priority_binding_null_cases = 1
    duplicant_priority_shape_checks = $DuplicantPriorityShapeChecks.Count
    observation_scope_world_binding_null_cases = 1
    observation_scope_identity_limit = $ObservationScopeIdentityLimit
    observation_scope_shape_checks = $ObservationScopeShapeChecks.Count
    power_registry_identity_cases = 7
    power_registry_limit = $PowerRegistryLimit
    power_production_shape_checks = $PowerProductionShapeChecks.Count
    battery_telemetry_cases = $BatteryTelemetryCases.Count
    assembly_backed_power_reflection = $true
    assembly_backed_power_world_id_api = $true
    pipeline_records = 9
    writer_fault_replacements = 4
    node_guard = $true
    session_reset = $true
    bridge_audit = $BridgeAuditJson | ConvertFrom-Json
    strict_json = $true
} | ConvertTo-Json
