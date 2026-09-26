# B18 `_safe_text` fast-path

## Scope

This change preserves the existing normalization, redaction, slicing, and
exception order in `agent/strategy.py::_safe_text`. It only avoids calling the
redaction substitution when the actual `_API_SECRET_RE` has no match:

```python
if _API_SECRET_RE.search(text) is not None:
    text = _API_SECRET_RE.sub("[redacted]", text)
```

No regex body, function signature, default limit, other function, safety gate,
fixture, Mod, save, queue, or runtime protocol was changed.

## Verifier and evidence

`scripts/verify_strategy_safe_text_fastpath.py` parses the checked preimage and
current source, extracts only `_API_SECRET_RE` and `_safe_text` for controlled
`re`/`typing.Any` compilation, and never imports the production module. Its AST
gate requires the regex definition and every node outside `_safe_text` to match,
and requires the exact guard transformation inside `_safe_text`.

The pure-function differential includes primitive values, CRLF/Unicode,
no-match and actual-regex synthetic positive controls, multiple matches, long
text, all requested limit categories, and custom `__bool__`, `__str__`, and
`__index__` exceptions. Reports contain case names and non-sensitive summaries,
not input text or secret-shaped values. The report records started/finished
timestamps and before/after SHA256 values for strategy, verifier, and preimage.

Run boundary:

```text
pure-function-only; no production-module import, game, Steam, Bridge, replay,
benchmark, or real queue
```

The B18 differential is not the formal Bridge performance gate. The previously
observed `10000initial replay8.082s` result remains unchanged and is not claimed
as passed here. Live game admission, end-to-end replay, deployment, and visible
runtime performance remain unverified.
