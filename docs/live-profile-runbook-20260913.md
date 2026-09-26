# Live profile read-only runbook

This runbook is for a dedicated Windows account. It does not modify the old
transaction and does not submit commands to the ONI action queues.

## Administrator phase

Run from an elevated PowerShell window. The account name must be unused and no
longer than 20 characters.

```powershell
cd C:\Users\user\Documents\ONI
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\create-live-profile-user.ps1 -UserName ONI-Lab
```

Sign out, then sign in interactively as `ONI-Lab`. Windows creates the profile
directory during first login. Do not pre-create a `C:\Users\ONI-Lab` path.

## First-login path inventory

From the ONI-Lab session, resolve the actual game path and create the Bridge
directory using the discovered Documents path:

```powershell
$game = 'C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded.exe'
$docs = [Environment]::GetFolderPath('MyDocuments')
$bridge = Join-Path $docs 'Klei\OxygenNotIncluded\ai-bridge'
New-Item -ItemType Directory -Force -Path $bridge | Out-Null
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-live-profile.ps1 `
  -Action Inventory -GameExe $game -BridgeDir $bridge -ExpectedUserName ONI-Lab
```

The inventory must show the actual `USERPROFILE`, Documents, LocalLow,
`BridgeDir`, and EXE hash. Freeze the displayed EXE hash. The Bridge directory
must be under the actual ONI-Lab profile and must not overlap the old user's
ONI root.

## Read-only Steam launch and identity capture

Disable Steam Cloud for this account, copy only a verified test save into the
ONI-Lab save directory, and launch ONI through Steam. Do not open the old cloud
save. While exactly one ONI process is running, capture it:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-live-profile.ps1 `
  -Action AwaitSteam -GameExe $game -BridgeDir $bridge `
  -ExpectedGameExeSha256 'sha256:<HASH_FROM_INVENTORY>' `
  -ExpectedUserName ONI-Lab `
  -ForbiddenRoot 'C:\Users\user' `
  -TimeoutSeconds 180
```

The result is written to `$bridge\live-profile-process.json`. It must report
`launch_mode=manual_steam_rendezvous`, the actual ONI-Lab owner, one PID, and the same EXE
hash. Then run the repository's `capture_validation_process_identity.py
create` and `verify` commands using that PID and the same path/hash anchors.

Only after process identity verification and the configured runtime/startup
anchors are available may `run-bridge.ps1` be started. Supply all nine startup
expectations; do not use `-Once`, partial expectations, or an action queue.

## Exit

Close ONI normally. If it remains open, use the recorded PID only after
`start-live-profile.ps1 -Action Status` confirms the same executable path and
creation time, then use `-Action Stop`. Do not terminate processes by name and
do not delete the account or evidence automatically.
