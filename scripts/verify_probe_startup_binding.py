#!/usr/bin/env python3
"""Offline contract and source-order verification for Probe startup binding."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
STARTUP = ROOT / "mod" / "Source" / "Probe" / "StartupBinding.cs"
SESSION = ROOT / "mod" / "Source" / "Probe" / "ProbeSession.cs"
RUNNER = ROOT / "mod" / "Source" / "Probe" / "ProbeRunner.cs"
WRITER = ROOT / "mod" / "Source" / "Probe" / "ProbeWriter.cs"
MOD = ROOT / "mod" / "Source" / "Mod.cs"
LIFECYCLE = ROOT / "mod" / "Source" / "Patches" / "LifecyclePatches.cs"
CHECKPOINT_CONTROLLER = ROOT / "mod" / "Source" / "Probe" / "ValidationCheckpointController.cs"

BINDING_FIELDS = {
    "schema",
    "validation_run_id",
    "startup_nonce",
    "startup_intent_hash",
    "runtime_config_evidence_sha256",
    "environment_manifest_sha256",
    "expected_game_build",
    "expected_mod_version",
    "expected_probe_dll_sha256",
    "runtime_bootstrap_result_path_id_lower",
    "runtime_bootstrap_result_sha256",
    "runtime_bootstrap_result_hash",
    "runtime_bootstrap_transaction_id",
    "installed_probe_tree_content_hash",
    "requested_loader_path_id_upper",
    "requested_loader_save_sha256",
    "requested_loader_save_length",
    "created_at",
    "document_hash",
}
RECORD_FIELDS = {
    "validation_run_id",
    "startup_binding_hash",
    "startup_intent_hash",
    "runtime_config_evidence_sha256",
    "validation_process_id",
    "startup_binding_status",
    "requested_save_path_id",
    "active_save_path_id",
}
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
NONCE = re.compile(r"^startup-[0-9a-f]{32}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
RUNTIME_BOOTSTRAP_TRANSACTION = re.compile(r"^rtb-[0-9a-f]{24}$")
OFFSET = re.compile(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$")
ASSERTIONS = 0
EXPECTED_ASSERTION_COUNT = 512


def require(condition: bool, message: str) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(message)


def sha256_id(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def document_hash(document: dict[str, Any]) -> str:
    material = {key: value for key, value in document.items() if key != "document_hash"}
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256_id(encoded)


def reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key}")
        result[key] = value
    return result


def strict_loads(raw: bytes) -> dict[str, Any]:
    require(0 < len(raw) <= 64 * 1024, "binding byte length must be 1..65536")
    text = raw.decode("utf-8", errors="strict")
    value = json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    require(type(value) is dict, "binding root must be an object")
    return value


def parse_timestamp(value: str) -> bool:
    if not OFFSET.search(value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_document(
    document: dict[str, Any],
    *,
    game_build: str,
    mod_version: str,
    dll_hash: str,
) -> None:
    require(set(document) == BINDING_FIELDS, "binding field set must be exact")
    string_fields = sorted(BINDING_FIELDS - {"requested_loader_save_length"})
    for key in string_fields:
        value = document[key]
        require(type(value) is str and value and value.isascii(), f"{key} must be nonempty ASCII")
    require(document["schema"] == "oni.runtime-startup-binding.v2", "schema mismatch")
    require(bool(RUN_ID.fullmatch(document["validation_run_id"])), "run id mismatch")
    require(bool(NONCE.fullmatch(document["startup_nonce"])), "nonce mismatch")
    for key in sorted(
        {
            "startup_intent_hash",
            "runtime_config_evidence_sha256",
            "environment_manifest_sha256",
            "expected_probe_dll_sha256",
            "runtime_bootstrap_result_path_id_lower",
            "runtime_bootstrap_result_sha256",
            "runtime_bootstrap_result_hash",
            "installed_probe_tree_content_hash",
            "requested_loader_path_id_upper",
            "requested_loader_save_sha256",
            "document_hash",
        }
    ):
        require(bool(SHA256.fullmatch(document[key])), f"{key} must be lowercase sha256")
    length = document["requested_loader_save_length"]
    require(type(length) is int and 0 <= length <= (1 << 63) - 1, "length must be nonnegative Int64")
    require(
        bool(RUNTIME_BOOTSTRAP_TRANSACTION.fullmatch(
            document["runtime_bootstrap_transaction_id"]
        )),
        "runtime bootstrap transaction id mismatch",
    )
    require(document["expected_game_build"] == game_build, "game build mismatch")
    require(document["expected_mod_version"] == mod_version, "mod version mismatch")
    require(document["expected_probe_dll_sha256"] == dll_hash, "DLL hash mismatch")
    require(parse_timestamp(document["created_at"]), "created_at needs an explicit offset")
    require(document["document_hash"] == document_hash(document), "document hash mismatch")


def make_binding() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "oni.runtime-startup-binding.v2",
        "validation_run_id": "startup-test-01",
        "startup_nonce": "startup-0123456789abcdef0123456789abcdef",
        "startup_intent_hash": "sha256:" + "11" * 32,
        "runtime_config_evidence_sha256": "sha256:" + "22" * 32,
        "environment_manifest_sha256": "sha256:" + "33" * 32,
        "expected_game_build": "cl-744825",
        "expected_mod_version": "0.7.0",
        "expected_probe_dll_sha256": "sha256:" + "44" * 32,
        "runtime_bootstrap_result_path_id_lower": "sha256:" + "45" * 32,
        "runtime_bootstrap_result_sha256": "sha256:" + "46" * 32,
        "runtime_bootstrap_result_hash": "sha256:" + "47" * 32,
        "runtime_bootstrap_transaction_id": "rtb-" + "48" * 12,
        "installed_probe_tree_content_hash": "sha256:" + "49" * 32,
        "requested_loader_path_id_upper": "sha256:" + "55" * 32,
        "requested_loader_save_sha256": "sha256:" + "66" * 32,
        "requested_loader_save_length": 1234,
        "created_at": "2026-08-31T08:00:00+08:00",
    }
    value["document_hash"] = document_hash(value)
    return value


def rehash(document: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(document)
    result["document_hash"] = document_hash(result)
    return result


@dataclass
class BindingState:
    path_id: str
    startup_hash: str
    startup_length: int
    status: str = "pending_request"
    terminal: bool = False
    request_ready: bool = False
    verified: bool = False
    authorized_hash: str | None = None
    authorized_length: int = -1
    active_path_id: str | None = None
    authorized_transition_path_id: str | None = None

    def fail(self, category: str) -> None:
        if self.terminal:
            return
        self.terminal = True
        self.request_ready = False
        self.verified = False
        self.status = category
        self.authorized_transition_path_id = None

    def request(self, path_id: str, save_hash: str, length: int) -> None:
        if self.terminal:
            return
        required_path = self.active_path_id if self.verified else self.path_id
        if path_id != required_path:
            self.fail("request_path_mismatch")
            return
        required_hash = self.authorized_hash if self.verified else self.startup_hash
        required_length = self.authorized_length if self.verified else self.startup_length
        if save_hash != required_hash:
            self.fail("request_hash_mismatch")
            return
        if length != required_length:
            self.fail("request_length_mismatch")
            return
        self.request_ready = True
        if not self.verified:
            self.status = "request_verified"

    def active(self, path_id: str, save_hash: str, length: int) -> None:
        if self.terminal:
            return
        if not self.request_ready:
            self.fail("request_missing")
            return
        required_path = self.active_path_id if self.verified else self.path_id
        if path_id != required_path:
            self.fail("active_path_mismatch")
            return
        required_hash = self.authorized_hash if self.verified else self.startup_hash
        required_length = self.authorized_length if self.verified else self.startup_length
        if save_hash != required_hash or length != required_length:
            self.fail("active_hash_mismatch")
            return
        self.request_ready = False
        self.authorized_hash = save_hash
        self.authorized_length = length
        self.active_path_id = path_id
        self.verified = True
        self.status = "verified"

    def authorize_transition(self, target_path_id: str) -> None:
        if self.terminal:
            return
        if not self.verified or not SHA256.fullmatch(target_path_id):
            self.fail("save_transition_authorization_invalid")
            return
        if self.authorized_transition_path_id is not None:
            self.fail("save_transition_authorization_nested")
            return
        if target_path_id == self.active_path_id:
            self.fail("save_transition_authorization_replay")
            return
        self.authorized_transition_path_id = target_path_id

    def clear_transition(self, target_path_id: str) -> None:
        if self.authorized_transition_path_id is None:
            return
        if target_path_id != self.authorized_transition_path_id:
            self.fail("save_transition_authorization_clear_mismatch")
            return
        self.authorized_transition_path_id = None

    def verified_write(self, path_id: str, save_hash: str, length: int) -> None:
        if self.terminal or not self.verified:
            return
        transition_pending = self.authorized_transition_path_id is not None
        required_path = (
            self.authorized_transition_path_id if transition_pending else self.active_path_id
        )
        if path_id != required_path:
            self.fail("save_transition_target_mismatch" if transition_pending else "active_path_mismatch")
            return
        if not SHA256.fullmatch(save_hash) or length < 0:
            self.fail("active_hash_mismatch")
            return
        self.authorized_hash = save_hash
        self.authorized_length = length
        self.active_path_id = path_id
        self.authorized_transition_path_id = None


def method_body(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"method did not close: {signature}")


def assert_rejects(document: dict[str, Any], message: str, **expected: str) -> None:
    candidate = rehash(document)
    try:
        validate_document(
            candidate,
            game_build=expected.get("game_build", "cl-744825"),
            mod_version=expected.get("mod_version", "0.7.0"),
            dll_hash=expected.get("dll_hash", "sha256:" + "44" * 32),
        )
    except AssertionError:
        return
    raise AssertionError(message)


def verify_behavior() -> None:
    valid = make_binding()
    raw = json.dumps(valid, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    parsed = strict_loads(raw)
    validate_document(
        parsed,
        game_build="cl-744825",
        mod_version="0.7.0",
        dll_hash="sha256:" + "44" * 32,
    )

    extra = copy.deepcopy(valid)
    extra["unexpected"] = "x"
    assert_rejects(extra, "extra field was accepted")
    missing = copy.deepcopy(valid)
    del missing["created_at"]
    assert_rejects(missing, "missing field was accepted")
    legacy = copy.deepcopy(valid)
    legacy["schema"] = "oni.runtime-startup-binding.v1"
    assert_rejects(legacy, "legacy v1 binding was accepted")
    added_fields = {
        "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256",
        "runtime_bootstrap_result_hash",
        "runtime_bootstrap_transaction_id",
        "installed_probe_tree_content_hash",
    }
    for field in sorted(added_fields):
        missing_added = copy.deepcopy(valid)
        del missing_added[field]
        assert_rejects(missing_added, f"missing v2 field was accepted: {field}")
        wrong_type = copy.deepcopy(valid)
        wrong_type[field] = True
        assert_rejects(wrong_type, f"bool v2 field was accepted: {field}")
    bool_length = copy.deepcopy(valid)
    bool_length["requested_loader_save_length"] = True
    assert_rejects(bool_length, "bool save length was accepted")

    duplicate = raw.replace(
        b'"schema":"oni.runtime-startup-binding.v2",',
        b'"schema":"oni.runtime-startup-binding.v2","schema":"duplicate",',
        1,
    )
    for malformed, label in (
        (duplicate, "duplicate field"),
        (raw + b"{}", "trailing JSON"),
        (raw.replace(b'"requested_loader_save_length":1234', b'"requested_loader_save_length":NaN'), "nonfinite"),
    ):
        try:
            strict_loads(malformed)
        except (AssertionError, UnicodeError, ValueError, json.JSONDecodeError):
            pass
        else:
            raise AssertionError(f"strict parser accepted {label}")

    bad_hash = copy.deepcopy(valid)
    bad_hash["startup_intent_hash"] = "sha256:" + "A" * 64
    assert_rejects(bad_hash, "uppercase hash was accepted")
    bad_document_hash = copy.deepcopy(valid)
    bad_document_hash["document_hash"] = "sha256:" + "00" * 32
    try:
        validate_document(
            bad_document_hash,
            game_build="cl-744825",
            mod_version="0.7.0",
            dll_hash="sha256:" + "44" * 32,
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("bad document hash was accepted")
    assert_rejects({**valid, "expected_game_build": "cl-1"}, "game mismatch was accepted")
    assert_rejects({**valid, "expected_mod_version": "9.9.9"}, "mod mismatch was accepted")
    assert_rejects(
        valid,
        "DLL mismatch was accepted",
        dll_hash="sha256:" + "99" * 32,
    )

    path_id = valid["requested_loader_path_id_upper"]
    save_hash = valid["requested_loader_save_sha256"]
    length = valid["requested_loader_save_length"]
    wrong_path = "sha256:" + "77" * 32
    wrong_hash = "sha256:" + "88" * 32

    state = BindingState(path_id, save_hash, length)
    state.request(wrong_path, save_hash, length)
    require(state.status == "request_path_mismatch" and state.terminal, "request path mismatch did not latch")
    state.request(path_id, save_hash, length)
    require(state.status == "request_path_mismatch", "request path failure recovered")

    state = BindingState(path_id, save_hash, length)
    state.request(path_id, wrong_hash, length)
    require(state.status == "request_hash_mismatch", "request content mismatch not detected")

    state = BindingState(path_id, save_hash, length)
    state.request(path_id, save_hash, length)
    state.active(wrong_path, save_hash, length)
    require(state.status == "active_path_mismatch", "active path mismatch not detected")

    state = BindingState(path_id, save_hash, length)
    state.request(path_id, save_hash, length)
    state.active(path_id, wrong_hash, length)
    require(state.status == "active_hash_mismatch", "active content mismatch not detected")

    state = BindingState(path_id, save_hash, length)
    state.request(path_id, save_hash, length)
    state.active(path_id, save_hash, length)
    require(state.verified and state.status == "verified", "initial binding did not verify")
    new_hash = "sha256:" + "99" * 32
    state.verified_write(path_id, new_hash, length + 10)
    state.request(path_id, new_hash, length + 10)
    state.active(path_id, new_hash, length + 10)
    require(state.verified and state.status == "verified", "verified same-path save/reload was rejected")

    state.verified_write(path_id, "sha256:" + "aa" * 32, length + 20)
    state.request(wrong_path, "sha256:" + "aa" * 32, length + 20)
    require(state.status == "request_path_mismatch", "unauthorized reload path switch did not latch")

    state = BindingState(path_id, save_hash, length)
    state.request(path_id, save_hash, length)
    state.active(path_id, save_hash, length)
    state.request(path_id, wrong_hash, length)
    require(state.status == "request_hash_mismatch", "unobserved post-verify content change was accepted")

    derivative_path = "sha256:" + "bb" * 32
    derivative_hash = "sha256:" + "aa" * 32
    state = BindingState(path_id, save_hash, length)
    state.request(path_id, save_hash, length)
    state.active(path_id, save_hash, length)
    state.authorize_transition(derivative_path)
    state.verified_write(derivative_path, derivative_hash, length + 20)
    require(
        state.verified
        and state.active_path_id == derivative_path
        and state.authorized_transition_path_id is None,
        "authorized distinct Save-As did not atomically migrate the active identity",
    )
    generic_record = {"requested_save_path_id": path_id, "active_save_path_id": derivative_path}
    require(
        generic_record["requested_save_path_id"] == path_id
        and generic_record["active_save_path_id"] == derivative_path,
        "generic record does not retain loader request plus current derivative active path",
    )
    state.request(derivative_path, derivative_hash, length + 20)
    state.active(derivative_path, derivative_hash, length + 20)
    require(state.verified and not state.terminal, "reload of the authorized derivative was rejected")

    unauthorized = BindingState(path_id, save_hash, length)
    unauthorized.request(path_id, save_hash, length)
    unauthorized.active(path_id, save_hash, length)
    unauthorized.verified_write(derivative_path, derivative_hash, length + 20)
    require(
        unauthorized.terminal and unauthorized.status == "active_path_mismatch",
        "unauthorized Save-As transition was accepted",
    )

    wrong_target = BindingState(path_id, save_hash, length)
    wrong_target.request(path_id, save_hash, length)
    wrong_target.active(path_id, save_hash, length)
    wrong_target.authorize_transition(derivative_path)
    wrong_target.verified_write(wrong_path, derivative_hash, length + 20)
    require(
        wrong_target.terminal and wrong_target.status == "save_transition_target_mismatch",
        "authorized transition accepted a different completion target",
    )

    replay = BindingState(path_id, save_hash, length)
    replay.request(path_id, save_hash, length)
    replay.active(path_id, save_hash, length)
    replay.authorize_transition(derivative_path)
    replay.verified_write(derivative_path, derivative_hash, length + 20)
    replay.authorize_transition(derivative_path)
    require(
        replay.terminal and replay.status == "save_transition_authorization_replay",
        "transition authorization was replayable after consumption",
    )

    stale = BindingState(path_id, save_hash, length)
    stale.request(path_id, save_hash, length)
    stale.active(path_id, save_hash, length)
    stale.authorize_transition(derivative_path)
    stale.clear_transition(derivative_path)
    require(
        stale.verified and stale.authorized_transition_path_id is None,
        "failed save left a stale transition authorization",
    )
    stale.verified_write(derivative_path, derivative_hash, length + 20)
    require(stale.terminal, "cleared failure authorization permitted a later transition")

    nested = BindingState(path_id, save_hash, length)
    nested.request(path_id, save_hash, length)
    nested.active(path_id, save_hash, length)
    nested.authorize_transition(derivative_path)
    nested.authorize_transition(wrong_path)
    require(
        nested.terminal and nested.status == "save_transition_authorization_nested",
        "concurrent/nested transition authorization was accepted",
    )


def verify_csharp_contract() -> None:
    startup = STARTUP.read_text(encoding="utf-8")
    session = SESSION.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    writer = WRITER.read_text(encoding="utf-8")
    mod = MOD.read_text(encoding="utf-8")
    lifecycle = LIFECYCLE.read_text(encoding="utf-8")
    checkpoint_controller = CHECKPOINT_CONTROLLER.read_text(encoding="utf-8")

    exact_field_block = startup[startup.index("ExactFields") : startup.index("ProcessId =")]
    for field in sorted(BINDING_FIELDS):
        require(f'"{field}"' in exact_field_block, f"C# exact field set misses {field}")
    require("document.Count != ExactFields.Count" in startup, "C# does not reject missing fields")
    require("!ExactFields.Contains(property.Name)" in startup, "C# does not reject extra fields")
    require("CanonicalHashContract.ParseObjectStrict(json)" in startup, "C# does not use strict JSON")
    require("MaximumBindingBytes = 64 * 1024" in startup, "C# size limit is absent")
    require("new UTF8Encoding(false, true)" in startup, "C# UTF-8 decoder is not strict")
    require("bindingHash = HashBytes(raw)" in startup, "raw-byte binding hash is not frozen")
    require("ComputeDocumentHash(document)" in startup, "document_hash is not verified")
    require("HashExecutingAssembly()" in startup, "executing DLL hash is not verified")
    require(
        'BindingSchema = "oni.runtime-startup-binding.v2"' in startup,
        "C# does not require startup binding v2",
    )
    require(
        '"^rtb-[0-9a-f]{24}$"' in startup
        and "RuntimeBootstrapTransactionPattern.IsMatch" in startup,
        "C# does not validate the runtime bootstrap transaction id",
    )
    for variable in {
        "runtimeBootstrapResultPathIdLower",
        "runtimeBootstrapResultSha256",
        "runtimeBootstrapResultHash",
        "runtimeBootstrapTransactionId",
        "installedProbeTreeContentHash",
    }:
        require(
            f"{variable} = candidate" in startup,
            f"C# does not freeze v2 field: {variable}",
        )
        require(
            f"{variable} = null" in startup,
            f"C# does not clear v2 field after failure: {variable}",
        )
    require("ProbeSession.GameBuild" in startup, "game build is not verified")
    require("Mod.ModVersion" in startup, "mod version is not verified")
    for category in sorted(
        {
            "missing",
            "invalid",
            "request_path_mismatch",
            "request_hash_mismatch",
            "request_length_mismatch",
            "request_missing",
            "active_path_mismatch",
            "active_hash_mismatch",
            "save_transition_authorization_invalid",
            "save_transition_authorization_nested",
            "save_transition_authorization_replay",
            "save_transition_target_mismatch",
            "save_transition_authorization_clear_mismatch",
        }
    ):
        require(f'"{category}"' in startup, f"stable category is absent: {category}")

    on_load = method_body(mod, "public override void OnLoad")
    require(
        on_load.index("ProbeWriter.Initialize()")
        < on_load.index("StartupBinding.Initialize")
        < on_load.index("ProbeSession.InitializeProcess()")
        < on_load.index('ProbeWriter.WriteEvent("probe_loaded"'),
        "OnLoad binding order is wrong",
    )

    initialize_writer = method_body(writer, "internal static void Initialize()")
    require('"commands"' not in initialize_writer, "invalid startup pre-creates/touches command queues")
    require('"validation-controls"' not in initialize_writer, "invalid startup touches control queues")

    prepare = method_body(session, "internal static void PrepareSaveLoad")
    require(
        prepare.index("NormalizePath(path)")
        < prepare.index("HashPath(normalizedPath)")
        < prepare.index("TryReadSaveFingerprint")
        < prepare.index("StartupBinding.ObserveRequestedSave"),
        "request path/content fingerprint order is wrong",
    )
    start_session = method_body(session, "internal static void StartSaveSession")
    require("StartupBinding.ObserveActiveSave" in start_session, "active save is not bound")
    require("sequence = 0" in start_session, "save session sequence is not reset")
    require("StartupBinding.ObserveVerifiedSaveWrite" in session, "verified save writes do not advance content binding")
    observe_request = method_body(startup, "internal static void ObserveRequestedSave")
    observe_active = method_body(startup, "internal static void ObserveActiveSave")
    observe_write = method_body(startup, "internal static void ObserveVerifiedSaveWrite")
    authorize_transition = method_body(startup, "internal static void AuthorizeSavePathTransition")
    clear_transition = method_body(startup, "internal static void ClearAuthorizedSavePathTransition")
    for body, label in ((observe_request, "requested"), (observe_active, "active")):
        require(
            "sessionVerified" in body and "activeSavePathId" in body and "expectedRequestedPathId" in body,
            f"{label} observation does not distinguish initial loader from current active path",
        )
    require(
        "authorizedTransitionPathId" in observe_write
        and "activeSavePathId = observedActivePathId" in observe_write
        and "authorizedTransitionPathId = null" in observe_write,
        "verified save completion does not consume authorization and migrate active identity",
    )
    require(
        "!string.IsNullOrEmpty(authorizedTransitionPathId)" in authorize_transition
        and "string.Equals(targetPathId, activeSavePathId" in authorize_transition,
        "transition authorization does not reject nesting and replay",
    )
    require(
        "authorizedTransitionPathId = null" in clear_transition,
        "transition authorization cannot be cleared after save failure",
    )

    fingerprint = method_body(session, "private static bool TryReadSaveFingerprint(")
    fingerprint_once = method_body(session, "private static bool TryReadSaveFingerprintOnce(")
    require(
        fingerprint.count("TryReadSaveFingerprintOnce") == 2,
        "save fingerprint is not based on two independent reads",
    )
    for anchor in sorted(
        {
            "first.Length != second.Length",
            "first.CreationTimeUtcTicks != second.CreationTimeUtcTicks",
            "first.LastWriteTimeUtcTicks != second.LastWriteTimeUtcTicks",
            "first.Hash, second.Hash",
        }
    ):
        require(anchor in fingerprint, f"save fingerprint stability comparison misses {anchor}")
    require("FileShare.Read" in fingerprint_once, "save fingerprint does not deny concurrent writes")
    require("FileShare.ReadWrite" not in fingerprint_once, "save fingerprint still shares concurrent writes")
    require(
        fingerprint_once.count("CreationTimeUtc.Ticks") >= 2
        and fingerprint_once.count("LastWriteTimeUtc.Ticks") >= 2,
        "single fingerprint pass does not compare file identity timestamps",
    )

    ensure_started = method_body(runner, "public static void EnsureStarted")
    session_event = ensure_started.index('ProbeWriter.WriteEvent("session_started"')
    action_begin = ensure_started.index("ActionExecutor.BeginSession()")
    controller_begin = ensure_started.index("ValidationCheckpointController.BeginSession()")
    require(session_event < action_begin < controller_begin, "session_started is not first controller-visible record")
    require(
        'TrustedSessionStartReason = "save_loader_load_complete"' in runner
        and 'ProbeWriter.WriteEvent("session_start_unbound"' in ensure_started,
        "generic lifecycle start is not emitted as an explicit unbound event",
    )
    require(
        "trustedSessionStarted && StartupBinding.IsSessionVerified && !ProbeWriter.HasSafetyFault" in ensure_started,
        "controller start is not jointly gated by trusted reason, binding, and writer safety",
    )
    require(
        ensure_started.index("ProbeSession.StartSaveSession()") < session_event,
        "trusted session_started is not guaranteed to receive seq=1",
    )

    update = method_body(runner, "private void Update()")
    require(
        "trustedSessionStarted && StartupBinding.IsSessionVerified && !ProbeWriter.HasSafetyFault" in update,
        "controller updates are not jointly gated",
    )
    require(
        "if (ProbeWriter.HasSafetyFault)" in update and "StopControllers();" in update,
        "writer safety fault does not stop controllers on the next update",
    )
    stop_controllers = method_body(runner, "internal static void StopControllers()")
    require(
        "if (actionExecutorStarted)" in stop_controllers
        and "if (validationControllerStarted)" in stop_controllers,
        "controller EndSession calls are not start-gated",
    )
    require(
        stop_controllers.index("actionExecutorStarted = false")
        < stop_controllers.index("ActionExecutor.EndSession()")
        and stop_controllers.index("validationControllerStarted = false")
        < stop_controllers.index("ValidationCheckpointController.EndSession()"),
        "controller stop flags are not cleared before re-entrant shutdown work",
    )

    write_record = method_body(writer, "internal static void WriteRecord")
    reserve_slot = method_body(writer, "private static bool TryReserveQueueSlot")
    release_slot = method_body(writer, "private static void ReleaseQueueSlot")
    register_fault = method_body(writer, "private static long RegisterSafetyFault")
    attach_diagnostics = method_body(writer, "private static void AttachWriterDiagnostics")
    prepare_record = method_body(writer, "private static IDictionary<string, object> PrepareRecordForWrite")
    exception_error = method_body(writer, "internal static void WriteError(string stage, Exception ex)")
    require("MaxQueueDepth = 1000" in writer, "writer queue has no fixed total bound")
    require("Interlocked.CompareExchange" in reserve_slot, "queue slot reservation is not atomic")
    require(
        "Interlocked.CompareExchange" in release_slot and "observed <= 0" in release_slot,
        "queue slot release can underflow the total-count invariant",
    )
    require("PendingRecords.Count" not in writer, "writer still relies on racy queue Count")
    require(
        write_record.index("if (!TryReserveQueueSlot())") < write_record.index("if (isSnapshot)"),
        "total queue cap is applied only to snapshots",
    )
    require(
        'RegisterSafetyFault("queue_capacity_exceeded", true, true)' in write_record
        and "Interlocked.Increment(ref droppedSnapshotCount)" in write_record,
        "queue saturation does not distinguish droppable snapshots from critical safety faults",
    )
    require(
        "Interlocked.Increment(ref writerFaultCount)" in register_fault
        and "if (processSticky)" in register_fault
        and "Interlocked.Increment(ref processSafetyFaultCount)" in register_fault
        and "Interlocked.CompareExchange(ref processSafetyFaultCategory" in register_fault
        and "Interlocked.Exchange(ref safetyFault, 1)" in register_fault
        and "Debug.LogError" in register_fault
        and "ProbeRunner.StopControllers()" in register_fault,
        "critical queue saturation is not a process-sticky controller-stopping fault",
    )
    require("HasSafetyFault" in writer, "writer safety latch is not exposed to ProbeRunner")
    require(
        "if (IsSessionStarted(record) && !HasSafetyFault)" in prepare_record
        and "Interlocked.Exchange(ref writerFaultCount, 0L)" in prepare_record
        and "Interlocked.Exchange(ref nonFiniteNumericCount, 0L)" in prepare_record,
        "session-scoped validation diagnostics do not reset behind the process safety latch",
    )
    require(
        "processSafetyFaultCount" not in prepare_record
        and "processSafetyFaultCategory" not in prepare_record
        and "processFaultCount = Interlocked.Read(ref processSafetyFaultCount)" in attach_diagnostics
        and "processFaultCount + Interlocked.Read(ref writerFaultCount)" in attach_diagnostics
        and "Volatile.Read(ref processSafetyFaultCategory)" in attach_diagnostics,
        "a concurrent/new-session reset can erase process-sticky fault evidence",
    )
    require(
        'RegisterSafetyFault("record_validation_failed", false, false)' in prepare_record,
        "record validation failures incorrectly set the process safety latch",
    )
    require(
        'writerStatus["fault"]' in attach_diagnostics
        and "Volatile.Read(ref writerFaultCategory)" in attach_diagnostics,
        "recovered snapshots do not carry the sticky writer fault",
    )
    require(
        '"category", "exception"' in exception_error
        and '"error_type", StableExceptionType(ex)' in exception_error
        and "ex.ToString" not in exception_error
        and "ex.Message" not in exception_error,
        "exception writer payload can expose unstable messages or paths",
    )

    enrich = method_body(session, "internal static void EnrichRecord")
    writer_fallback = method_body(writer, "private static IDictionary<string, object> CreateWriterValidationError")
    for field in sorted(RECORD_FIELDS):
        require(f'"{field}"' in enrich, f"EnrichRecord misses {field}")
        require(f'"{field}"' in writer_fallback, f"writer fallback misses {field}")
    require(
        'if (!record.ContainsKey("requested_save_path_id"))' in enrich
        and 'if (!record.ContainsKey("active_save_path_id"))' in enrich,
        "EnrichRecord overwrites explicit save-completion path evidence",
    )

    save_finish = method_body(lifecycle, "private static void OnSaveFinished")
    require(
        save_finish.index("ProbeSession.CompleteSaveWrite")
        < save_finish.index('ProbeWriter.WriteEvent("save_write_completed"')
        and '{ "requested_save_path_id", completion.RequestedPathId }' in save_finish
        and '{ "result_save_path_id", completion.ResultPathId }' in save_finish
        and '{ "active_save_path_id", completion.ActivePathId }' in save_finish,
        "save completion does not publish the verified derivative path triplet",
    )

    execute_checkpoint = method_body(
        checkpoint_controller, "private static void ExecuteSaveCheckpoint"
    )
    require(
        execute_checkpoint.index("StartupBinding.AuthorizeSavePathTransition(targetPathId)")
        < execute_checkpoint.index("SaveLoader.Instance.Save(targetPath, false, true)")
        < execute_checkpoint.index("StartupBinding.ClearAuthorizedSavePathTransition(targetPathId)"),
        "Save-As authorization is not scoped immediately around SaveLoader.Save",
    )
    require(
        "finally" in execute_checkpoint
        and "transitionAuthorized" in execute_checkpoint,
        "Save-As authorization is not cleared on save exceptions/failures",
    )
    validate_control = method_body(checkpoint_controller, "private static string ValidateControl")
    for anchor in sorted(
        {
            'Path.GetExtension(targetPath), ".sav"',
            "Path.GetDirectoryName(targetPath), Path.GetDirectoryName(activePath)",
            "ProbeSession.PathsEqual(targetPath, activePath)",
            "File.Exists(targetPath)",
            'policy["allowed_target_save_path_ids"]',
        }
    ):
        require(anchor in validate_control, f"Save-As target authorization lacks policy gate: {anchor}")
    require("validationProcessId > 0" not in startup, "unexpected deferred PID check")
    require("if (processId <= 0)" in startup, "validation PID is not strictly positive")

    load_start = method_body(lifecycle, "private static void OnSaveLoadStarting")
    game_start = method_body(lifecycle, "private static void OnGameStarted")
    load_finish = method_body(lifecycle, "private static void OnSaveLoadFinished")
    require(
        load_start.index("saveLoadInProgress = true")
        < load_start.index("ProbeSession.PrepareSaveLoad"),
        "save-load nesting guard is armed after the request can start",
    )
    require(
        "if (saveLoadInProgress)" in game_start
        and game_start.index("if (saveLoadInProgress)")
        < game_start.index('ProbeRunner.EnsureStarted("game_lifecycle_patch")'),
        "generic game lifecycle can steal the completed-load session anchor",
    )
    require(
        'ProbeRunner.EnsureStarted("save_loader_load_complete")' in load_finish
        and "finally" in load_finish
        and load_finish.index('ProbeRunner.EnsureStarted("save_loader_load_complete")')
        < load_finish.index("saveLoadInProgress = false"),
        "save-load guard is not held through the completed-load session start",
    )

    verified_marker = method_body(startup, "private static void WriteVerifiedMarkerLocked")
    failure_marker = method_body(startup, "private static void WriteFailureMarkerLocked")
    for marker in (verified_marker, failure_marker):
        for forbidden in sorted(
            {
                "bindingPath",
                "bridgeDirectory",
                "RequestedPath",
                "ReadActiveSavePath",
                "Assembly.GetExecutingAssembly().Location",
            }
        ):
            require(forbidden not in marker, f"marker can expose raw path via {forbidden}")
    require("if (verifiedMarkerWritten)" in verified_marker, "verified marker is not single-shot")
    require("if (failureMarkerWritten)" in failure_marker, "failure marker is not single-shot")
    require("STARTUP_BINDING_VERIFIED" in verified_marker, "verified marker prefix is wrong")
    for field in sorted(
        {
            "validation_run_id",
            "startup_binding_hash",
            "startup_intent_hash",
            "validation_process_id",
            "requested_save_path_id",
            "active_save_path_id",
        }
    ):
        require(field in verified_marker, f"verified marker misses {field}")
    require("STARTUP_BINDING_FAILED" in failure_marker, "failure marker prefix is wrong")
    require("startup_binding_status" in failure_marker, "failure marker misses stable category")


def main() -> int:
    global ASSERTIONS
    ASSERTIONS = 0
    verify_behavior()
    verify_csharp_contract()
    if ASSERTIONS != EXPECTED_ASSERTION_COUNT:
        raise AssertionError(
            "assertion count contract changed: "
            f"expected {EXPECTED_ASSERTION_COUNT}, observed {ASSERTIONS}"
        )
    print("probe startup binding verification: PASS")
    print("checks: strict JSON, exact fields, hashes, game/mod/DLL, request/active binding")
    print("checks: sticky failures, same-path verified save/reload, completed-load session gating, seq/controller gating, record fields, markers")
    print(f"scenarios: 2; assertions: {ASSERTIONS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
