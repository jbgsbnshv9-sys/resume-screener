"""Content-addressed blob storage on local disk (original documents and rendered PDFs).

Encryption at rest is provided by the volume in v1 (see SPEC.md, Security);
this module never logs or copies document content anywhere else.
"""

from __future__ import annotations

from pathlib import Path

from screener import settings
from screener.hashing import sha256_bytes


def _path(ref: str, base: Path | None = None) -> Path:
    if not ref.startswith("sha256:"):
        raise ValueError("unknown blob ref")
    digest = ref.removeprefix("sha256:")
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        raise ValueError("malformed blob ref")
    return (base or settings.blob_dir()) / digest[:2] / digest


def put(data: bytes, base: Path | None = None) -> str:
    ref = f"sha256:{sha256_bytes(data)}"
    path = _path(ref, base)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return ref


def get(ref: str, base: Path | None = None) -> bytes:
    return _path(ref, base).read_bytes()


def delete(ref: str, base: Path | None = None) -> None:
    _path(ref, base).unlink(missing_ok=True)
