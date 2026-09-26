#!/usr/bin/env python3
"""Read-only preflight checks for the ONI AI Colony Agent runtime.

The doctor deliberately does not launch Oxygen Not Included, call an LLM, move
command files, or write any files.  It is intended to be safe to run before a
new runtime session is started.  Exit codes are intentionally small and
script-friendly:

* ``0``: all checks are healthy;
* ``1``: the setup is usable but degraded (for example, the game is not
  running yet, the Bridge snapshot is stale, or no optional LLM key is set);
* ``2``: a required identity/configuration check is blocked or malformed.

Use ``--json`` for machine-readable output.  Secrets are reduced to boolean
presence and a source label; key material is never copied into the report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.runtime_confirmation import (
    CONFIRMED_MUTATION_MODE,
    LEGACY_MUTATION_MODE,
    archived_confirmation_records,
    assess_command_confirmation,
    confirmation_summary,
)
DEFAULT_BRIDGE_DIR = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"
DEFAULT_PLAYER_LOG = pathlib.Path.home() / "AppData" / "LocalLow" / "Klei" / "Oxygen Not Included" / "Player.log"
STEAM_APP_ID = "457140"
MOD_ID = "oni-ai-colony-agent"
MOD_VERSION_PATTERN = re.compile(r'ModVersion\s*=\s*"([^"]+)"')
BUILD_PATTERN = re.compile(r"\bBuild:\s*(\S+)(?:\s+\(([^)]+)\))?", re.IGNORECASE)
PROBE_MARKER_PATTERN = re.compile(r"\[ONI AI Probe\]|ONI AI Probe:oni-ai-colony-agent", re.IGNORECASE)
KEY_NAMES = {"OPENAI_API_KEY", "ONI_AI_API_KEY", "API_KEY", "KEY"}
KEY_LINE_PATTERN = re.compile(r"^\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*[:=]\s*(.*?)\s*$")
MAX_SECRET_FILE_BYTES = 2 * 1024 * 1024
MAX_LOG_TAIL_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
CRITICAL_FIELDS = (
    "game.paused",
    "cycle",
    "duplicants.total",
    "resources.food_kcal",
    "research.current",
)
QUEUE_NAMES = (
    "pending",
    "approving",
    "approved",
    "executing",
    "confirming",
    "done",
    "failed",
    "rejected",
    "quarantine",
)
EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_BLOCKED = 2


def _confirmation_records(
    bridge_dir: pathlib.Path,
    command: Dict[str, Any],
) -> List[Dict[str, Any]]:
    return archived_confirmation_records(bridge_dir, command)


def confirmation_audit(
    bridge_dir: pathlib.Path,
    issues: List[Dict[str, str]],
) -> Dict[str, Any]:
    confirmed = 0
    legacy = 0
    invalid = 0
    samples: List[Dict[str, Any]] = []
    done = bridge_dir / "commands" / "done"
    if not done.is_dir():
        return {
            "confirmed_mutations": 0,
            "legacy_read_only_mutations": 0,
            "invalid_confirmed_mutations": 0,
            "samples": [],
        }
    for path in sorted(done.glob("*.json")):
        command, error = read_json(path)
        if error or not isinstance(command, dict):
            continue
        assessment = assess_command_confirmation(
            bridge_dir,
            command,
            _confirmation_records(bridge_dir, command),
            directory="done",
        )
        if assessment.get("mode") == CONFIRMED_MUTATION_MODE and assessment.get("accepted") is True:
            confirmed += 1
        elif assessment.get("mode") == LEGACY_MUTATION_MODE:
            legacy += 1
            add_issue(
                issues,
                "warning",
                "legacy_mutating_done_read_only",
                "A mutating done/status=verified command is legacy read-only evidence and cannot satisfy live acceptance.",
            )
        elif assessment.get("mutating") is True and command.get("status") == "confirmed":
            invalid += 1
            add_issue(
                issues,
                "error",
                "invalid_confirmed_mutation",
                "A done/status=confirmed command lacks a valid embedded/published attestation, ordered ACKs, or later trusted witness.",
            )
        if len(samples) < 20 and (
            assessment.get("mode") in {CONFIRMED_MUTATION_MODE, LEGACY_MUTATION_MODE}
            or (assessment.get("mutating") is True and command.get("status") == "confirmed")
        ):
            summary = confirmation_summary(assessment)
            summary["command_id"] = command.get("id")
            samples.append(summary)
    return {
        "confirmed_mutations": confirmed,
        "legacy_read_only_mutations": legacy,
        "invalid_confirmed_mutations": invalid,
        "samples": samples,
    }


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_path(value: Optional[str | pathlib.Path]) -> Optional[pathlib.Path]:
    if value is None:
        return None
    text = os.path.expandvars(os.path.expanduser(str(value))).strip()
    if not text:
        return None
    return pathlib.Path(text).resolve(strict=False)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_json(path: pathlib.Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        if not path.is_file():
            return None, "missing"
        if path.stat().st_size > MAX_JSON_BYTES:
            return None, "file_too_large"
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, UnicodeError) as exc:
        return None, type(exc).__name__
    except json.JSONDecodeError:
        return None, "invalid_json"


def read_tail(path: pathlib.Path, max_bytes: int = MAX_LOG_TAIL_BYTES) -> str:
    """Read only the tail of a potentially very large Player.log."""

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_player_log(path: pathlib.Path) -> Dict[str, Any]:
    text = read_tail(path)
    builds = list(BUILD_PATTERN.finditer(text))
    build = builds[-1] if builds else None
    marker_lines = [line for line in text.splitlines() if PROBE_MARKER_PATTERN.search(line)]
    loaded = any(
        "Loading mod content DLL [ONI AI Probe" in line
        or "Loading MOD dll: OniAiProbeMod.dll" in line
        or "[ONI AI Probe] Loading" in line
        for line in marker_lines
    )
    # Older Probe builds publish the structured ``probe_patch_registered``
    # event, while the current observer-default build logs one success line
    # per Harmony target.  Require both target lines so one partial patch is
    # never presented as a complete registration.  This remains historical
    # log evidence; it does not grant live admission.
    patch_registered = (
        "probe_patch_registered" in text
        or (
            "[ONI AI Probe] Patched Game.OnSpawn" in text
            and "[ONI AI Probe] Patched Game.OnCleanUp" in text
        )
    )
    probe_errors = len(re.findall(r"\[ONI AI Probe\].*(?:ERROR|Exception|probe_error)", text, re.IGNORECASE))
    return {
        "present": path.is_file(),
        "path": str(path),
        "build_label": build.group(1) if build else None,
        "game_branch": build.group(2).strip() if build and build.group(2) else None,
        "probe_marker_seen": bool(marker_lines),
        "probe_loaded": loaded,
        "probe_patch_registered": patch_registered,
        "probe_error_count": probe_errors,
        "tail_bytes_scanned": len(text.encode("utf-8", errors="replace")),
    }


def parse_steam_buildid(oni_dir: Optional[pathlib.Path]) -> Dict[str, Any]:
    if oni_dir is None:
        return {"present": False, "buildid": None, "path": None}
    candidates = [oni_dir.parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"]
    # A user may pass a symlink/junction-like path.  Check nearby steamapps
    # directories without recursively searching the disk.
    for parent in oni_dir.parents:
        candidates.append(parent / f"appmanifest_{STEAM_APP_ID}.acf")
    seen: set[pathlib.Path] = set()
    for manifest in candidates:
        if manifest in seen:
            continue
        seen.add(manifest)
        if not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r'"buildid"\s*"([^"]+)"', text, re.IGNORECASE)
        app_match = re.search(r'"appid"\s*"([^"]+)"', text, re.IGNORECASE)
        if match:
            return {
                "present": True,
                "buildid": match.group(1),
                "appid": app_match.group(1) if app_match else None,
                "path": str(manifest),
            }
    return {"present": False, "buildid": None, "path": None}


def _steam_library_roots() -> Iterable[pathlib.Path]:
    env_roots = [os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")]
    for root in env_roots:
        if root:
            yield pathlib.Path(root) / "Steam"
    yield pathlib.Path("C:/Program Files (x86)/Steam")
    yield pathlib.Path("C:/Program Files/Steam")


def _libraryfolders_paths(steam_root: pathlib.Path) -> Iterable[pathlib.Path]:
    config = steam_root / "steamapps" / "libraryfolders.vdf"
    if not config.is_file():
        return
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    # VDF paths are quoted and may contain escaped backslashes.  We only use
    # values attached to a "path" key; no shell evaluation is involved.
    for raw in re.findall(r'"path"\s*"((?:\\.|[^"\\])*)"', text, re.IGNORECASE):
        value = raw.replace("\\\\", "\\").replace('\\"', '"')
        yield pathlib.Path(value)


def resolve_oni_dir(raw: Optional[str]) -> Tuple[Optional[pathlib.Path], bool, List[str]]:
    """Return (path, explicit, candidates_checked)."""

    candidates: List[pathlib.Path] = []
    if raw:
        explicit_path = normalize_path(raw)
        if explicit_path is None:
            return None, True, []
        if explicit_path.is_file() and explicit_path.name.lower() == "oxygennotincluded.exe":
            explicit_path = explicit_path.parent
        return explicit_path, True, [str(explicit_path)]

    env_path = normalize_path(os.environ.get("ONI_INSTALL_DIR"))
    if env_path:
        candidates.append(env_path.parent if env_path.is_file() else env_path)
    for steam_root in _steam_library_roots():
        candidates.append(steam_root / "steamapps" / "common" / "OxygenNotIncluded")
        for library in _libraryfolders_paths(steam_root) or ():
            candidates.append(library / "steamapps" / "common" / "OxygenNotIncluded")
    unique: List[pathlib.Path] = []
    for candidate in candidates:
        candidate = candidate.resolve(strict=False)
        if candidate not in unique:
            unique.append(candidate)
    for candidate in unique:
        if (candidate / "OxygenNotIncluded.exe").is_file() or (candidate / "OxygenNotIncluded_Data").is_dir():
            return candidate, False, [str(item) for item in unique]
    return (unique[0] if unique else None), False, [str(item) for item in unique]


def resolve_managed_dir(oni_dir: Optional[pathlib.Path]) -> Optional[pathlib.Path]:
    if oni_dir is None:
        return None
    direct = oni_dir / "OxygenNotIncluded_Data" / "Managed"
    if direct.is_dir():
        return direct
    if oni_dir.name.lower() == "managed" and oni_dir.is_dir():
        return oni_dir
    return direct


def source_mod_version() -> Optional[str]:
    source = PROJECT_ROOT / "mod" / "Source" / "Mod.cs"
    try:
        match = MOD_VERSION_PATTERN.search(source.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group(1) if match else None


def select_build_artifact() -> Optional[pathlib.Path]:
    candidates = (
        PROJECT_ROOT / "mod" / "OniAiProbeMod.dll",
        PROJECT_ROOT / "mod" / "bin" / "Release" / "OniAiProbeMod.dll",
        PROJECT_ROOT / "mod" / "bin" / "Debug" / "OniAiProbeMod.dll",
    )
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    # The project-root copy is the artifact consumed by install-dev-mod.ps1;
    # prefer it even if an older bin output has a newer timestamp.
    return existing[0]


def file_descriptor(path: Optional[pathlib.Path], include_hash: bool = True) -> Dict[str, Any]:
    if path is None:
        return {"present": False, "path": None}
    result: Dict[str, Any] = {"present": path.is_file(), "path": str(path)}
    if not path.is_file():
        return result
    try:
        stat = path.stat()
        result["bytes"] = stat.st_size
        result["modified_at"] = dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
        if include_hash:
            result["sha256"] = sha256_file(path)
    except OSError as exc:
        result["error"] = type(exc).__name__
    return result


def find_process(process_name: str) -> bool:
    """Best-effort process check with no third-party dependency."""

    try:
        import psutil  # type: ignore

        wanted = process_name.lower()
        for process in psutil.process_iter(["name", "exe"]):
            try:
                name = str(process.info.get("name") or "").lower()
                exe = str(process.info.get("exe") or "").lower()
                if name == wanted or pathlib.Path(exe).name == wanted:
                    return True
            except (OSError, PermissionError):
                continue
        return False
    except ImportError:
        pass

    if os.name == "nt" and shutil.which("tasklist"):
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
                capture_output=True,
                # Windows may emit the localized "no tasks" line in the
                # system code page (often cp936), so decode explicitly and
                # never let locale output abort a read-only health check.
                text=False,
                timeout=3,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = (result.stdout or b"").decode(errors="replace").lower()
            return process_name.lower() in output and "no tasks" not in output
        except (OSError, subprocess.SubprocessError):
            return False

    proc_root = pathlib.Path("/proc")
    if proc_root.is_dir():
        wanted = process_name.lower()
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip().lower()
            except OSError:
                continue
            if name == wanted:
                return True
    return False


def sanitize_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        if not parsed.scheme or not parsed.netloc:
            return text[:200]
        # Drop credentials, query and fragment.  A custom endpoint path is
        # useful for diagnostics, while query strings can contain credentials.
        host = parsed.hostname or ""
        netloc = host
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))[:200]
    except ValueError:
        return "[invalid_endpoint]"


def secret_file_has_key(path: pathlib.Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SECRET_FILE_BYTES:
            return False
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            for key in KEY_NAMES:
                candidate = value.get(key)
                if candidate is not None and str(candidate).strip():
                    return True
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = KEY_LINE_PATTERN.match(line)
        # Mirrors agent.llm_planner.read_secret_file: only explicit NAME=value
        # lines count; bare token-looking lines are never used as keys.
        if match and match.group(1).upper() in KEY_NAMES and match.group(2).strip().strip("'\"`"):
            return True
    return False


def detect_llm_key(secrets_dir: Optional[pathlib.Path]) -> Dict[str, Any]:
    env_sources = [name for name in ("OPENAI_API_KEY", "ONI_AI_API_KEY") if os.environ.get(name, "").strip()]
    secret_dir_configured = secrets_dir is not None
    secret_dir_exists = bool(secrets_dir and secrets_dir.is_dir())
    secret_files_scanned = 0
    secret_key_present = False
    if secret_dir_exists and secrets_dir is not None:
        try:
            for path in sorted(secrets_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in {".md", ".txt", ".env", ".json"}:
                    secret_files_scanned += 1
                    if not secret_key_present and secret_file_has_key(path):
                        secret_key_present = True
        except OSError:
            secret_dir_exists = False
    source: Optional[str]
    if env_sources:
        source = "environment"
    elif secret_key_present:
        source = "secrets_dir"
    else:
        source = None
    return {
        "api_key_present": bool(env_sources or secret_key_present),
        "source": source,
        "environment_sources": env_sources,
        "secrets_dir_configured": secret_dir_configured,
        "secrets_dir_exists": secret_dir_exists,
        "secret_files_scanned": secret_files_scanned,
    }


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def builds_compatible(first: Any, second: Any) -> bool:
    """Compare ONI build labels across Probe/Unity naming conventions.

    The Probe emits ``cl-740622`` while Player.log commonly prints
    ``U59-740622-SCR``.  A shared changelist number is sufficient evidence of
    the same game build; requiring literal equality would flag every healthy
    current session.  Unrecognized labels remain compatible only when their
    normalized text is identical.
    """

    left = str(first or "").strip().lower()
    right = str(second or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True

    def changelist(label: str) -> Optional[int]:
        for pattern in (
            r"cl[-_ ]*(\d{4,})",
            r"u\d+[-_](\d{4,})(?:[-_][a-z0-9]+)*",
            r"(\d{4,})",
        ):
            match = re.fullmatch(pattern, label, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    left_changelist = changelist(left)
    right_changelist = changelist(right)
    if left_changelist is None or right_changelist is None:
        return False
    return left_changelist == right_changelist


def age_seconds(value: Any) -> Optional[float]:
    timestamp = parse_timestamp(value)
    if timestamp is None:
        return None
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds())


def queue_counts(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    commands_root = bridge_dir / "commands"
    counts: Dict[str, int] = {}
    malformed = 0
    temporary = 0
    for name in QUEUE_NAMES:
        directory = commands_root / name
        count = 0
        if directory.is_dir():
            try:
                for path in directory.iterdir():
                    if path.name.endswith(".tmp"):
                        temporary += 1
                    if path.is_file() and path.suffix.lower() == ".json":
                        count += 1
                        payload, error = read_json(path)
                        if error or not isinstance(payload, dict):
                            malformed += 1
            except OSError:
                pass
        counts[name] = count
    counts["malformed_json"] = malformed
    counts["temporary_files"] = temporary
    return counts


def quality_summary(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    quality = state.get("quality") if isinstance(state, dict) else None
    if not isinstance(quality, dict):
        return {"fields": 0, "observed": 0, "estimated": 0, "unsupported": 0, "error": 0, "stale": 0, "unsupported_fields": []}
    counts = {"observed": 0, "estimated": 0, "unsupported": 0, "error": 0, "stale": 0}
    unsupported_fields: List[str] = []
    for path, entry in quality.items():
        status = str(entry.get("status") if isinstance(entry, dict) else "unsupported")
        if status not in counts:
            status = "error"
        counts[status] += 1
        if status in {"unsupported", "error", "stale"} and len(unsupported_fields) < 32:
            unsupported_fields.append(str(path))
    return {"fields": len(quality), **counts, "unsupported_fields": unsupported_fields}


def add_issue(issues: List[Dict[str, str]], severity: str, code: str, message: str) -> None:
    issues.append({"severity": severity, "code": code, "message": message})


def check_install(
    oni_dir: Optional[pathlib.Path],
    explicit: bool,
    candidates: Sequence[str],
    issues: List[Dict[str, str]],
) -> Dict[str, Any]:
    if oni_dir is None:
        add_issue(issues, "error", "oni_install_not_found", "Could not resolve an ONI installation directory.")
        return {"status": "error", "present": False, "explicit": explicit, "candidates": list(candidates)}
    executable = oni_dir / "OxygenNotIncluded.exe"
    data_dir = oni_dir / "OxygenNotIncluded_Data"
    present = executable.is_file() and data_dir.is_dir()
    details = {
        "status": "ok" if present else "error",
        "present": present,
        "explicit": explicit,
        "path": str(oni_dir),
        "executable": file_descriptor(executable),
        "data_dir_present": data_dir.is_dir(),
        "candidates": list(candidates)[:16],
    }
    if not present:
        add_issue(issues, "error", "oni_install_incomplete", "ONI executable or data directory is missing.")
    return details


def check_game_identity(
    oni_dir: Optional[pathlib.Path],
    player_log: pathlib.Path,
    bridge_state: Optional[Dict[str, Any]],
    manifest: Optional[Dict[str, Any]],
    issues: List[Dict[str, str]],
) -> Dict[str, Any]:
    managed = resolve_managed_dir(oni_dir)
    assembly = managed / "Assembly-CSharp.dll" if managed else None
    assembly_info = file_descriptor(assembly)
    build_info = parse_player_log(player_log)
    steam_info = parse_steam_buildid(oni_dir)
    expected_hash: Optional[str] = None
    manifest_path: Optional[str] = None
    if isinstance(manifest, dict):
        manifest_path = str(manifest.get("_path") or "") or None
        references = manifest.get("references")
        if isinstance(references, list):
            for item in references:
                if isinstance(item, dict) and str(item.get("file_name")) == "Assembly-CSharp.dll":
                    value = item.get("sha256")
                    if value:
                        expected_hash = str(value).lower()
                    break
    observed_hash = str(assembly_info.get("sha256") or "").lower() or None
    status = "ok"
    if not assembly_info.get("present"):
        status = "error"
        add_issue(issues, "error", "assembly_missing", "Assembly-CSharp.dll is missing from the ONI Managed directory.")
    elif expected_hash and observed_hash != (
        expected_hash if expected_hash.startswith("sha256:") else "sha256:" + expected_hash
    ):
        status = "error"
        add_issue(
            issues,
            "error",
            "assembly_hash_mismatch",
            "Assembly-CSharp.dll differs from the recorded build manifest; rebuild the Probe against this game build.",
        )
    if not steam_info.get("present"):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "steam_manifest_missing", "Steam appmanifest/buildid was not found beside the ONI installation.")
    if not build_info.get("build_label"):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "player_log_build_missing", "Player.log has no recognizable ONI Build line in its tail.")
    if build_info.get("probe_error_count", 0):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "probe_errors_in_log", "Player.log contains ONI AI Probe error markers.")
    if isinstance(bridge_state, dict):
        state_build = bridge_state.get("game_build")
        log_build = build_info.get("build_label")
        if state_build and log_build and not builds_compatible(state_build, log_build):
            if status == "ok":
                status = "warn"
            add_issue(issues, "warning", "runtime_build_differs", "Bridge state and Player.log report different game builds.")
    return {
        "status": status,
        "managed_dir": str(managed) if managed else None,
        "assembly": assembly_info,
        "recorded_manifest": manifest_path,
        "recorded_assembly_sha256": expected_hash,
        "steam": steam_info,
        "player_log": build_info,
    }


def check_mod(
    bridge_dir: pathlib.Path,
    oni_dir: Optional[pathlib.Path],
    player_log_info: Dict[str, Any],
    latest_state: Optional[Dict[str, Any]],
    build_manifest: Optional[Dict[str, Any]],
    issues: List[Dict[str, str]],
) -> Dict[str, Any]:
    source = select_build_artifact()
    source_info = file_descriptor(source)
    source_hash = str(source_info.get("sha256") or "").lower() or None
    # Dev mods live in the user's ONI data directory, not beside the Steam
    # installation.  Derive it from the Bridge root (…/OxygenNotIncluded/
    # ai-bridge) so custom Steam library locations work too.
    data_root = bridge_dir.parent
    dev_dir = data_root / "mods" / "Dev" / MOD_ID
    installed = dev_dir / "OniAiProbeMod.dll" if dev_dir else None
    installed_info = file_descriptor(installed)
    installed_hash = str(installed_info.get("sha256") or "").lower() or None
    version = source_mod_version()
    status = "ok"
    if source is None:
        status = "error"
        add_issue(issues, "error", "probe_artifact_missing", "No built OniAiProbeMod.dll exists in the project.")
    if installed is None or not installed_info.get("present"):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "probe_not_installed", "The ONI Dev mod folder has no installed Probe DLL.")
    elif source_hash and installed_hash and source_hash != installed_hash:
        status = "error"
        add_issue(issues, "error", "probe_hash_mismatch", "Installed Probe DLL does not match the project build artifact.")
    if not player_log_info.get("probe_loaded"):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "probe_load_not_observed", "Player.log tail contains no Probe load marker.")
    state_version = latest_state.get("mod_version") if isinstance(latest_state, dict) else None
    if version and state_version and str(version) != str(state_version):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "mod_version_differs", "Bridge state mod version differs from the source Mod.cs version.")
    manifest_artifact_hash: Optional[str] = None
    if isinstance(build_manifest, dict):
        artifacts = build_manifest.get("artifacts")
        if isinstance(artifacts, list):
            for item in artifacts:
                if isinstance(item, dict) and item.get("label") in {"build_output", "project_root_copy"}:
                    value = item.get("sha256")
                    if value:
                        manifest_artifact_hash = str(value).lower()
                    break
    if manifest_artifact_hash and source_hash and manifest_artifact_hash.replace("sha256:", "") != source_hash.replace("sha256:", ""):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "artifact_manifest_stale", "The optional local build manifest predates the current Probe artifact.")
    return {
        "status": status,
        "source_mod_version": version,
        "source_artifact": source_info,
        "dev_install": installed_info,
        "dev_mod_dir": str(dev_dir) if dev_dir else None,
        "loaded_marker_observed": bool(player_log_info.get("probe_loaded")),
        "patch_marker_observed": bool(player_log_info.get("probe_patch_registered")),
    }


def check_bridge(
    bridge_dir: pathlib.Path,
    latest_state: Optional[Dict[str, Any]],
    health: Optional[Dict[str, Any]],
    action_policy: Optional[Dict[str, Any]],
    validation_policy: Optional[Dict[str, Any]],
    llm_health: Optional[Dict[str, Any]],
    parse_errors: Dict[str, Optional[str]],
    issues: List[Dict[str, str]],
) -> Dict[str, Any]:
    status = "ok"
    if not bridge_dir.is_dir():
        add_issue(issues, "error", "bridge_dir_missing", "Bridge directory does not exist.")
        return {"status": "error", "present": False, "path": str(bridge_dir)}
    for name, error in parse_errors.items():
        if error == "missing":
            if name in {"health.json", "latest_state.json"}:
                if status == "ok":
                    status = "warn"
                add_issue(issues, "warning", f"{name[:-5]}_missing", f"Bridge file {name} is not present yet.")
        elif error:
            status = "error"
            add_issue(issues, "error", f"{name[:-5]}_invalid", f"Bridge file {name} could not be read ({error}).")

    health_status = str(health.get("status") if isinstance(health, dict) else "missing")
    health_age = age_seconds(health.get("updated_at")) if isinstance(health, dict) else None
    if health_status not in {"ok", "ready", "waiting_current_session_snapshot", "no_state"}:
        if health_status in {"stale", "starting", "idle", "disabled"}:
            if status == "ok":
                status = "warn"
            add_issue(issues, "warning", "bridge_not_fresh", f"Bridge health status is {health_status}.")
        elif health_status != "missing":
            status = "error"
            add_issue(issues, "error", "bridge_health_error", f"Bridge health status is {health_status}.")
    if health_age is not None and health_age > 120:
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "bridge_health_old", "Bridge health.json has not been updated recently.")
    if isinstance(latest_state, dict):
        if latest_state.get("schema") != "oni.state.v2":
            status = "error"
            add_issue(issues, "error", "state_schema_invalid", "latest_state.json is not an oni.state.v2 snapshot.")
        if latest_state.get("bridge", {}).get("stale") is True:
            if status == "ok":
                status = "warn"
            add_issue(issues, "warning", "latest_state_stale", "The latest Bridge snapshot is marked stale.")
        if isinstance(health, dict) and health.get("current_session_id") and latest_state.get("session_id"):
            if str(health.get("current_session_id")) != str(latest_state.get("session_id")):
                status = "error"
                add_issue(issues, "error", "session_identity_mismatch", "Bridge health and latest state belong to different sessions.")
    counts = queue_counts(bridge_dir)
    if counts.get("malformed_json", 0):
        status = "error"
        add_issue(issues, "error", "malformed_command_json", "At least one command queue entry is malformed JSON.")
    if counts.get("temporary_files", 0):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "command_temp_files", "Temporary command files remain in the queue.")
    if counts.get("executing", 0):
        status = "error"
        add_issue(
            issues,
            "error",
            "executing_commands_present",
            "Durable executing claims are present; treat their outcomes as unknown and do not replay them.",
        )
    elif counts.get("confirming", 0):
        status = "error"
        add_issue(
            issues,
            "error",
            "confirming_commands_present",
            "Mutating commands still await independent snapshot confirmation.",
        )
    elif counts.get("approving", 0):
        status = "error"
        add_issue(
            issues,
            "error",
            "approving_commands_present",
            "Interrupted approval claims are present; quarantine them before a new run.",
        )
    elif counts.get("approved", 0):
        status = "error"
        add_issue(issues, "error", "approved_commands_present", "Approved commands are present; review/quarantine them before a new run.")
    elif counts.get("pending", 0):
        if status == "ok":
            status = "warn"
        add_issue(issues, "warning", "pending_commands_present", "Pending commands require explicit human review.")

    confirmations = confirmation_audit(bridge_dir, issues)
    if confirmations["invalid_confirmed_mutations"]:
        status = "error"
    elif confirmations["legacy_read_only_mutations"] and status == "ok":
        status = "warn"

    action_summary: Dict[str, Any]
    if isinstance(action_policy, dict):
        enabled = bool(action_policy.get("actions_enabled", False))
        action_summary = {
            "schema": action_policy.get("schema"),
            "actions_enabled": enabled,
            "suggestions_enabled": bool(action_policy.get("suggestions_enabled", False)),
            "allowed_actions_count": len(action_policy.get("allowed_actions", [])) if isinstance(action_policy.get("allowed_actions"), list) else None,
            "allowed_save_hash_count": len(action_policy.get("allowed_save_hashes", [])) if isinstance(action_policy.get("allowed_save_hashes"), list) else None,
        }
        if action_policy.get("schema") != "oni.action-policy.v1":
            status = "error"
            add_issue(issues, "error", "action_policy_schema_invalid", "Action policy schema is not oni.action-policy.v1.")
        if enabled:
            if status == "ok":
                status = "warn"
            add_issue(issues, "warning", "actions_enabled", "Action execution policy is enabled; keep it disabled outside a supervised checkpoint.")
    else:
        action_summary = {"present": False}

    validation_summary: Dict[str, Any]
    if isinstance(validation_policy, dict):
        controls_enabled = bool(validation_policy.get("controls_enabled", False))
        validation_summary = {
            "schema": validation_policy.get("schema"),
            "controls_enabled": controls_enabled,
            "test_run_configured": bool(validation_policy.get("test_run_id")),
            "allowed_operations_count": len(validation_policy.get("allowed_operations", [])) if isinstance(validation_policy.get("allowed_operations"), list) else None,
        }
        if validation_policy.get("schema") != "oni.validation-control-policy.v1":
            status = "error"
            add_issue(issues, "error", "validation_policy_schema_invalid", "Validation control policy schema is invalid.")
        if controls_enabled:
            if status == "ok":
                status = "warn"
            add_issue(issues, "warning", "validation_controls_enabled", "Validation controls are enabled; disable them before normal play.")
    else:
        validation_summary = {"present": False}

    llm_summary: Dict[str, Any] = {}
    if isinstance(llm_health, dict):
        llm_summary = {
            "schema": llm_health.get("schema"),
            "enabled": bool(llm_health.get("enabled", False)),
            "called": bool(llm_health.get("called", False)),
            "status": str(llm_health.get("status") or "unknown"),
            "api_key_present_reported": llm_health.get("api_key_present") if isinstance(llm_health.get("api_key_present"), bool) else None,
            "model": str(llm_health.get("model") or "")[:120] or None,
            "base_url": sanitize_url(llm_health.get("base_url")),
        }
    return {
        "status": status,
        "path": str(bridge_dir),
        "health": {
            "status": health_status,
            "updated_at": health.get("updated_at") if isinstance(health, dict) else None,
            "age_seconds": round(health_age, 3) if health_age is not None else None,
            "current_session_id": health.get("current_session_id") if isinstance(health, dict) else None,
            "latest_seq": health.get("latest_seq") if isinstance(health, dict) else None,
        },
        "latest_state": {
            "schema": latest_state.get("schema") if isinstance(latest_state, dict) else None,
            "session_id": latest_state.get("session_id") if isinstance(latest_state, dict) else None,
            "game_build": latest_state.get("game_build") if isinstance(latest_state, dict) else None,
            "mod_version": latest_state.get("mod_version") if isinstance(latest_state, dict) else None,
            "seq": latest_state.get("seq") if isinstance(latest_state, dict) else None,
            "stale": latest_state.get("bridge", {}).get("stale") if isinstance(latest_state, dict) else None,
            "quality": quality_summary(latest_state),
        },
        "queues": counts,
        "confirmations": confirmations,
        "action_policy": action_summary,
        "validation_policy": validation_summary,
        "llm_health": llm_summary,
    }


def load_optional_manifest() -> Optional[Dict[str, Any]]:
    path = PROJECT_ROOT / "docs" / "build-manifest.local.json"
    payload, error = read_json(path)
    if error or not isinstance(payload, dict):
        return None
    payload["_path"] = str(path)
    return payload


def run_doctor(
    *,
    bridge_dir: Optional[str | pathlib.Path] = None,
    oni_dir: Optional[str | pathlib.Path] = None,
    secrets_dir: Optional[str | pathlib.Path] = None,
    player_log: Optional[str | pathlib.Path] = None,
    process_probe: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    bridge_root = normalize_path(bridge_dir) or DEFAULT_BRIDGE_DIR.resolve(strict=False)
    explicit_oni = oni_dir is not None
    resolved_oni, _, candidates = resolve_oni_dir(str(oni_dir) if oni_dir is not None else None)
    if oni_dir is not None:
        resolved_oni = normalize_path(oni_dir)
        if resolved_oni and resolved_oni.is_file() and resolved_oni.name.lower() == "oxygennotincluded.exe":
            resolved_oni = resolved_oni.parent
    log_path = normalize_path(player_log) or DEFAULT_PLAYER_LOG.resolve(strict=False)
    secret_path = normalize_path(secrets_dir)
    issues: List[Dict[str, str]] = []
    parse_errors: Dict[str, Optional[str]] = {}
    payloads: Dict[str, Optional[Any]] = {}
    for name in ("health.json", "latest_state.json", "action-policy.json", "validation-control-policy.json", "llm-health.json"):
        payload, error = read_json(bridge_root / name)
        payloads[name] = payload
        parse_errors[name] = error

    health = payloads.get("health.json") if isinstance(payloads.get("health.json"), dict) else None
    latest = payloads.get("latest_state.json") if isinstance(payloads.get("latest_state.json"), dict) else None
    action_policy = payloads.get("action-policy.json") if isinstance(payloads.get("action-policy.json"), dict) else None
    validation_policy = payloads.get("validation-control-policy.json") if isinstance(payloads.get("validation-control-policy.json"), dict) else None
    llm_health = payloads.get("llm-health.json") if isinstance(payloads.get("llm-health.json"), dict) else None
    manifest = load_optional_manifest()

    checks: Dict[str, Any] = {}
    checks["oni_install"] = check_install(resolved_oni, explicit_oni, candidates, issues)
    checks["game_identity"] = check_game_identity(resolved_oni, log_path, latest, manifest, issues)
    process_checker = process_probe or find_process
    game_running = bool(process_checker("OxygenNotIncluded.exe"))
    checks["process"] = {
        "status": "ok" if game_running else "warn",
        "oxygen_not_included_running": game_running,
    }
    if not game_running:
        add_issue(issues, "warning", "game_not_running", "OxygenNotIncluded.exe is not running; runtime load evidence may be historical.")
    checks["probe"] = check_mod(bridge_root, resolved_oni, checks["game_identity"].get("player_log", {}), latest, manifest, issues)
    checks["bridge"] = check_bridge(
        bridge_root,
        latest,
        health,
        action_policy,
        validation_policy,
        llm_health,
        parse_errors,
        issues,
    )
    llm_config = detect_llm_key(secret_path)
    llm_status = "ok" if llm_config["api_key_present"] else "warn"
    if not llm_config["api_key_present"]:
        add_issue(issues, "warning", "llm_key_absent", "No LLM API key was detected; Bridge will remain rule-only.")
    if secret_path is not None and not secret_path.is_dir():
        llm_status = "warn"
        add_issue(issues, "warning", "secrets_dir_missing", "The requested secrets directory does not exist.")
    if isinstance(llm_health, dict):
        reported = llm_health.get("api_key_present")
        if reported is True and not llm_config["api_key_present"]:
            llm_status = "warn"
            add_issue(issues, "warning", "llm_health_key_unavailable", "llm-health.json reports a key, but no key is available to this process.")
        if llm_config["api_key_present"] and llm_health.get("status") in {"disabled", "missing_api_key", None}:
            llm_status = "warn"
            add_issue(issues, "warning", "llm_not_verified", "A key is configured but no successful LLM request is recorded.")
    checks["llm"] = {"status": llm_status, **llm_config}

    errors = [item for item in issues if item.get("severity") == "error"]
    warnings = [item for item in issues if item.get("severity") == "warning"]
    exit_code = EXIT_BLOCKED if errors else EXIT_DEGRADED if warnings else EXIT_OK
    report = {
        "schema": "oni.doctor.v1",
        "generated_at": now_iso(),
        "read_only": True,
        "status": "blocked" if errors else "degraded" if warnings else "healthy",
        "exit_code": exit_code,
        "bridge_dir": str(bridge_root),
        "oni_dir": str(resolved_oni) if resolved_oni else None,
        "player_log": str(log_path),
        "checks": checks,
        "issues": issues,
        "issue_counts": {"errors": len(errors), "warnings": len(warnings)},
    }
    return report


def human_summary(report: Dict[str, Any]) -> str:
    lines = [
        f"ONI AI doctor: {str(report.get('status', 'unknown')).upper()} (exit {report.get('exit_code')})",
        "Read-only: yes; no game, save, command, or network writes performed.",
    ]
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    for name in ("oni_install", "game_identity", "process", "probe", "bridge", "llm"):
        item = checks.get(name) if isinstance(checks.get(name), dict) else {}
        status = str(item.get("status") or "unknown").upper()
        lines.append(f"[{status}] {name}")
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    if issues:
        lines.append("Issues:")
        for issue in issues[:32]:
            if isinstance(issue, dict):
                lines.append(f"- {issue.get('code')}: {issue.get('message')}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the full machine-readable report.")
    parser.add_argument("--bridge-dir", help=f"Bridge root (default: {DEFAULT_BRIDGE_DIR}).")
    parser.add_argument("--oni-dir", help="ONI installation root or OxygenNotIncluded.exe path (auto-detected by default).")
    parser.add_argument("--secrets-dir", help="Optional local secrets directory; only key presence is inspected.")
    parser.add_argument("--player-log", help=f"Player.log path (default: {DEFAULT_PLAYER_LOG}).")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_doctor(
            bridge_dir=args.bridge_dir,
            oni_dir=args.oni_dir,
            secrets_dir=args.secrets_dir,
            player_log=args.player_log,
        )
    except Exception as exc:  # Keep diagnostics safe and deterministic.
        report = {
            "schema": "oni.doctor.v1",
            "generated_at": now_iso(),
            "read_only": True,
            "status": "blocked",
            "exit_code": EXIT_BLOCKED,
            "checks": {},
            "issues": [{"severity": "error", "code": "doctor_exception", "message": type(exc).__name__}],
            "issue_counts": {"errors": 1, "warnings": 0},
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(human_summary(report))
    return int(report.get("exit_code", EXIT_BLOCKED))


if __name__ == "__main__":
    raise SystemExit(main())
