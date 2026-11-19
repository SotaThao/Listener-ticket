"""Canonical assistant-triage pack, version 1.

The LF-only header starts with ``<!-- assistant-triage pack v1``, followed by
``pack_fingerprint: <hex>``, then ``source <name> sha256 <hex>`` for principles
and every rule in loading order, and ``-->``. Each body is enclosed by lines
``<!-- source: <name> -->`` and ``<!-- end-source: <name> -->``; one framing
newline precedes the closing marker and is not part of the source text.
These marker prefixes are reserved in source text. Source hashes cover UTF-8
text after removing a leading BOM and replacing CRLF/CR with LF, preserving
all other bytes, including trailing newlines. The pack fingerprint is SHA-256
of the entire rendered UTF-8 text with ONLY the header fingerprint value
replaced by 64 ASCII zeros. The pack always ends with a newline.
Pack input also strips a leading BOM and normalizes CRLF/CR to LF before
validation; Pack.text contains that canonical LF form.

Importing this module does not read files. Pack.sources maps names to hashes.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from .decision_rules import FILES
from .rules_extensions import ExtensionRules

if TYPE_CHECKING:
    from .assistant_core import CorePack

SOURCES: tuple[str, ...] = tuple(f"{name}.md" for name in (*FILES, *ExtensionRules.FILES))
PRINCIPLES_FILE = "principles.md"
PACK_FILE = "assistant-triage.md"
CORE_FILE = "assistant-core.json"
_START = "<!-- assistant-triage pack v1\npack_fingerprint: "
_ZEROS = "0" * 64
_HEX = re.compile(r"[0-9a-f]{64}")
_RESERVED = re.compile(r"^<!-- (?:source:|end-source:)", re.M)


class AssistantPackError(ValueError):
    """Missing, malformed, unreadable or stale assistant pack/source."""


def _normalize(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _hash(text: str) -> str:
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise AssistantPackError(f"invalid UTF-8 text: {exc}") from exc


def render(principles_text: str, sources: Sequence[tuple[str, str]]) -> str:
    """Render all required sources in loading order, deterministically."""
    if tuple(name for name, _ in sources) != SOURCES:
        raise AssistantPackError("source names must match SOURCES in loading order")
    entries = [(PRINCIPLES_FILE, _normalize(principles_text))] + [
        (name, _normalize(body)) for name, body in sources
    ]
    if any(_RESERVED.search(body) for _, body in entries):
        raise AssistantPackError("source text contains a reserved section marker")
    text = _START + _ZEROS + "\n"
    text += "".join(f"source {name} sha256 {_hash(body)}\n" for name, body in entries)
    text += "-->\n"
    text += "".join(
        f"<!-- source: {name} -->\n{body}\n<!-- end-source: {name} -->\n"
        for name, body in entries
    )
    return text.replace(_START + _ZEROS, _START + _hash(text), 1)


@dataclass(frozen=True)
class Pack:
    fingerprint: str
    sources: Mapping[str, str]
    text: str
    bodies: Mapping[str, str]
    core: CorePack | None = None


def parse(text: str) -> Pack:
    """Normalize input, then validate its header, bodies, hashes and fingerprint."""
    text = _normalize(text)
    if not text.startswith(_START):
        raise AssistantPackError("malformed pack header")
    fingerprint, sep, rest = text[len(_START):].partition("\n")
    if not sep or not _HEX.fullmatch(fingerprint):
        raise AssistantPackError("malformed pack fingerprint")
    hashes = {}
    for name in (PRINCIPLES_FILE, *SOURCES):
        line, sep, rest = rest.partition("\n")
        prefix = f"source {name} sha256 "
        if not sep or not line.startswith(prefix) or not _HEX.fullmatch(line[len(prefix):]):
            raise AssistantPackError(f"missing, extra, unordered or malformed source: {name}")
        hashes[name] = line[len(prefix):]
    if not rest.startswith("-->\n"):
        raise AssistantPackError("malformed header terminator or extra source")
    rest = rest[len("-->\n"):]
    bodies = {}
    for name, expected in hashes.items():
        marker = f"<!-- source: {name} -->\n"
        if not rest.startswith(marker):
            raise AssistantPackError(f"missing or unordered section: {name}")
        body, sep, rest = rest[len(marker):].partition(f"\n<!-- end-source: {name} -->\n")
        if (not sep or _RESERVED.search(body) or _normalize(body) != body
                or _hash(body) != expected):
            raise AssistantPackError(f"invalid section or source hash mismatch: {name}")
        bodies[name] = body
    if rest:
        raise AssistantPackError("extra pack content")
    zeroed = _START + _ZEROS + text[len(_START) + 64:]
    if _hash(zeroed) != fingerprint:
        raise AssistantPackError("pack fingerprint mismatch")
    return Pack(fingerprint, MappingProxyType(hashes), text, MappingProxyType(bodies))


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise AssistantPackError(f"{path}: cannot read UTF-8 file: {exc}") from exc


def load(directory: Path, rules_dir: Path, *, pack_mode: str = "full") -> Pack:
    """Read a pack and refuse any drift from the current normalized sources."""
    directory, rules_dir = Path(directory), Path(rules_dir)
    pack = parse(_read(directory / PACK_FILE))
    for name, expected in pack.sources.items():
        path = (directory if name == PRINCIPLES_FILE else rules_dir) / name
        if _hash(_normalize(_read(path))) != expected:
            raise AssistantPackError(f"{path}: source drift (rebuild assistant pack)")
    if pack_mode == "core":
        from .assistant_core import load_core
        return replace(pack, core=load_core(pack, directory / CORE_FILE, rules_dir))
    return pack
