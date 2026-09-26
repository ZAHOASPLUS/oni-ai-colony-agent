#!/usr/bin/env python3
"""Isolated worker for the live-runtime-bootstrap offline verifier.

The parent owns source handles, process containment, and publication. This
worker imports only attested project sources and records the effective
run-scoped bytecode-cache paths before returning control to the parent.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
from types import ModuleType
from typing import Iterable


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
ATTESTATION_SCHEMA = "oni.live-runtime-bootstrap-pycache-attestation.v1"


def resolved_file(path: pathlib.Path, label: str) -> pathlib.Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"{label} is not a readable file: {path}") from exc
    if not resolved.is_file():
        raise RuntimeError(f"{label} is not a file: {resolved}")
    return resolved


def resolved_directory(path: pathlib.Path, label: str) -> pathlib.Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"{label} is not a readable directory: {path}") from exc
    if not resolved.is_dir() or path.is_symlink():
        raise RuntimeError(f"{label} is not a plain directory: {resolved}")
    return resolved


def path_is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def require_runtime_isolation(expected_prefix: pathlib.Path, stage: str) -> None:
    configured = sys.pycache_prefix
    if not isinstance(configured, str) or not configured:
        raise RuntimeError(f"sys.pycache_prefix is not configured during {stage}")
    observed = pathlib.Path(configured).expanduser().resolve(strict=True)
    if observed != expected_prefix:
        raise RuntimeError(
            f"sys.pycache_prefix mismatch during {stage}: "
            f"observed={observed} expected={expected_prefix}"
        )
    configured_option = sys._xoptions.get("pycache_prefix")
    if not isinstance(configured_option, str):
        raise RuntimeError(f"-X pycache_prefix was not explicit during {stage}")
    option_path = pathlib.Path(configured_option).expanduser().resolve(strict=True)
    if option_path != expected_prefix:
        raise RuntimeError(f"-X pycache_prefix mismatch during {stage}")
    if sys.flags.isolated != 1 or sys.flags.dont_write_bytecode != 1:
        raise RuntimeError(f"worker isolation or no-bytecode flag is missing during {stage}")
    if os.environ.get("PYTHONDONTWRITEBYTECODE") != "1":
        raise RuntimeError(f"child no-bytecode environment is missing during {stage}")
    for unsafe_name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"):
        if os.environ.get(unsafe_name):
            raise RuntimeError(f"unsafe child Python environment survived during {stage}: {unsafe_name}")
    if os.environ.get("PYTHONNOUSERSITE") != "1" or os.environ.get("PYTHONSAFEPATH") != "1":
        raise RuntimeError(f"child site/path isolation environment is missing during {stage}")
    inherited_prefix = os.environ.get("PYTHONPYCACHEPREFIX")
    if not inherited_prefix:
        raise RuntimeError(f"child pycache-prefix environment is missing during {stage}")
    if pathlib.Path(inherited_prefix).expanduser().resolve(strict=True) != expected_prefix:
        raise RuntimeError(f"child pycache-prefix environment mismatched during {stage}")


def require_fresh_empty_prefix(prefix: pathlib.Path) -> None:
    require_runtime_isolation(prefix, "pre-import check")
    if any(prefix.iterdir()):
        raise RuntimeError(f"pycache prefix was not empty before project imports: {prefix}")


def import_verifier(verifier_script: pathlib.Path) -> ModuleType:
    module_suffix = hashlib.sha256(str(verifier_script).encode("utf-8")).hexdigest()[:16]
    module_name = f"_oni_live_runtime_bootstrap_verifier_{module_suffix}"
    spec = importlib.util.spec_from_file_location(module_name, verifier_script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import verifier module from {verifier_script}")
    module = importlib.util.module_from_spec(spec)
    # Import-time constructs such as dataclasses require this registration
    # before exec_module() begins.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require_manager_binding(module: ModuleType, manager_script: pathlib.Path) -> None:
    declared_manager = getattr(module, "MANAGER", None)
    if declared_manager is None:
        raise RuntimeError("verifier module does not declare MANAGER")
    declared_path = pathlib.Path(declared_manager).resolve(strict=False)
    if declared_path != manager_script:
        raise RuntimeError(
            "verifier MANAGER does not match the attested manager: "
            f"declared={declared_path} expected={manager_script}"
        )

    # The production verifier imports the manager as ``core``. Binding the path
    # constant alone is insufficient if a different module was imported.
    core_module = getattr(module, "core", None)
    if not isinstance(core_module, ModuleType):
        raise RuntimeError("verifier module does not expose imported manager as core")
    core_file = getattr(core_module, "__file__", None)
    if not isinstance(core_file, (str, bytes, os.PathLike)):
        raise RuntimeError("verifier core module does not expose a file path")
    imported_path = pathlib.Path(core_file).resolve(strict=True)
    if imported_path != manager_script:
        raise RuntimeError(
            "verifier core module does not match the attested manager: "
            f"imported={imported_path} expected={manager_script}"
        )


def audit_loaded_project_modules(
    module_roots: Iterable[pathlib.Path],
    attested_sources: set[pathlib.Path],
    pycache_prefix: pathlib.Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    roots = tuple(module_roots)
    observed: list[dict[str, object]] = []
    namespaces: list[dict[str, object]] = []
    for module_name, module in sorted(sys.modules.items(), key=lambda item: item[0]):
        module_file = getattr(module, "__file__", None)
        module_spec = getattr(module, "__spec__", None)
        search_locations = getattr(module_spec, "submodule_search_locations", None)
        if not isinstance(module_file, (str, bytes, os.PathLike)):
            if search_locations is None:
                continue
            resolved_locations = [
                pathlib.Path(location).expanduser().resolve(strict=True)
                for location in search_locations
            ]
            relevant_locations = [
                location
                for location in resolved_locations
                if any(path_is_within(location, root) for root in roots)
            ]
            if not relevant_locations:
                continue
            if len(relevant_locations) != len(resolved_locations):
                raise RuntimeError(f"project namespace package escaped module roots: {module_name}")
            if getattr(module_spec, "origin", None) is not None:
                raise RuntimeError(f"project namespace package unexpectedly has code: {module_name}")
            namespaces.append(
                {"name": module_name, "searchLocations": [str(path) for path in resolved_locations]}
            )
            continue

        unresolved = pathlib.Path(module_file).expanduser().resolve(strict=False)
        if not any(path_is_within(unresolved, root) for root in roots):
            continue
        try:
            source_path = pathlib.Path(module_file).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"loaded project module has an unreadable __file__: {module_name}={module_file}"
            ) from exc
        if source_path.suffix.casefold() != ".py":
            raise RuntimeError(f"loaded project module is not source-backed: {module_name}={source_path}")
        if source_path not in attested_sources:
            raise RuntimeError(
                f"loaded project module is outside the held source set: {module_name}={source_path}"
            )

        spec_origin_value = getattr(module_spec, "origin", None)
        spec_origin: pathlib.Path | None = None
        if spec_origin_value is not None:
            if not isinstance(spec_origin_value, (str, bytes, os.PathLike)):
                raise RuntimeError(f"project module has a non-path spec.origin: {module_name}")
            spec_origin = pathlib.Path(spec_origin_value).expanduser().resolve(strict=True)
            if spec_origin != source_path:
                raise RuntimeError(f"project module spec.origin mismatched __file__: {module_name}")

        expected_cached = importlib.util.cache_from_source(str(source_path))
        expected_cached_path = pathlib.Path(expected_cached).resolve(strict=False)
        if not path_is_within(expected_cached_path, pycache_prefix):
            raise RuntimeError(f"cache_from_source escaped the run-scoped prefix: {module_name}")
        cached_value = getattr(module, "__cached__", None)
        spec_cached_value = getattr(module_spec, "cached", None)
        cached_path: pathlib.Path | None = None
        spec_cached_path: pathlib.Path | None = None
        if cached_value is not None:
            if not isinstance(cached_value, (str, bytes, os.PathLike)):
                raise RuntimeError(f"project module has a non-path __cached__: {module_name}")
            cached_path = pathlib.Path(cached_value).expanduser().resolve(strict=False)
            if cached_path != expected_cached_path:
                raise RuntimeError(f"project module __cached__ mismatched cache_from_source: {module_name}")
        if spec_cached_value is not None:
            if not isinstance(spec_cached_value, (str, bytes, os.PathLike)):
                raise RuntimeError(f"project module has a non-path spec.cached: {module_name}")
            spec_cached_path = pathlib.Path(spec_cached_value).expanduser().resolve(strict=False)
            if spec_cached_path != expected_cached_path:
                raise RuntimeError(f"project module spec.cached mismatched cache_from_source: {module_name}")
        if module_name != "__main__" and (cached_path is None or spec_cached_path is None):
            raise RuntimeError(f"project module lacks bytecode-cache metadata: {module_name}")
        if expected_cached_path.exists() or expected_cached_path.is_symlink():
            raise RuntimeError(f"project bytecode cache unexpectedly exists: {expected_cached_path}")
        observed.append(
            {
                "name": module_name,
                "filePath": str(source_path),
                "specOrigin": None if spec_origin is None else str(spec_origin),
                "cachedPath": None if cached_path is None else str(cached_path),
                "specCachedPath": None if spec_cached_path is None else str(spec_cached_path),
            }
        )
    if not observed:
        raise RuntimeError("no loaded project modules were available for pycache attestation")
    return observed, namespaces


def write_attestation(path: pathlib.Path, document: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"worker attestation path is not fresh: {path}")
    raw = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def run_verifier(
    verifier_script: pathlib.Path,
    manager_script: pathlib.Path,
    report_path: pathlib.Path,
    rounds: int,
    module_roots: Iterable[pathlib.Path],
    attested_sources: set[pathlib.Path],
    pycache_prefix: pathlib.Path,
) -> int:
    if rounds != 2:
        raise RuntimeError(f"worker requires exactly two rounds, received {rounds}")
    if report_path.exists() or report_path.is_symlink():
        raise RuntimeError(f"worker report path is not fresh: {report_path}")

    module = import_verifier(verifier_script)
    require_manager_binding(module, manager_script)
    # The import closure is authoritative only after the verifier and its
    # manager binding have completed.  Keep the post-main audit below as a
    # separate check so delayed imports are still rejected after execution.
    audit_loaded_project_modules(module_roots, attested_sources, pycache_prefix)

    if not hasattr(module, "REPORT"):
        raise RuntimeError("verifier module does not declare REPORT")
    declared_report = pathlib.Path(module.REPORT).resolve(strict=False)
    if declared_report == report_path:
        raise RuntimeError("worker report path is not external to the verifier default")
    # Reject an import-time writer before assigning the external report path.
    if report_path.exists() or report_path.is_symlink():
        raise RuntimeError(f"worker report path was created during import: {report_path}")

    module.REPORT = report_path
    previous_argv = list(sys.argv)
    previous_tempdir = tempfile.tempdir
    # Keep verifier-created roots with the worker logs/report so a failed run's
    # complete evidence remains under one run-scoped directory.
    tempfile.tempdir = str(report_path.parent)
    sys.argv = [str(verifier_script), "--rounds", str(rounds)]
    try:
        result = module.main()
    finally:
        sys.argv = previous_argv
        tempfile.tempdir = previous_tempdir

    if type(result) is not int:
        raise RuntimeError(f"verifier main() returned non-integer status: {type(result).__name__}")
    if result == 0 and not report_path.is_file():
        raise RuntimeError(f"verifier returned success without a report: {report_path}")
    return result if 0 <= result <= 255 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verifier", type=pathlib.Path, required=True)
    parser.add_argument("--manager", type=pathlib.Path, required=True)
    parser.add_argument("--rounds", type=int, choices=(2,), default=2)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--pycache-prefix", type=pathlib.Path, required=True)
    parser.add_argument("--attestation-report", type=pathlib.Path, required=True)
    parser.add_argument("--attested-source", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--module-root", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--required-loaded-source", type=pathlib.Path, action="append", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    verifier_script = resolved_file(args.verifier, "verifier")
    manager_script = resolved_file(args.manager, "manager")
    report_path = args.report.expanduser().resolve(strict=False)
    attestation_path = args.attestation_report.expanduser().resolve(strict=False)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    pycache_prefix = resolved_directory(args.pycache_prefix, "pycache prefix")
    if pycache_prefix.parent != report_path.parent:
        raise RuntimeError("pycache prefix must be a direct child of the worker execution root")
    if attestation_path.parent != report_path.parent:
        raise RuntimeError("worker attestation must be inside the worker execution root")

    module_roots = tuple(resolved_directory(path, "module root") for path in args.module_root)
    attested_sources = {resolved_file(path, "attested source") for path in args.attested_source}
    required_loaded_sources = {
        resolved_file(path, "required loaded source") for path in args.required_loaded_source
    }
    worker_source = pathlib.Path(__file__).resolve(strict=True)
    for required_source in (worker_source, verifier_script, manager_script):
        if required_source not in attested_sources:
            raise RuntimeError(f"required worker input is outside the held source set: {required_source}")
    if not required_loaded_sources.issubset(attested_sources):
        raise RuntimeError("a required loaded source is outside the held source set")
    for source in attested_sources:
        if not any(path_is_within(source, root) for root in module_roots):
            raise RuntimeError(f"attested Python source is outside every module root: {source}")

    require_fresh_empty_prefix(pycache_prefix)
    result = run_verifier(
        verifier_script,
        manager_script,
        report_path,
        args.rounds,
        module_roots,
        attested_sources,
        pycache_prefix,
    )
    require_runtime_isolation(pycache_prefix, "post-main check")
    loaded_modules, namespaces = audit_loaded_project_modules(
        module_roots, attested_sources, pycache_prefix
    )
    loaded_sources = {pathlib.Path(item["filePath"]) for item in loaded_modules}
    missing_loaded = required_loaded_sources - loaded_sources
    if missing_loaded:
        raise RuntimeError(
            "required project sources were not loaded: "
            + ", ".join(str(path) for path in sorted(missing_loaded))
        )
    prefix_entries = sorted(str(path.relative_to(pycache_prefix)) for path in pycache_prefix.rglob("*"))
    if prefix_entries:
        raise RuntimeError("pycache prefix was not empty after verifier main")
    attested_text = sorted(str(path) for path in attested_sources)
    write_attestation(
        attestation_path,
        {
            "schema": ATTESTATION_SCHEMA,
            "status": "passed",
            "pycachePrefix": str(pycache_prefix),
            "isolated": True,
            "dontWriteBytecode": True,
            "childEnvironmentSanitized": True,
            "prefixEmptyBeforeImport": True,
            "prefixEmptyAfterMain": True,
            "inheritedPycachePrefix": os.environ["PYTHONPYCACHEPREFIX"],
            "attestedSources": attested_text,
            "loadedProjectModules": loaded_modules,
            "projectNamespaces": namespaces,
        },
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
