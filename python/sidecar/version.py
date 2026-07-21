"""Sidecar ↔ UI protocol version.

`PROTOCOL_VERSION` is the contract version for the NDJSON request/response
shape implemented in this directory's `main.py` and mirrored in
`src/sidecar/contract.ts`. The UI sends a `handshake` request once on spawn and
compares the sidecar's reported version against its own `PROTOCOL_VERSION`
(contract.ts); a mismatch means the app shell and its bundled analyzer came
from different builds (e.g. a partial update), so the UI fails loudly instead
of letting one side silently mis-parse the other's payloads.

Bump this **and** the `PROTOCOL_VERSION` constant in `src/sidecar/contract.ts`
together, but only on an *incompatible* wire-shape change — renaming/removing a
field every client reads, changing a request's required args, or altering the
response envelope. Additive, pass-through data (a new job's aspect states /
comparisons, which the camelizer forwards verbatim) does NOT need a bump.
"""
from __future__ import annotations

PROTOCOL_VERSION = 1
