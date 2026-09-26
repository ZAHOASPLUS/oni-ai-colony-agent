"""Verify B18 _safe_text without importing agent.strategy."""

from __future__ import annotations

import argparse
import ast
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / "agent" / "strategy.py"
REDACTED = "[redacted]"
EXPECTED_CASE_NAMES = (
    "none-default", "false-default", "true-default", "zero-int-default",
    "integer-negative-default", "integer-positive-default", "float-default",
    "empty-string-default", "whitespace-crlf-unicode", "string-no-match",
    "string-single-hit", "string-multiple-hit", "string-long-no-match",
    "string-long-hit", "limit-zero-hit", "limit-negative-hit",
    "limit-greater-than-text", "limit-index-normal", "limit-illegal-float",
    "limit-illegal-string", "limit-illegal-none", "bool-exception",
    "str-exception", "index-exception",
)
NO_LIMIT = object()


class VerificationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def message(error: BaseException) -> str:
    try:
        return str(error)
    except BaseException:
        return "<unprintable exception>"


def error_summary(error: BaseException) -> dict[str, object]:
    text = message(error)
    return {"type": type(error).__name__,
            "message_sha256": hashlib.sha256(text.encode("utf-8", "backslashreplace")).hexdigest()}


def dump(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def read_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def top(module: ast.Module, predicate: Callable[[ast.AST], bool], label: str) -> ast.AST:
    found = [node for node in module.body if predicate(node)]
    if len(found) != 1:
        raise VerificationError(f"expected one top-level {label}")
    return copy.deepcopy(found[0])


def assignment(module: ast.Module, name: str) -> ast.Assign:
    node = top(module, lambda n: isinstance(n, ast.Assign) and len(n.targets) == 1
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name, f"assignment for {name}")
    return node  # type: ignore[return-value]


def safe_text(module: ast.Module) -> ast.FunctionDef:
    node = top(module, lambda n: isinstance(n, ast.FunctionDef) and n.name == "_safe_text", "_safe_text function")
    if node.decorator_list:  # type: ignore[union-attr]
        raise VerificationError("decorated _safe_text is outside the allowed subset")
    return node  # type: ignore[return-value]


def without_safe_text(module: ast.Module) -> ast.Module:
    clone = copy.deepcopy(module)
    clone.body = [n for n in clone.body if not (isinstance(n, ast.FunctionDef) and n.name == "_safe_text")]
    return clone


def is_redaction(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return False
    target, call = node.targets[0], node.value
    if not (isinstance(target, ast.Name) and target.id == "text"):
        return False
    if not (isinstance(call, ast.Call) and not call.keywords and len(call.args) == 2):
        return False
    function = call.func
    return (isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
            and function.value.id == "_API_SECRET_RE" and function.attr == "sub"
            and isinstance(call.args[0], ast.Constant) and call.args[0].value == REDACTED
            and isinstance(call.args[1], ast.Name) and call.args[1].id == "text")


def expected_safe_text(preimage: ast.FunctionDef) -> ast.FunctionDef:
    matches = [(i, n) for i, n in enumerate(preimage.body) if is_redaction(n)]
    if len(matches) != 1:
        raise VerificationError("expected one canonical redaction statement")
    index, statement = matches[0]
    search = ast.Call(ast.Attribute(ast.Name("_API_SECRET_RE", ast.Load()), "search", ast.Load()),
                      [ast.Name("text", ast.Load())], [])
    guard = ast.If(ast.Compare(search, [ast.IsNot()], [ast.Constant(None)]), [copy.deepcopy(statement)], [])
    expected = copy.deepcopy(preimage)
    expected.body[index] = guard
    return expected


def compile_subset(module: ast.Module) -> tuple[Callable[..., str], re.Pattern[str]]:
    subset = ast.Module(body=[assignment(module, "_API_SECRET_RE"), safe_text(module)], type_ignores=[])
    ast.fix_missing_locations(subset)
    namespace: dict[str, object] = {"re": re, "Any": Any, "str": str, "__builtins__": {}}
    exec(compile(subset, "<strategy-safe-text-subset>", "exec"), namespace, namespace)
    pattern, function = namespace["_API_SECRET_RE"], namespace["_safe_text"]
    if not isinstance(pattern, re.Pattern) or not callable(function):
        raise VerificationError("controlled subset did not produce expected objects")
    return function, pattern


class Case:
    def __init__(self, name: str, value_factory: Callable[[], object],
                 limit_factory: Callable[[], object] | None = None, must_redact: bool = False) -> None:
        self.name, self.value_factory, self.limit_factory, self.must_redact = name, value_factory, limit_factory, must_redact

    def build(self) -> tuple[tuple[object], dict[str, object]]:
        kwargs = {} if self.limit_factory is None else {"limit": self.limit_factory()}
        return (self.value_factory(),), kwargs


def fresh(value: object) -> Callable[[], object]:
    if isinstance(value, str):
        return lambda value=value: value.encode("utf-8").decode("utf-8")
    return lambda value=value: value


def make_case(name: str, value: object, limit: object = NO_LIMIT, must_redact: bool = False) -> Case:
    value_factory = value if callable(value) else fresh(value)
    limit_factory = None if limit is NO_LIMIT else (limit if callable(limit) else fresh(limit))
    return Case(name, value_factory, limit_factory, must_redact)


class BoolFailure:
    def __bool__(self) -> bool:
        raise RuntimeError("synthetic __bool__ failure")


class StrFailure:
    def __str__(self) -> str:
        raise RuntimeError("synthetic __str__ failure")


class IndexFailure:
    def __index__(self) -> int:
        raise RuntimeError("synthetic __index__ failure")


class IndexFour:
    def __index__(self) -> int:
        return 4


def build_cases() -> list[Case]:
    hit, multiple = "token:dummy_secret_1234", "sk-ABCDEFGH api_key=DummyValue123 secret:Another_1234"
    long_hit = hit + " " + "z" * 1200
    cases = [
        make_case("none-default", None), make_case("false-default", False), make_case("true-default", True),
        make_case("zero-int-default", 0), make_case("integer-negative-default", -17),
        make_case("integer-positive-default", 42), make_case("float-default", 3.25),
        make_case("empty-string-default", ""), make_case("whitespace-crlf-unicode", " \r\n 你好\t世界 \n"),
        make_case("string-no-match", "ordinary diagnostic text"),
        make_case("string-single-hit", hit, must_redact=True),
        make_case("string-multiple-hit", multiple, must_redact=True),
        make_case("string-long-no-match", "x" * 1400), make_case("string-long-hit", long_hit, must_redact=True),
        make_case("limit-zero-hit", hit, 0), make_case("limit-negative-hit", hit, -1),
        make_case("limit-greater-than-text", "short text", 9999), make_case("limit-index-normal", "abcdefgh", lambda: IndexFour()),
        make_case("limit-illegal-float", "text", 1.5), make_case("limit-illegal-string", "text", "2"),
        make_case("limit-illegal-none", "text", None), make_case("bool-exception", lambda: BoolFailure()),
        make_case("str-exception", lambda: StrFailure()), make_case("index-exception", "text", lambda: IndexFailure()),
    ]
    if tuple(case.name for case in cases) != EXPECTED_CASE_NAMES:
        raise VerificationError("case definition drifted from expected case set")
    return cases


def capture(function: Callable[..., str], args: tuple[object], kwargs: dict[str, object]) -> tuple[str, object]:
    try:
        return "return", function(*args, **kwargs)
    except BaseException as error:
        return "exception", (type(error), message(error))


def same_outcome(left: tuple[str, object], right: tuple[str, object]) -> bool:
    if left[0] != right[0]:
        return False
    if left[0] == "return":
        return type(left[1]) is type(right[1]) and left[1] == right[1]
    left_type, left_message = left[1]
    right_type, right_message = right[1]
    return left_type is right_type and left_message == right_message


def outcome_summary(outcome: tuple[str, object]) -> dict[str, object]:
    if outcome[0] == "return":
        value = outcome[1]
        return {"kind": "return", "type": type(value).__name__, "length": len(value) if isinstance(value, str) else None}
    error_type, text = outcome[1]
    return {"kind": "exception", "type": error_type.__name__, "message_length": len(text),
            "message_sha256": hashlib.sha256(text.encode("utf-8", "backslashreplace")).hexdigest()}


def run_case(case: Case, old: Callable[..., str], new: Callable[..., str]) -> dict[str, object]:
    try:
        old_args, old_kwargs, new_args, new_kwargs = (*case.build(), *case.build())
    except BaseException as error:
        return {"name": case.name, "status": "failed", "reason": "input_generation_error", "error": error_summary(error)}
    old_outcome, new_outcome = capture(old, old_args, old_kwargs), capture(new, new_args, new_kwargs)
    matched = same_outcome(old_outcome, new_outcome)
    positive = (not case.must_redact or all(outcome[0] == "return" and isinstance(outcome[1], str)
                                              and REDACTED in outcome[1] for outcome in (old_outcome, new_outcome)))
    return {"name": case.name, "status": "passed" if matched and positive else "failed",
            "comparison": "matched" if matched else "different", "positive_control": "passed" if positive else "failed",
            "old": outcome_summary(old_outcome), "new": outcome_summary(new_outcome)}


def make_report_dir(requested: str | None) -> Path:
    if requested:
        path = Path(requested).resolve()
        if not path.is_dir() or not path.name.startswith("b18-safe-text-fastpath-"):
            raise VerificationError("--report-dir must be an existing unique B18 directory")
        if (path / "report.json").exists():
            raise VerificationError("report.json exists; refusing to overwrite it")
        return path
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    while True:
        path = reports / (f"b18-safe-text-fastpath-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-pid{os.getpid()}-{secrets.token_hex(6)}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preimage", required=True, type=Path)
    parser.add_argument("--expected-preimage-sha256", required=True)
    parser.add_argument("--report-dir", type=str)
    return parser.parse_args()


def main() -> int:
    report_dir: Path | None = None
    preimage: Path | None = None
    before: dict[str, str] = {}
    report: dict[str, object] = {"schema": "b18-safe-text-fastpath-v1", "status": "failed",
        "scope": "pure-function-only; no production-module import, game, Bridge, replay, benchmark, or real queue"}
    started_at, timer = now(), time.perf_counter()
    try:
        args = parse_args()
        report_dir, preimage = make_report_dir(args.report_dir), args.preimage.resolve()
        expected = args.expected_preimage_sha256.strip().upper()
        if not re.fullmatch(r"[0-9A-F]{64}", expected):
            raise VerificationError("expected preimage SHA256 must be 64 hexadecimal characters")
        report["paths"] = {"strategy": str(STRATEGY), "preimage": str(preimage), "report_dir": str(report_dir)}
        before = {"strategy": sha256(STRATEGY), "preimage": sha256(preimage), "verifier": sha256(Path(__file__).resolve())}
        report.update({"hashes_before": before, "expected_preimage_sha256": expected,
                       "preimage_hash_match": before["preimage"] == expected, "differential_started": False})
        if not report["preimage_hash_match"]:
            raise VerificationError("preimage hash mismatch; differential was not executed")
        pre_module, cur_module = read_ast(preimage), read_ast(STRATEGY)
        pre_assignment, cur_assignment = assignment(pre_module, "_API_SECRET_RE"), assignment(cur_module, "_API_SECRET_RE")
        pre_safe, cur_safe = safe_text(pre_module), safe_text(cur_module)
        checks = {"regex_definition_same": dump(pre_assignment) == dump(cur_assignment),
                  "outside_safe_text_same": dump(without_safe_text(pre_module)) == dump(without_safe_text(cur_module)),
                  "exact_guard_diff": dump(cur_safe) == dump(expected_safe_text(pre_safe))}
        report["ast_checks"] = checks
        if not all(checks.values()):
            raise VerificationError("AST gate failed; diff is outside the allowed guard")
        old, old_re = compile_subset(pre_module)
        new, new_re = compile_subset(cur_module)
        if (old_re.pattern, old_re.flags) != (new_re.pattern, new_re.flags):
            raise VerificationError("compiled regex definition changed")
        cases, positives = build_cases(), []
        positives = [case for case in cases if case.must_redact]
        positive_count = sum(old_re.search(str(case.value_factory())) is not None for case in positives)
        if positive_count != len(positives) or positive_count == 0:
            raise VerificationError("synthetic positive control did not hit actual regex")
        report["differential_started"] = True
        results = [run_case(case, old, new) for case in cases]
        passed, assertions = sum(r["status"] == "passed" for r in results), len(results) + positive_count
        report.update({"expected_case_count": len(EXPECTED_CASE_NAMES), "actual_case_count": len(results),
                       "expected_case_names": list(EXPECTED_CASE_NAMES), "passed_case_count": passed,
                       "assertion_count": assertions, "positive_control_count": positive_count, "case_results": results})
        if ([r["name"] for r in results] != list(EXPECTED_CASE_NAMES) or len(results) != len(EXPECTED_CASE_NAMES)
                or passed != len(EXPECTED_CASE_NAMES) or assertions == 0):
            raise VerificationError("case count, case result, or assertion gate failed")
    except BaseException as error:
        report["error"] = error_summary(error)
    report.update({"started_at": started_at, "finished_at": now(), "duration_ms": round((time.perf_counter() - timer) * 1000, 3)})
    if before and preimage is not None:
        try:
            after = {"strategy": sha256(STRATEGY), "preimage": sha256(preimage), "verifier": sha256(Path(__file__).resolve())}
            report["hashes_after"], report["hashes_stable"] = after, after == before
        except BaseException as error:
            report["hashes_after_error"], report["hashes_stable"] = error_summary(error), False
    report["status"] = "passed" if report.get("hashes_stable", False) and "error" not in report else "failed"
    try:
        if report_dir is None:
            report_dir = make_report_dir(None)
        path = report_dir / "report.json"
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        print(json.dumps({"status": report["status"], "report": str(path)}, ensure_ascii=False))
    except BaseException as error:
        print(f"report write failed: {type(error).__name__}: {hashlib.sha256(message(error).encode()).hexdigest()}", file=sys.stderr)
        return 1
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
