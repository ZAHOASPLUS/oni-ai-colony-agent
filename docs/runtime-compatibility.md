# Runtime compatibility detector

`scripts/detect_runtime_compatibility.py` performs a read-only inspection of
the ONI installation and this mod project. It does not launch ONI, load or
modify saves, install a DLL, or change Steam/mod files. A report is written
only to the path explicitly passed with `--output`.

The detector discovers the current `OxygenNotIncluded_Data/Managed` directory,
hashes `Assembly-CSharp.dll`, and extracts `KleiVersion.ChangeList` and
`BuildBranch`. `ilspycmd` is used when available. Without it, only a strongly
labelled `cl-N` string is accepted; the detector reports an unknown build
instead of guessing from an unrelated number in a binary.

It parses the `.csproj`, `mod_info.yaml`, `mod.yaml`, and `Mod.cs` to report the
target framework, ONI reference paths/hashes, APIVersion, static ID, mod
version, and local build artifacts. No game-build allow-list is embedded. An
exact deployment pin can be supplied by the operator:

```powershell
py -3 .\scripts\detect_runtime_compatibility.py `
  --managed-dir "C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"

# Optional exact policy checks (the values are not persisted by the detector)
py -3 .\scripts\detect_runtime_compatibility.py `
  --expected-build cl-744825 `
  --expected-assembly-sha256 <sha256> `
  --strict
```

PowerShell users can call the equivalent wrapper:

```powershell
.\scripts\detect-runtime-compatibility.ps1 -Strict
```

The JSON schema is `oni.runtime-compatibility.v1`. Stable top-level fields for
automation are `game_build`, `assembly_csharp_sha256`, and
`mod_target_info`; detailed evidence is under `game`, `checks`, and
`diagnostics`. Status is `compatible`, `needs_review`, or `incompatible`.
`needs_review` exits zero by default so an informational probe does not stop a
bootstrap; pass `--strict` when unknown metadata should fail a deployment gate.

Run the dependency-free tests with:

```powershell
py -3 .\scripts\test_runtime_compatibility.py
```

