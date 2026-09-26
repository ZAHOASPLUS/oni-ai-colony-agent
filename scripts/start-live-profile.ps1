<#
.SYNOPSIS
  Inventory a real Windows profile and rendezvous with one ONI process started through Steam.
.DESCRIPTION
  Run after signing in to the target account. Paths are discovered from the
  actual profile; no C:\Users\<name> path is constructed. This helper never
  starts Steam or ONI and never uses process fixtures. Start ONI through Steam,
  then use AwaitSteam to capture one exact PID. Start Bridge separately with
  run-bridge.ps1 after identity verification.
#>
[CmdletBinding(SupportsShouldProcess=$true,ConfirmImpact='High')]
param(
  [ValidateSet('Inventory','AwaitSteam','Status','Stop')][string]$Action='Inventory',
  [string]$GameExe,
  [Parameter(Mandatory=$true)][string]$BridgeDir,
  [string]$ExpectedGameExeSha256,
  [string]$ExpectedUserName,
  [string]$ForbiddenRoot,
  [string]$StateFile,
  [ValidateRange(1,600)][int]$TimeoutSeconds=120,
  [ValidateRange(100,5000)][int]$PollMilliseconds=500
)
$ErrorActionPreference='Stop'
function Full([string]$p){if([string]::IsNullOrWhiteSpace($p)){throw 'path is required'};[IO.Path]::GetFullPath($p)}
function Hash([string]$p){'sha256:'+(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()}
function ExpectedHash([string]$h){if([string]::IsNullOrWhiteSpace($h)-or $h-notmatch '^(?i:sha256:)?[0-9a-f]{64}$'){throw 'ExpectedGameExeSha256 must be a 64-hex SHA-256, optionally prefixed with sha256:'};'sha256:'+($h-replace '^(?i:sha256:)','').ToLowerInvariant()}
function Assert-NoReparse([string]$p){$cursor=Full $p;while($true){if(Test-Path -LiteralPath $cursor){$item=Get-Item -LiteralPath $cursor -Force -ErrorAction Stop;if(($item.Attributes-band[IO.FileAttributes]::ReparsePoint)-ne 0){throw 'path contains a reparse point'}};$parent=[IO.Directory]::GetParent($cursor);if($null-eq $parent){break};$cursor=$parent.FullName}}
function Norm([string]$p){(Full $p).TrimEnd('\')}
function Same([string]$a,[string]$b){[string]::Equals((Norm $a),(Norm $b),[StringComparison]::OrdinalIgnoreCase)}
function Desc([string]$p,[string]$r){(Full $p).StartsWith((Norm $r)+'\',[StringComparison]::OrdinalIgnoreCase)}
function Owner([int]$ProcessId){$x=Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop;$o=$x|Invoke-CimMethod -MethodName GetOwner -ErrorAction SilentlyContinue;if($o.ReturnValue -eq 0){"$($o.Domain)\$($o.User)"}else{''}}
function Identity([int]$ProcessId){$x=Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue;if($null -eq $x){return $null};if([string]::IsNullOrWhiteSpace($x.ExecutablePath)){throw 'process image path unavailable'};$proc=Get-Process -Id $ProcessId -ErrorAction Stop;if($null -eq $proc.StartTime){throw 'process creation time unavailable'};$c=$proc.StartTime.ToUniversalTime();[pscustomobject]@{pid=$ProcessId;path=(Full $x.ExecutablePath);name=[string]$x.Name;creation_utc=$c.ToString('o');owner=(Owner $ProcessId)}}
function Parse-CreationUtc([object]$Value){
  if($null -eq $Value){throw 'process creation time is missing'}
  if($Value -is [DateTimeOffset]){return $Value.ToUniversalTime()}
  if($Value -is [DateTime]){$date=[DateTime]$Value;if($date.Kind -eq [DateTimeKind]::Unspecified){throw 'process creation time timezone is ambiguous'};return ([DateTimeOffset]$date).ToUniversalTime()}
  if(-not($Value -is [string])){throw 'process creation time type is invalid'}
  $text=$Value.Trim();if([string]::IsNullOrWhiteSpace($text)){throw 'process creation time is missing'};if($text-notmatch '(?i)(?:Z|[+-]\d{2}:?\d{2})$'){throw 'process creation time timezone is missing or ambiguous'}
  try{return [DateTimeOffset]::Parse($text,[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()}catch{throw 'process creation time is invalid'}
}
$profile=Full $env:USERPROFILE;$documents=[Environment]::GetFolderPath('MyDocuments');$localLow=Join-Path $profile 'AppData\LocalLow';$localApp=Join-Path $profile 'AppData\Local'
if([string]::IsNullOrWhiteSpace($documents)-or -not(Desc $documents $profile)){throw 'actual Documents path unavailable or outside USERPROFILE'}
if(-not(Test-Path -LiteralPath $localLow -PathType Container)){throw 'actual LocalLow path unavailable'}
if($ExpectedUserName -and $env:USERNAME -cne $ExpectedUserName){throw "logged-in user mismatch: $env:USERNAME"}
$BridgeDir=Full $BridgeDir;if(-not(Desc $BridgeDir $profile)){throw 'BridgeDir must be inside actual USERPROFILE'};if(-not(Test-Path -LiteralPath $BridgeDir -PathType Container)){throw 'BridgeDir must already exist'};Assert-NoReparse $BridgeDir
if($ForbiddenRoot-and(Same $profile $ForbiddenRoot-or Desc $profile $ForbiddenRoot-or Desc $ForbiddenRoot $profile)){throw 'actual profile overlaps ForbiddenRoot'}
if(-not $StateFile){$StateFile=Join-Path $BridgeDir 'live-profile-process.json'};$StateFile=Full $StateFile;if(-not(Desc $StateFile $BridgeDir)){throw 'StateFile must be inside BridgeDir'};Assert-NoReparse (Split-Path $StateFile -Parent)
if($Action -eq 'Inventory'){$game=$null;$gameHash=$null;if($GameExe){$game=Full $GameExe;if(Test-Path -LiteralPath $game -PathType Leaf){$gameHash=Hash $game}};[ordered]@{schema='oni.live-profile-inventory.v1';recorded_at_utc=[DateTime]::UtcNow.ToString('o');user_name=$env:USERNAME;user_profile=$profile;documents=$documents;local_low=$localLow;local_app_data=$localApp;bridge_dir=$BridgeDir;state_file=$StateFile;game_exe=$game;game_exe_exists=[bool]($gameHash);game_exe_sha256=$gameHash;steam_launch_required=$true}|ConvertTo-Json -Depth 5;return}
if($Action -eq 'AwaitSteam'){
  if(-not $GameExe){throw 'AwaitSteam requires -GameExe'};if(-not $ExpectedGameExeSha256){throw 'AwaitSteam requires explicit -ExpectedGameExeSha256'}
  $GameExe=Full $GameExe;if(-not(Test-Path -LiteralPath $GameExe -PathType Leaf)){throw 'GameExe does not exist'};if((Split-Path $GameExe -Leaf)-cne 'OxygenNotIncluded.exe'){throw 'GameExe must be OxygenNotIncluded.exe'};$actual=Hash $GameExe;$expected=ExpectedHash $ExpectedGameExeSha256;if($actual -cne $expected){throw 'Game executable hash mismatch'}
  if(Test-Path -LiteralPath $StateFile -PathType Leaf){throw 'StateFile already exists; stop or archive its recorded process before capture'}
  $deadline=[DateTime]::UtcNow.AddSeconds($TimeoutSeconds);$found=@();while([DateTime]::UtcNow-lt $deadline){$all=@(Get-CimInstance Win32_Process|Where-Object{$_.Name-ieq 'OxygenNotIncluded.exe'});if($all.Count-gt 1){throw 'more than one ONI process exists; refusing ambiguous capture'};$found=@($all|Where-Object{$_.ExecutablePath-and(Same $_.ExecutablePath $GameExe)});if($found.Count-eq 1){break};Start-Sleep -Milliseconds $PollMilliseconds};if($found.Count-ne 1){throw 'timed out waiting for exactly one ONI process launched through Steam'};$id=Identity ([int]$found[0].ProcessId);$record=[ordered]@{schema='oni.live-profile-process.v1';status='captured';captured_at_utc=[DateTime]::UtcNow.ToString('o');user_name=$env:USERNAME;user_profile=$profile;documents=$documents;local_low=$localLow;local_app_data=$localApp;bridge_dir=$BridgeDir;game_exe=$GameExe;game_exe_sha256=$actual;process_id=$id.pid;process_creation_time_utc=$id.creation_utc;owner=$id.owner;launch_mode='manual_steam_rendezvous';steam_launch_verified=$false};$record|ConvertTo-Json -Depth 5|Set-Content -LiteralPath $StateFile -Encoding UTF8;$record|ConvertTo-Json -Depth 5;return
}
if(-not(Test-Path -LiteralPath $StateFile -PathType Leaf)){if($Action -eq 'Status'){[ordered]@{schema='oni.live-profile-status.v1';status='not_running';process_id=$null;state_file=$StateFile}|ConvertTo-Json;return};throw 'StateFile does not exist'};$state=Get-Content -Raw $StateFile|ConvertFrom-Json;if($state.schema -cne 'oni.live-profile-process.v1'){throw 'StateFile schema is invalid'};$recorded=[int]$state.process_id;if($recorded -le 0){throw 'StateFile process_id is invalid'};$id=Identity $recorded
if($Action -eq 'Status'){
  $status='not_running';if($id){try{$stateCreation=Parse-CreationUtc $state.process_creation_time_utc;$currentCreation=Parse-CreationUtc $id.creation_utc;if($id.name -ieq 'OxygenNotIncluded.exe'-and(Same $id.path $state.game_exe)-and((Hash $id.path) -ceq ([string]$state.game_exe_sha256).ToLowerInvariant())-and([Math]::Abs(($currentCreation-$stateCreation).TotalSeconds)-le 2)){$status='running'}}catch{$status='identity_mismatch'}}
  [ordered]@{schema='oni.live-profile-status.v1';status=$status;process_id=$recorded;state_file=$StateFile}|ConvertTo-Json;return
}
if($null-eq $id-or $id.name -ine 'OxygenNotIncluded.exe'-or -not(Same $id.path $state.game_exe)-or (Hash $id.path) -cne ([string]$state.game_exe_sha256).ToLowerInvariant()){throw 'recorded PID is not the same executable or hash; refusing to stop'}
try{$stateCreation=Parse-CreationUtc $state.process_creation_time_utc;$currentCreation=Parse-CreationUtc $id.creation_utc}catch{throw 'recorded process creation time is invalid'};if([Math]::Abs(($currentCreation-$stateCreation).TotalSeconds)-gt 2){throw 'recorded PID was reused; refusing to stop'}
$proc=Get-Process -Id $recorded -ErrorAction Stop;if($proc.MainWindowHandle -eq 0){throw 'ONI has no main window; refusing non-normal termination'};[void]$proc.CloseMainWindow();if(-not $proc.WaitForExit($TimeoutSeconds*1000)){throw 'ONI did not exit normally within TimeoutSeconds; StateFile retained'};if(Get-Process -Id $recorded -ErrorAction SilentlyContinue){throw 'ONI process remained after normal close; StateFile retained'};$stoppedAt=[DateTime]::UtcNow.ToString('o');$stateRecord=[ordered]@{};foreach($prop in $state.PSObject.Properties){$stateRecord[$prop.Name]=$prop.Value};$stateRecord['status']='stopped';$stateRecord['stopped_at_utc']=$stoppedAt;$stateRecord|ConvertTo-Json -Depth 5|Set-Content -LiteralPath $StateFile -Encoding UTF8;[ordered]@{schema='oni.live-profile-status.v1';status='stopped';process_id=$recorded;stopped_at_utc=$stoppedAt;state_file=$StateFile}|ConvertTo-Json
