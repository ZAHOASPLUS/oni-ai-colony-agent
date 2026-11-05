"""Checkpoint chain for a named test clone (roadmap B16, decision D4).

A game launch always loaded the original clone save, so a run's progress was
lost with its process (Phase 2 session start cycles 15.8 -> 11.7 -> 10.0 ->
6.4).  The Mod can already write a verified save (``save_checkpoint``); this
module gives those saves an identity the launcher can trust.

Decision D4 (2026-10-27): ``SaveLoader.Save`` writes no ``.png`` preview, and a
checkpoint is not identified by a copied placeholder preview.  Its identity is
a manifest chain instead.  Checkpoint ``n`` of a clone lives next to the clone
save as ``<stem>-ckpt-<nnn>.sav`` with ``<stem>-ckpt-<nnn>.manifest.json``; the
manifest binds the save bytes, the root clone manifest and the previous link
(the clone itself for checkpoint 1).

``select_latest_checkpoint`` walks the whole chain and fails closed on any gap,
fork, foreign file, hash mismatch, autosave or link to a different root: a
broken chain is evidence to inspect, never something to skip over.  Nothing
here launches a process or writes a save; the only write is the create-only
manifest publication.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
from typing import Any, Dict, List, Mapping, Optional

from agent.clone_launch_guard import (
    CloneLaunchGuardError,
    _digest,
    _load_manifest,
    _path_id,
    _read,
    _regular_file,
    read_save_header,
)

CHECKPOINT_SCHEMA = "oni.clone-checkpoint.v1"
MAX_CHECKPOINTS = 999
_INDEX_RE = re.compile(r"^(?P<stem>.+)-ckpt-(?P<index>[0-9]{3})\.(?P<kind>sav|manifest\.json)$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,159}$")


class CheckpointChainError(RuntimeError):
    pass


def checkpoint_paths(clone_save: pathlib.Path, index: int) -> Dict[str, pathlib.Path]:
    """The save and manifest paths of checkpoint ``index`` (1..999) of a clone."""

    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= MAX_CHECKPOINTS:
        raise CheckpointChainError("checkpoint index must be an integer in 1..999")
    clone_save = pathlib.Path(clone_save)
    stem = f"{clone_save.stem}-ckpt-{index:03d}"
    return {
        "save": clone_save.with_name(stem + ".sav"),
        "manifest": clone_save.with_name(stem + ".manifest.json"),
    }


def _root(clone_manifest: pathlib.Path, clone_save: pathlib.Path) -> Dict[str, Any]:
    """The verified root link: the named clone exactly as its manifest created it."""

    try:
        manifest_file = _regular_file(pathlib.Path(clone_manifest).expanduser().resolve(), "The clone manifest")
        manifest = _load_manifest(manifest_file)
        save = _regular_file(pathlib.Path(clone_save).expanduser().resolve(), "The clone save")
        payload = _read(save)
    except CloneLaunchGuardError as exc:
        raise CheckpointChainError(str(exc)) from exc
    target = manifest["target"]
    expected = str(target.get("save_sha256") or "")
    if _digest(payload) != expected:
        raise CheckpointChainError("The clone save does not match its manifest.")
    recorded_path = target.get("save_path_id")
    if recorded_path is None and isinstance(target.get("save"), Mapping):
        recorded_path = target["save"].get("path_id")
    if _path_id(save) != str(recorded_path or ""):
        raise CheckpointChainError("The clone save is not the file this manifest created.")
    return {
        "index": 0,
        "save_path": save,
        "save_sha256": expected,
        "manifest_path": manifest_file,
        "manifest_sha256": _digest(_read(manifest_file)),
    }


def build_checkpoint_manifest(
    *,
    clone_manifest: pathlib.Path,
    clone_save: pathlib.Path,
    index: int,
    run_id: str,
    session_id: str,
    game_time_cycles: float,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Describe a checkpoint save the Mod has already written and verified.

    The previous link must be intact: checkpoint ``index`` extends the chain
    only when ``select_latest_checkpoint`` currently ends at ``index - 1``.
    """

    for name, value in (("run_id", run_id), ("session_id", session_id)):
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            raise CheckpointChainError(f"{name} is invalid")
    if isinstance(game_time_cycles, bool) or not isinstance(game_time_cycles, (int, float)) or game_time_cycles < 0:
        raise CheckpointChainError("game_time_cycles must be a non-negative number")
    latest = _select(clone_manifest, clone_save, pending_index=index)
    if latest["index"] != index - 1:
        raise CheckpointChainError(
            f"checkpoint {index} does not extend the chain, which ends at {latest['index']}"
        )
    paths = checkpoint_paths(latest["root_save_path"], index)
    save = _checked_save(paths["save"])
    payload = _read(save)
    return {
        "schema": CHECKPOINT_SCHEMA,
        "index": index,
        "root": {
            "clone_manifest_sha256": latest["root_manifest_sha256"],
            "clone_save_sha256": latest["root_save_sha256"],
        },
        "parent": {"index": latest["index"], "save_sha256": latest["save_sha256"]},
        "save": {
            "file_name": save.name,
            "path_id": _path_id(save),
            "sha256": _digest(payload),
            "size": len(payload),
        },
        "run_id": run_id,
        "session_id": session_id,
        "game_time_cycles": float(game_time_cycles),
        "created_at": created_at or dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def publish_checkpoint_manifest(path: pathlib.Path, manifest: Mapping[str, Any]) -> None:
    """Create the manifest file; an existing file is never replaced."""

    payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(str(path), flags, 0o644)
    except FileExistsError as exc:
        raise CheckpointChainError("The checkpoint manifest already exists.") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _checked_save(path: pathlib.Path) -> pathlib.Path:
    try:
        save = _regular_file(path, "The checkpoint save")
        header = read_save_header(_read(save))
    except CloneLaunchGuardError as exc:
        raise CheckpointChainError(str(exc)) from exc
    if header["is_auto_save"]:
        # An autosave header points the active save back at the source colony
        # folder on load (clone_launch_guard, review P0-1).
        raise CheckpointChainError("The checkpoint save is an autosave.")
    return save


def _section(link: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = link.get(name)
    return value if isinstance(value, Mapping) else {}


def _load_link(path: pathlib.Path) -> Dict[str, Any]:
    try:
        manifest_file = _regular_file(path, "The checkpoint manifest")
        value = json.loads(_read(manifest_file).decode("utf-8"))
    except (CloneLaunchGuardError, UnicodeError, ValueError) as exc:
        raise CheckpointChainError("A checkpoint manifest is unreadable.") from exc
    if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA:
        raise CheckpointChainError("A checkpoint manifest has the wrong schema.")
    return value


def select_latest_checkpoint(clone_manifest: pathlib.Path, clone_save: pathlib.Path) -> Dict[str, Any]:
    """Return the newest intact link of the clone's chain (index 0 is the clone).

    Every ``-ckpt-`` file beside the clone must belong to one gap-free chain
    1..n whose links all verify; anything else raises.
    """

    return _select(clone_manifest, clone_save, pending_index=None)


def _select(
    clone_manifest: pathlib.Path, clone_save: pathlib.Path, *, pending_index: Optional[int]
) -> Dict[str, Any]:
    """Walk the chain; ``pending_index`` admits only the save being described.

    While its manifest is being built, checkpoint ``pending_index`` exists as a
    save without a manifest, and a run that saved again has later saves waiting
    the same way.  Those files, at the end of the chain, are left out; every
    other incomplete link still raises.
    """

    root = _root(clone_manifest, clone_save)
    directory = root["save_path"].parent
    stem = root["save_path"].stem
    found: Dict[int, Dict[str, pathlib.Path]] = {}
    for entry in directory.iterdir():
        match = _INDEX_RE.fullmatch(entry.name)
        if match is None or match.group("stem") != stem:
            continue
        kind = "save" if match.group("kind") == "sav" else "manifest"
        found.setdefault(int(match.group("index")), {})[kind] = entry
    if found and sorted(found) != list(range(1, max(found) + 1)):
        raise CheckpointChainError("The checkpoint chain has a gap.")
    if pending_index is not None and pending_index in found:
        waiting = [index for index in found if index >= pending_index]
        if any(set(found[index]) != {"save"} for index in waiting):
            raise CheckpointChainError(f"Checkpoint {pending_index} is not a new save at the end of the chain.")
        for index in waiting:
            del found[index]

    current = root
    chain: List[int] = [0]
    for index in sorted(found):
        files = found[index]
        if set(files) != {"save", "manifest"}:
            raise CheckpointChainError(f"Checkpoint {index} lacks its save or its manifest.")
        link = _load_link(files["manifest"])
        save = _checked_save(files["save"])
        payload = _read(save)
        expected = checkpoint_paths(root["save_path"], index)
        link_root, parent, link_save = _section(link, "root"), _section(link, "parent"), _section(link, "save")
        recorded = link_save.get("sha256")
        checks = (
            link.get("index") == index and not isinstance(link.get("index"), bool),
            link_root.get("clone_manifest_sha256") == root["manifest_sha256"],
            link_root.get("clone_save_sha256") == root["save_sha256"],
            parent.get("index") == current["index"] and not isinstance(parent.get("index"), bool),
            parent.get("save_sha256") == current["save_sha256"],
            link_save.get("file_name") == expected["save"].name,
            link_save.get("path_id") == _path_id(save),
            isinstance(recorded, str) and _HASH_RE.fullmatch(recorded) is not None,
            recorded == _digest(payload),
        )
        if not all(checks):
            raise CheckpointChainError(f"Checkpoint {index} does not verify against its chain.")
        current = {
            "index": index,
            "save_path": save,
            "save_sha256": recorded,
            "manifest_path": files["manifest"],
            "manifest_sha256": _digest(_read(files["manifest"])),
            "game_time_cycles": link.get("game_time_cycles"),
        }
        chain.append(index)
    return {
        **current,
        "chain": chain,
        "root_save_path": root["save_path"],
        "root_save_sha256": root["save_sha256"],
        "root_manifest_sha256": root["manifest_sha256"],
    }


def loader_path_id_upper(path: pathlib.Path) -> str:
    """The Probe/startup-binding path id: sha256 of the full resolved path, upper-cased.

    Named-clone manifests hash ``path.lower()``; the runtime startup contract
    hashes the upper-cased path (see ``prepare_dsh_autonomous_run.loader_path_id``
    and ``oni_autonomous.load_config``).
    """

    text = str(pathlib.Path(path).expanduser().resolve()).rstrip("\\/").upper()
    return _digest(text.encode("utf-8"))


def loader_target(clone_manifest: pathlib.Path, clone_save: pathlib.Path) -> Dict[str, Any]:
    """What a launch should load: the newest intact link (the clone when none)."""

    latest = select_latest_checkpoint(clone_manifest, clone_save)
    save = pathlib.Path(latest["save_path"])
    return {
        "index": latest["index"],
        "save_path": save,
        "save_sha256": latest["save_sha256"],
        "save_length": save.stat().st_size,
        "path_id_upper": loader_path_id_upper(save),
        "chain": latest["chain"],
    }


def match_loader_anchors(
    clone_manifest: pathlib.Path,
    clone_save: pathlib.Path,
    *,
    loader_save_sha256: str,
    loader_path_id_upper_value: str,
) -> Dict[str, Any]:
    """Accept the external loader anchors only for the newest link of the chain.

    Loading the clone or an older checkpoint while a newer one exists would
    silently drop the progress the newer checkpoint holds, so it is refused.
    """

    target = loader_target(clone_manifest, clone_save)
    if target["save_sha256"] != loader_save_sha256 or target["path_id_upper"] != loader_path_id_upper_value:
        raise CheckpointChainError(
            f"The loader anchors are not the newest link of the chain (index {target['index']})."
        )
    return target
