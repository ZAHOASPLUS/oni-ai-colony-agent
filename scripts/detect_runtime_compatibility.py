#!/usr/bin/env python3
"""Read-only ONI runtime and mod compatibility detector.

The detector deliberately does not embed a game-build allow-list.  ONI updates
replace ``Assembly-CSharp.dll`` frequently, so compatibility is established from
the files that are actually present on the host and from the mod project's
declared references.  An operator may supply an expected build/hash when a
deployment policy needs an exact pin.

The command writes a report only when ``--output`` is supplied; it never writes
inside the ONI installation, touches saves, starts the game, or changes mods.
It is also importable by the bridge/bootstrap checks:

    from scripts.detect_runtime_compatibility import detect_runtime
    report = detect_runtime(managed_dir=Path("..."))

Exit codes are intended for automation:

* 0: no hard incompatibility was found (``compatible`` or ``needs_review``)
* 1: a required file/metadata check failed, or ``--strict`` rejects review
* 2: command-line/input error
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = "oni.runtime-compatibility.v1"
DEFAULT_REQUIRED_REFERENCES = (
    "0Harmony.dll",
    "Assembly-CSharp.dll",
    "Assembly-CSharp-firstpass.dll",
    "UnityEngine.CoreModule.dll",
    "Newtonsoft.Json.dll",
)
SUPPORTED_TARGET_FRAMEWORKS = frozenset(
    {
        # Existing ONI mod projects use either of these.  Keep this list a
        # capability hint, not a game-build policy.
        "net48",
        "netstandard2.0",
        "netstandard2.1",
    }
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _path(value: os.PathLike[str] | str | None) -> Optional[pathlib.Path]:
    if value is None:
        return None
    text = os.fspath(value).strip()
    if not text:
        return None
    # ``strict=False`` is intentional: a missing path must be represented in
    # the report rather than causing a read/write side effect or a traceback.
    return pathlib.Path(text).expanduser().resolve(strict=False)


def _norm_path_text(value: str | None) -> str:
    """Normalize a path for comparison without requiring it to exist."""

    if not value:
        return ""
    expanded = os.path.expandvars(value.strip())
    candidate = pathlib.Path(expanded).expanduser()
    try:
        return str(candidate.resolve(strict=False)).casefold()
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(str(candidate)))


def sha256_file(path: pathlib.Path, *, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Return a lower-case SHA-256 digest, or ``None`` for unreadable files."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(chunk_size)
                if not block:
                    break
                digest.update(block)
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def _file_record(path: pathlib.Path, *, include_hash: bool = True) -> dict[str, Any]:
    """Describe a file without changing it."""

    record: dict[str, Any] = {
        "path": str(path),
        "file_name": path.name,
        "present": False,
    }
    try:
        stat = path.stat()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return record
    if not path.is_file():
        return record

    record.update(
        {
            "present": True,
            "length": stat.st_size,
            "last_write_time_utc": _dt.datetime.fromtimestamp(
                stat.st_mtime, tz=_dt.timezone.utc
            ).isoformat(timespec="seconds"),
        }
    )
    if include_hash:
        record["sha256"] = sha256_file(path)
    # Python's stdlib has no cross-platform PE version reader.  Keep these
    # fields explicit so consumers can distinguish an unavailable version
    # from a fabricated value.  On Windows, an optional PowerShell/pefile
    # enrichment can be layered on later without changing the schema.
    record["file_version"] = None
    record["product_version"] = None
    return record


def _candidate_steam_roots() -> list[pathlib.Path]:
    """Return cheap, deterministic Steam library candidates.

    This is discovery only.  We intentionally do not recursively scan a whole
    drive.  ``libraryfolders.vdf`` is parsed with a small regex because Steam's
    VDF is not JSON and no dependency is needed for this read-only operation.
    """

    roots: list[pathlib.Path] = []
    seen: set[str] = set()

    def add(value: pathlib.Path | str | None) -> None:
        candidate = _path(value)
        if candidate is None:
            return
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            roots.append(candidate)

    for env_name in ("ProgramFiles(x86)", "ProgramFiles", "PROGRAMFILES(X86)", "PROGRAMFILES"):
        value = os.environ.get(env_name)
        if value:
            add(pathlib.Path(value) / "Steam")

    # Common per-user Steam locations (only direct candidates).
    home = pathlib.Path.home()
    add(home / "AppData" / "Local" / "Steam")
    add(home / ".steam" / "steam")
    add(home / ".local" / "share" / "Steam")

    # A user may expose a library directly for portable/CI installs.
    for env_name in ("STEAM_PATH", "STEAM_ROOT", "STEAM_LIBRARY"):
        add(os.environ.get(env_name))

    # Parse known libraryfolders files and add each library root.
    library_files: list[pathlib.Path] = []
    for steam in tuple(roots):
        library_files.append(steam / "steamapps" / "libraryfolders.vdf")
        library_files.append(steam / "config" / "libraryfolders.vdf")
    for vdf in library_files:
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Handles both `"path" "D:\\SteamLibrary"` and forward slashes.
        for match in re.finditer(r'"path"\s*"([^"]+)"', text, flags=re.IGNORECASE):
            raw = match.group(1).replace("\\\\", "\\")
            add(raw)
    return roots


def discover_managed_dir(
    *,
    managed_dir: pathlib.Path | str | None = None,
    oni_root: pathlib.Path | str | None = None,
) -> tuple[Optional[pathlib.Path], list[str]]:
    """Resolve the ONI Managed directory and explain discovery decisions."""

    diagnostics: list[str] = []
    explicit_managed = _path(managed_dir)
    if explicit_managed is not None:
        diagnostics.append("managed_dir_source=argument")
        return explicit_managed, diagnostics

    explicit_root = _path(oni_root)
    if explicit_root is not None:
        diagnostics.append("managed_dir_source=oni_root_argument")
        return explicit_root / "OxygenNotIncluded_Data" / "Managed", diagnostics

    for env_name in ("ONI_MANAGED_DIR", "ONI_MANAGED_PATH"):
        candidate = _path(os.environ.get(env_name))
        if candidate is not None:
            diagnostics.append(f"managed_dir_source={env_name.lower()}")
            return candidate, diagnostics
    for env_name in ("ONI_ROOT", "OXYGEN_NOT_INCLUDED_ROOT"):
        candidate = _path(os.environ.get(env_name))
        if candidate is not None:
            diagnostics.append(f"managed_dir_source={env_name.lower()}")
            return candidate / "OxygenNotIncluded_Data" / "Managed", diagnostics

    # Check each Steam library's standard app location.
    for steam_root in _candidate_steam_roots():
        candidate = steam_root / "steamapps" / "common" / "OxygenNotIncluded" / "OxygenNotIncluded_Data" / "Managed"
        if candidate.is_dir():
            diagnostics.append("managed_dir_source=steam_discovery")
            return candidate, diagnostics

    diagnostics.append("managed_dir_source=not_found")
    return None, diagnostics


def _parse_version_output(text: str, *, source: str) -> dict[str, Any]:
    """Parse decompiled ``KleiVersion`` text.

    ILSpy has emitted several formatting variants over time, so whitespace,
    optional type suffixes, and quoting are deliberately permissive.
    """

    change_match = re.search(
        r"\bChangeList\b\s*=\s*(\d{4,})\s*[uUlL]*\b", text, flags=re.IGNORECASE
    )
    branch_match = re.search(
        r"\bBuildBranch\b\s*=\s*['\"]([^'\"]*)['\"]", text, flags=re.IGNORECASE
    )
    patch_match = re.search(
        r"\bPatch\b\s*=\s*['\"]([^'\"]*)['\"]", text, flags=re.IGNORECASE
    )
    change_list = int(change_match.group(1)) if change_match else None
    branch = branch_match.group(1) if branch_match else None
    patch = patch_match.group(1) if patch_match else None
    build_id = f"cl-{change_list}" if change_list is not None else None
    confidence = "high" if change_list is not None else "unknown"
    return {
        "id": build_id,
        "change_list": change_list,
        "branch": branch,
        "patch": patch,
        "source": source,
        "confidence": confidence,
    }


def _binary_version_fallback(path: pathlib.Path) -> tuple[dict[str, Any], list[str]]:
    """Best-effort extraction when ILSpy is unavailable.

    .NET metadata stores field names and constants in separate heaps, therefore
    a plain byte search cannot safely associate an arbitrary number with
    ``ChangeList``.  We only accept strongly labelled ``cl-N`` forms.  If none
    are present, returning ``unknown`` is safer than guessing a build.
    """

    diagnostics: list[str] = []
    try:
        data = path.read_bytes()
    except OSError as exc:
        diagnostics.append(f"version_fallback_read_failed={type(exc).__name__}")
        return _parse_version_output("", source="unavailable"), diagnostics

    # Restrict decoding to printable-ish bytes to avoid enormous diagnostic
    # strings.  ONI's assembly is small enough for one pass (~14 MiB).
    text = data.decode("latin-1", errors="ignore")
    labelled = re.search(r"\bcl[-_ ](\d{4,})\b", text, flags=re.IGNORECASE)
    if labelled:
        result = {
            "id": f"cl-{int(labelled.group(1))}",
            "change_list": int(labelled.group(1)),
            "branch": None,
            "patch": None,
            "source": "binary_label_fallback",
            "confidence": "medium",
        }
        diagnostics.append("version_source=binary_label_fallback")
        return result, diagnostics

    diagnostics.append("klei_version_not_extractable_without_ilspy")
    return _parse_version_output("", source="unavailable"), diagnostics


def _find_ilspy(command: str | None) -> Optional[list[str]]:
    if command:
        # Permit a quoted executable plus optional launcher arguments while
        # still passing the assembly path as a separate subprocess argument.
        try:
            pieces = shlex.split(command, posix=False)
        except ValueError:
            pieces = [command]
        return pieces or None
    env_value = os.environ.get("ILSPY_CMD")
    if env_value:
        try:
            pieces = shlex.split(env_value, posix=False)
        except ValueError:
            pieces = [env_value]
        return pieces or None
    for name in ("ilspycmd", "ilspycmd.exe"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def extract_klei_version(
    assembly_path: pathlib.Path,
    *,
    ilspycmd: str | None = None,
    timeout_seconds: float = 20.0,
    allow_external_tool: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Extract ``KleiVersion`` from Assembly-CSharp without modifying it."""

    diagnostics: list[str] = []
    if not assembly_path.is_file():
        diagnostics.append("assembly_csharp_missing")
        return _parse_version_output("", source="unavailable"), diagnostics

    if allow_external_tool:
        launcher = _find_ilspy(ilspycmd)
        if launcher:
            command = [*launcher, "-t", "KleiVersion", str(assembly_path)]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(0.1, float(timeout_seconds)),
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                diagnostics.append(f"ilspy_failed={type(exc).__name__}")
            else:
                parsed = _parse_version_output(completed.stdout, source="ilspycmd")
                if parsed["change_list"] is not None:
                    diagnostics.append("version_source=ilspycmd")
                    return parsed, diagnostics
                diagnostics.append(
                    f"ilspy_no_klei_version_exit={completed.returncode}"
                )
                # Do not include tool output: it can contain paths or unrelated
                # source details and is not needed for compatibility decisions.
        else:
            diagnostics.append("ilspycmd_not_found")
    else:
        diagnostics.append("external_version_tool_disabled")

    fallback, fallback_diagnostics = _binary_version_fallback(assembly_path)
    diagnostics.extend(fallback_diagnostics)
    return fallback, diagnostics


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _property_map(root: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag) not in {"TargetFramework", "TargetFrameworks", "AssemblyName", "RootNamespace", "ONIManagedDir", "OutputPath", "ModVersion"}:
            continue
        if element.text and element.text.strip():
            values[_local_name(element.tag)] = element.text.strip()
    return values


