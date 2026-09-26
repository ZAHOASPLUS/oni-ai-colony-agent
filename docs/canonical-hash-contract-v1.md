# ONI typed canonical SHA-256 v1

Contract identifier: `oni.typed-canonical-sha256.v1`

This contract hashes a typed tree, not rendered JSON. Its SHA-256 input starts
with the ASCII contract identifier followed by one zero byte, then exactly one
value encoded by the grammar below. Digest strings retain the external form
`sha256:<64 lowercase hexadecimal digits>`.

| Value | Encoding |
| --- | --- |
| null | ASCII `n` |
| false / true | ASCII `b0` / `b1` |
| signed 64-bit integer | ASCII `i`, normalized base-10 integer, ASCII `;` |
| finite binary64 float | ASCII `f`, its 16 lowercase big-endian IEEE-754 hex digits, ASCII `;` |
| string | ASCII `s`, UTF-8 byte length in base 10, ASCII `:`, strict UTF-8 bytes |
| array | ASCII `a`, item count in base 10, ASCII `:`, encoded items in input order |
| object | ASCII `o`, property count in base 10, ASCII `:`, encoded key strings and values |

Object keys must be strings, must be unique after JSON escape decoding, and are
ordered by unsigned lexicographic comparison of their strict UTF-8 bytes. No
Unicode normalization is performed. This makes control characters, U+2028, and
non-BMP scalars unambiguous without relying on a JSON escape policy. Unpaired
surrogates are rejected.

Integers and floats remain different typed values. The v1 integer domain is the
closed signed 64-bit range; larger values are rejected instead of being rounded
or accepted differently by the two runtimes. This range includes exact values
on both sides of the 53-bit binary64 boundary. Floats are binary64; the raw-bit
rule preserves negative zero and avoids exponent-rendering differences. NaN and
both infinities are rejected. Objects with duplicate decoded property names are
rejected before a JSON object model is created, including aliases such as `a`
and `\u0061`.

The contract is used without projection changes for `command_intent_hash`,
`policy_hash`, and `receipt_hash`. The command-intent field projection and the
receipt rule of omitting only `receipt_hash` remain unchanged.
