"""A searchable catalog of every symbol KiCad installs, built once and cached.

Parsing all 224 ``.kicad_sym`` files takes about 45 s (measured on KiCad 10.0.6:
22,860 symbols), so the catalog is written once to a gzip'd JSON file keyed by
the library directory and its newest modification time, and loaded from there
on every later call. Nothing here reaches the network.

Each :class:`SymbolEntry` carries what a design needs and a model should never
have to invent: the ``Lib:Name`` id, the description and keywords, the default
footprint, the footprint filters, the datasheet link, and every pin as
``(number, name, electrical type)``. A derived symbol (``(extends "AP1117-15")``
-- the AMS1117-3.3 is one) has no pins of its own in the file; KiCad resolves
them from the parent in the same library, and so does this.

Search is deterministic token scoring, not embeddings: part numbers are exact
strings ("AMS1117-3.3", "ESP32-WROOM-32E"), and an engineer searching for one
wants that part first, not its nearest neighbour in meaning.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import symbol_dir

__all__ = ["LibraryIndex", "SymbolEntry", "build_index", "load_index"]

#: Bumped when the cached format changes, so an old cache is rebuilt.
INDEX_VERSION = 1


@dataclass(frozen=True)
class SymbolEntry:
    lib_id: str
    description: str = ""
    keywords: str = ""
    footprint: str = ""
    footprint_filters: str = ""
    datasheet: str = ""
    #: ``(number, name, electrical type)`` for every pin across every unit,
    #: de-duplicated (a multi-unit part repeats its power pins per unit).
    pins: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return self.lib_id.split(":", 1)[-1]

    @property
    def library(self) -> str:
        return self.lib_id.split(":", 1)[0]


def _cache_dir() -> Path:
    configured = os.environ.get("SILKSCREEN_CACHE_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".cache" / "silkscreen"


def _fingerprint(root: Path) -> str:
    files = sorted(root.glob("*.kicad_sym"))
    newest = max((f.stat().st_mtime for f in files), default=0.0)
    key = f"{INDEX_VERSION}|{root.resolve()}|{len(files)}|{newest:.0f}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _pins_of(symbol) -> list[tuple[str, str, str]]:
    seen: dict[str, tuple[str, str, str]] = {}
    for unit in list(symbol.units) + [symbol]:
        for pin in getattr(unit, "pins", []) or []:
            number = str(pin.number)
            if number not in seen:
                seen[number] = (number, str(pin.name), str(pin.electricalType))
    return list(seen.values())


def _parse_library(path: Path) -> list[SymbolEntry]:
    from kiutils.symbol import SymbolLib

    lib = SymbolLib().from_file(str(path))
    by_name = {s.entryName: s for s in lib.symbols}
    entries: list[SymbolEntry] = []
    for symbol in lib.symbols:
        props = {p.key: p.value for p in symbol.properties}
        pins = _pins_of(symbol)
        parent = symbol
        # Derived symbols inherit pins (and unset fields) from their parent,
        # which KiCad only allows within the same library. Bounded, in case a
        # malformed file makes a cycle.
        for _ in range(8):
            if pins or not parent.extends or parent.extends not in by_name:
                break
            parent = by_name[parent.extends]
            pins = _pins_of(parent)
            parent_props = {p.key: p.value for p in parent.properties}
            for key, value in parent_props.items():
                props.setdefault(key, value)
        entries.append(
            SymbolEntry(
                lib_id=f"{path.stem}:{symbol.entryName}",
                description=props.get("Description", ""),
                keywords=props.get("ki_keywords", ""),
                footprint=props.get("Footprint", ""),
                footprint_filters=props.get("ki_fp_filters", ""),
                datasheet=props.get("Datasheet", ""),
                pins=tuple(pins),
            )
        )
    return entries


def build_index(root: Path) -> list[SymbolEntry]:
    """Parse every library under ``root``. Slow; :func:`load_index` caches it."""
    entries: list[SymbolEntry] = []
    for path in sorted(root.glob("*.kicad_sym")):
        try:
            entries.extend(_parse_library(path))
        except Exception:  # noqa: BLE001 - one unreadable library is skipped
            continue
    return entries


_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class LibraryIndex:
    """The catalog, with exact lookup and ranked search."""

    def __init__(self, entries: list[SymbolEntry], root: Path | None = None):
        self.entries = entries
        self.root = root
        self._by_id = {e.lib_id: e for e in entries}
        self._by_name: dict[str, list[SymbolEntry]] = {}
        #: Names with ST/Microchip-style ``x`` placeholders ("STM32F103C8Tx")
        #: as patterns, so "STM32F103C8T6" finds its symbol. Only an ``x``
        #: that follows a digit or capital counts: "Box" is not a wildcard.
        self._patterns: list[tuple[re.Pattern[str], SymbolEntry]] = []
        for entry in entries:
            self._by_name.setdefault(entry.name.lower(), []).append(entry)
            if re.search(r"(?<=[0-9A-Z])x", entry.name):
                wild = re.sub(
                    r"(?<=[0-9A-Z])x", ".", re.escape(entry.name).replace(r"\-", "-")
                )
                self._patterns.append((re.compile(wild, re.IGNORECASE), entry))

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, lib_id: str) -> SymbolEntry | None:
        return self._by_id.get(lib_id)

    def by_name(self, name: str) -> list[SymbolEntry]:
        """Every symbol whose name is exactly ``name`` (case-insensitive)."""
        return list(self._by_name.get(name.strip().lower(), []))

    def search(self, query: str, limit: int = 10) -> list[SymbolEntry]:
        """Symbols ranked for ``query``: exact name, then name prefix, then
        token overlap with name, keywords and description. Ties break on the
        id, so the order is stable."""
        q = query.strip().lower()
        if not q:
            return []
        qt = _tokens(q)
        wild = {
            entry.lib_id
            for pattern, entry in self._patterns
            if pattern.fullmatch(query.strip())
        }
        scored: list[tuple[float, str, SymbolEntry]] = []
        for entry in self.entries:
            name = entry.name.lower()
            score = 0.0
            if name == q:
                score += 100
            elif entry.lib_id in wild:
                score += 90
            elif name.startswith(q) or q.startswith(name):
                score += 40
            elif q in name:
                score += 20
            name_t = _tokens(name)
            text_t = _tokens(f"{entry.keywords} {entry.description}")
            score += 6 * len(qt & name_t) + 2 * len(qt & text_t)
            if score > 0:
                scored.append((-score, entry.lib_id, entry))
        scored.sort()
        return [entry for _, _, entry in scored[:limit]]


def load_index(
    root: Path | None = None, *, rebuild: bool = False
) -> LibraryIndex | None:
    """The cached catalog for the installed library, building it on first use.

    None when no KiCad symbol library is installed -- stated, never an empty
    index that would read as "no such part exists".
    """
    root = root or symbol_dir()
    if root is None or not root.is_dir():
        return None
    cache = _cache_dir() / f"kicadlib-symbols-{_fingerprint(root)}.json.gz"
    if cache.is_file() and not rebuild:
        try:
            with gzip.open(cache, "rt", encoding="utf-8") as fh:
                raw = json.load(fh)
            entries = [
                SymbolEntry(**{**e, "pins": tuple(tuple(p) for p in e["pins"])})
                for e in raw["entries"]
            ]
            return LibraryIndex(entries, root)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # a damaged cache is rebuilt, not trusted
    entries = build_index(root)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            payload = {
                "version": INDEX_VERSION,
                "entries": [asdict(e) for e in entries],
            }
            json.dump(payload, fh)
        tmp.replace(cache)
    except OSError:
        pass  # an unwritable cache costs speed next time, not correctness
    return LibraryIndex(entries, root)