def _split_frameworks(value: str | None) -> list[str]:
    if not value:
        return []
    return [piece.strip() for piece in re.split(r"[;,]", value) if piece.strip()]


def _read_text(path: pathlib.Path, *, limit: int = 2 * 1024 * 1024) -> str:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            return stream.read(limit)
    except OSError:
        return ""


def _regex_value(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _parse_yaml_scalar(text: str, key: str) -> Optional[str]:
    # The project metadata files intentionally use simple scalar YAML.  Avoid
    # adding a PyYAML dependency merely to read two fields.
    value = _regex_value(text, rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$")
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value or None


def _resolve_hint_path(
    hint_path: str | None,
    *,
    project_root: pathlib.Path,
    managed_dir: Optional[pathlib.Path],
) -> Optional[pathlib.Path]:
    if not hint_path:
        return None
    expanded = os.path.expandvars(hint_path.strip())
    # MSBuild properties may remain unresolved in a source project.  Resolve
    # the one property relevant to ONI references when possible.
    if "$(ONIManagedDir)" in expanded and managed_dir is not None:
        expanded = expanded.replace("$(ONIManagedDir)", str(managed_dir))
    expanded = expanded.replace("$(MSBuildProjectDirectory)", str(project_root))
    candidate = pathlib.Path(expanded)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.expanduser().resolve(strict=False)


def read_mod_target(
    *,
    project_root: pathlib.Path,
    project_path: pathlib.Path | str | None = None,
    managed_dir: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    """Read target framework, references, and mod metadata from the project."""

    root = _path(project_root) or PROJECT_ROOT
    selected = _path(project_path)
    if selected is not None and project_path is not None:
        # A relative --project is naturally relative to --project-root, not to
        # whichever directory happened to launch the detector.
        raw_project = os.fspath(project_path)
        if not pathlib.Path(raw_project).is_absolute():
            selected = (root / raw_project).expanduser().resolve(strict=False)
    if selected is None:
        candidates = sorted(root.glob("mod/*.csproj")) + sorted(root.glob("*.csproj"))
        selected = candidates[0] if candidates else None

    result: dict[str, Any] = {
        "project_path": str(selected) if selected else None,
        "project_present": bool(selected and selected.is_file()),
        "assembly_name": None,
        "root_namespace": None,
        "target_framework": None,
        "target_frameworks": [],
        "target_framework_status": "unknown",
        "configured_managed_dir": None,
        "managed_dir_match": None,
        "mod_version": None,
        "static_id": None,
        "api_version": None,
        "references": [],
        "artifacts": [],
        "diagnostics": [],
    }

    if selected is None or not selected.is_file():
        result["diagnostics"].append("mod_project_not_found")
    else:
        try:
            tree = ET.parse(selected)
            xml_root = tree.getroot()
        except (ET.ParseError, OSError) as exc:
            result["diagnostics"].append(f"mod_project_parse_failed={type(exc).__name__}")
            xml_root = None
        if xml_root is not None:
            props = _property_map(xml_root)
            frameworks = _split_frameworks(props.get("TargetFrameworks"))
            frameworks.extend(_split_frameworks(props.get("TargetFramework")))
            # Preserve order while de-duplicating.
            result["target_frameworks"] = list(dict.fromkeys(frameworks))
            result["target_framework"] = (
                result["target_frameworks"][0] if result["target_frameworks"] else None
            )
            result["assembly_name"] = props.get("AssemblyName")
            result["root_namespace"] = props.get("RootNamespace")
            result["configured_managed_dir"] = props.get("ONIManagedDir")
            configured = props.get("ONIManagedDir")
            if configured and managed_dir is not None:
                resolved_configured = _resolve_hint_path(
                    configured,
                    project_root=selected.parent,
                    managed_dir=managed_dir,
                )
                result["managed_dir_match"] = _norm_path_text(str(resolved_configured)) == _norm_path_text(str(managed_dir))

            for item in xml_root.iter():
                if _local_name(item.tag) != "Reference":
                    continue
                include = (item.attrib.get("Include") or "").strip()
                hint_element = next(
                    (child for child in item if _local_name(child.tag) == "HintPath"),
                    None,
                )
                hint = hint_element.text.strip() if hint_element is not None and hint_element.text else None
                resolved = _resolve_hint_path(
                    hint,
                    project_root=selected.parent,
                    managed_dir=managed_dir,
                )
                # If no HintPath exists, use the managed directory for known
                # ONI assembly names; this mirrors MSBuild's intended lookup.
                if resolved is None and managed_dir is not None and include:
                    name = include.split(",", 1)[0].strip()
                    if not name.lower().endswith(".dll"):
                        name += ".dll"
                    resolved = managed_dir / name
                ref_name = include.split(",", 1)[0].strip() if include else None
                result["references"].append(
                    {
                        "include": include or None,
                        "name": ref_name,
                        "hint_path": hint,
                        "resolved_path": str(resolved) if resolved else None,
                        "present": bool(resolved and resolved.is_file()),
                        "sha256": sha256_file(resolved) if resolved and resolved.is_file() else None,
                    }
                )

    framework_set = set(result["target_frameworks"])
    if framework_set & SUPPORTED_TARGET_FRAMEWORKS:
        result["target_framework_status"] = "supported"
    elif framework_set:
        result["target_framework_status"] = "review"

    source_candidates = [
        root / "mod" / "Source" / "Mod.cs",
        root / "mod" / "Mod.cs",
        root / "Source" / "Mod.cs",
    ]
    source_text = next((_read_text(path) for path in source_candidates if path.is_file()), "")
    result["mod_version"] = _regex_value(
        source_text, r"\bModVersion\s*=\s*[\"']([^\"']+)[\"']"
    )
    if result["mod_version"] is None and selected is not None:
        result["mod_version"] = _regex_value(
            _read_text(selected), r"\bModVersion\s*[=:]\s*[\"']([^\"']+)[\"']"
        )

    info_candidates = [root / "mod" / "mod_info.yaml", root / "mod_info.yaml"]
    info_text = next((_read_text(path) for path in info_candidates if path.is_file()), "")
    api_value = _parse_yaml_scalar(info_text, "APIVersion")
    if api_value is not None:
        try:
            result["api_version"] = int(api_value)
        except ValueError:
            result["api_version"] = api_value

    descriptor_candidates = [root / "mod" / "mod.yaml", root / "mod.yaml"]
    descriptor_text = next(
        (_read_text(path) for path in descriptor_candidates if path.is_file()), ""
    )
    result["static_id"] = _parse_yaml_scalar(descriptor_text, "staticID")
    result["title"] = _parse_yaml_scalar(descriptor_text, "title")

    artifact_names: list[pathlib.Path] = []
    assembly_name = result.get("assembly_name") or "OniAiProbeMod"
    for candidate in (
        root / "mod" / "bin" / "Release" / f"{assembly_name}.dll",
        root / "mod" / "bin" / "Debug" / f"{assembly_name}.dll",
        root / "mod" / f"{assembly_name}.dll",
    ):
        if candidate not in artifact_names:
            artifact_names.append(candidate)
    result["artifacts"] = [_file_record(path) for path in artifact_names if path.is_file()]
    return result


def _check(
    checks: list[dict[str, Any]],
    check_id: str,
    ok: bool,
    *,
    severity: str = "error",
    detail: Any = None,
) -> None:
    checks.append(
        {
            "id": check_id,
            "ok": bool(ok),
            "severity": severity,
            "detail": detail,
        }
    )


def detect_runtime(
    *,
    managed_dir: pathlib.Path | str | None = None,
    oni_root: pathlib.Path | str | None = None,
    project_root: pathlib.Path | str | None = None,
    project_path: pathlib.Path | str | None = None,
    ilspycmd: str | None = None,
    expected_build: str | None = None,
    expected_assembly_sha256: str | None = None,
    allow_external_tool: bool = True,
) -> dict[str, Any]:
    """Return a JSON-serializable, read-only compatibility report."""

    resolved_managed, discovery_diagnostics = discover_managed_dir(
        managed_dir=managed_dir,
        oni_root=oni_root,
    )
    root = _path(project_root) or PROJECT_ROOT
    managed_for_report = resolved_managed or pathlib.Path("")
    assembly_path = managed_for_report / "Assembly-CSharp.dll" if resolved_managed else None

    if assembly_path is not None:
        assembly_record = _file_record(assembly_path)
        version, version_diagnostics = extract_klei_version(
            assembly_path,
            ilspycmd=ilspycmd,
            allow_external_tool=allow_external_tool,
        )
    else:
        assembly_record = {
            "path": None,
            "file_name": "Assembly-CSharp.dll",
            "present": False,
        }
        version = _parse_version_output("", source="unavailable")
        version_diagnostics = ["assembly_csharp_path_unresolved"]

    mod_target = read_mod_target(
        project_root=root,
        project_path=project_path,
        managed_dir=resolved_managed,
    )

    required_names: list[str] = []
    for reference in mod_target.get("references", []):
        name = str(reference.get("name") or "").strip()
        if name:
            if not name.lower().endswith(".dll"):
                name += ".dll"
            required_names.append(name)
    if not required_names:
        required_names = list(DEFAULT_REQUIRED_REFERENCES)
    required_names = list(dict.fromkeys(required_names))

    references: list[dict[str, Any]] = []
    if resolved_managed:
        for name in required_names:
            references.append(_file_record(resolved_managed / name))
    else:
        references = [
            {"path": None, "file_name": name, "present": False} for name in required_names
        ]

    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "managed_dir_present",
        bool(resolved_managed and resolved_managed.is_dir()),
        detail=str(resolved_managed) if resolved_managed else None,
    )
    _check(
        checks,
        "assembly_csharp_present",
        bool(assembly_record.get("present")),
        detail=assembly_record.get("path"),
    )
    _check(
        checks,
        "assembly_csharp_hash_available",
        bool(assembly_record.get("sha256")),
        severity="review",
        detail=assembly_record.get("sha256"),
    )
    _check(
        checks,
        "klei_build_detected",
        bool(version.get("id")),
        severity="review",
        detail=version,
    )
    _check(
        checks,
        "required_references_present",
        all(item.get("present") for item in references),
        detail=[item.get("file_name") for item in references if not item.get("present")],
    )
    _check(
        checks,
        "mod_project_present",
        bool(mod_target.get("project_present")),
        severity="review",
        detail=mod_target.get("project_path"),
    )
    _check(
        checks,
        "target_framework_declared",
        bool(mod_target.get("target_frameworks")),
        severity="review",
        detail=mod_target.get("target_frameworks"),
    )
    _check(
        checks,
        "target_framework_supported",
        mod_target.get("target_framework_status") == "supported",
        severity="review",
        detail=mod_target.get("target_framework_status"),
    )
    if mod_target.get("managed_dir_match") is not None:
        _check(
            checks,
            "project_managed_dir_matches_runtime",
            bool(mod_target.get("managed_dir_match")),
            severity="review",
            detail=mod_target.get("configured_managed_dir"),
        )

    expected_build_clean = (expected_build or "").strip()
    if expected_build_clean:
        if expected_build_clean.isdigit():
            expected_build_clean = f"cl-{expected_build_clean}"
        expected_build_clean = expected_build_clean.lower()
        actual_build = str(version.get("id") or "").lower()
        _check(
            checks,
            "expected_build_matches",
            bool(actual_build and actual_build == expected_build_clean),
            detail={"expected": expected_build_clean, "actual": actual_build or None},
        )

    expected_hash_clean = (expected_assembly_sha256 or "").strip().lower().removeprefix("sha256:")
    if expected_hash_clean:
        actual_hash = str(assembly_record.get("sha256") or "").lower()
        _check(
            checks,
            "expected_assembly_hash_matches",
            bool(actual_hash and actual_hash == expected_hash_clean),
            detail={"expected": expected_hash_clean, "actual": actual_hash or None},
        )

    hard_failures = [item for item in checks if not item["ok"] and item["severity"] == "error"]
    review_findings = [item for item in checks if not item["ok"] and item["severity"] == "review"]
    if hard_failures:
        status = "incompatible"
    elif review_findings:
        status = "needs_review"
    else:
        status = "compatible"

    assembly_hash = assembly_record.get("sha256")
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": _now_iso(),
        "read_only": True,
        "status": status,
        "ok": status == "compatible",
        # Stable aliases make this useful to shell scripts and the planner.
        "game_build": version.get("id"),
        "build_id": version.get("id"),
        "assembly_csharp_sha256": assembly_hash,
        "assembly_csharp_hash": f"sha256:{assembly_hash}" if assembly_hash else None,
        "mod_target_info": mod_target,
        "game": {
            "root": str(resolved_managed.parents[1]) if resolved_managed and len(resolved_managed.parents) >= 2 else None,
            "managed_dir": str(resolved_managed) if resolved_managed else None,
            "build": version,
            "assembly_csharp": assembly_record,
            "references": references,
        },
        "checks": checks,
        "diagnostics": [*discovery_diagnostics, *version_diagnostics],
        "policy": {
            "expected_build": expected_build_clean or None,
            "expected_assembly_sha256": expected_hash_clean or None,
            "hardcoded_build_allowlist": False,
        },
    }
    return report


def _json_dump(payload: Mapping[str, Any], *, compact: bool = False) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only ONI Managed/mod compatibility detector (no build allow-list)."
    )
    parser.add_argument("--oni-root", help="OxygenNotIncluded installation root")
    parser.add_argument("--managed-dir", help="OxygenNotIncluded_Data/Managed directory")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="automation project root")
    parser.add_argument("--project", dest="project_path", help="specific .csproj to inspect")
    parser.add_argument("--ilspycmd", help="optional ilspycmd executable/launcher")
    parser.add_argument("--expected-build", help="optional exact build (e.g. cl-744825 or 744825)")
    parser.add_argument("--expected-assembly-sha256", help="optional exact Assembly-CSharp SHA-256")
    parser.add_argument("--output", help="write JSON report outside ONI; stdout remains quiet")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    parser.add_argument("--strict", action="store_true", help="return 1 for needs_review")
    parser.add_argument(
        "--no-external-tool",
        action="store_true",
        help="do not invoke ilspycmd; use conservative binary fallback",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = detect_runtime(
            managed_dir=args.managed_dir,
            oni_root=args.oni_root,
            project_root=args.project_root,
            project_path=args.project_path,
            ilspycmd=args.ilspycmd,
            expected_build=args.expected_build,
            expected_assembly_sha256=args.expected_assembly_sha256,
            allow_external_tool=not args.no_external_tool,
        )
        rendered = _json_dump(report, compact=args.compact)
        if args.output:
            output_path = _path(args.output)
            if output_path is None:
                parser.error("--output must not be empty")
            # Report output is explicitly caller-selected and may not be in the
            # ONI installation.  Atomic replace avoids partial reports.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = output_path.with_name(output_path.name + ".tmp")
            temporary.write_text(rendered + "\n", encoding="utf-8")
            temporary.replace(output_path)
        else:
            print(rendered)
    except (OSError, ValueError) as exc:
        print(f"runtime compatibility detector error: {exc}", file=sys.stderr)
        return 2

    if report["status"] == "incompatible":
        return 1
    if args.strict and report["status"] == "needs_review":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())
