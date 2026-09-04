#!/usr/bin/env python3
"""
EDSX — convert Odyssey Materials Helper (EDOMH) ship share links to EDSY
SLEF JSON.

Usage:
    ./edsx.py example-urls.txt
    ./edsx.py https://link.edomh.nl/f5aDKAAq
    ./edsx.py --refresh-data example-urls.txt
    ./edsx.py raw/*.edomh.json          # re-convert preserved payloads

Input:
    One EDOMH share URL per line, URLs directly on the command line, or
    previously-decoded .edomh.json payloads.

Output:
    ./slef/<ShipName>.slef.json
    ./raw/<ShipName> [UUID].edomh.json

Reference data:
    EDSY's current eddb.js is the primary authority. It is parsed
    structurally: module records, the blueprint/expeffect/mtype tables and
    the per-ship slot layouts are all read, not just fdname strings.

    FDevIDs outfitting.csv and shipyard.csv are cached locally and merged
    in as a secondary source.

    Both are cached under Documents/EDSX/cache, or under EDSX_HOME when
    that is set. Nothing is written beside the executable.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Iterator


# ===========================================================================
# URLs / paths
# ===========================================================================

APP_NAME = "EDSX"


def _windows_documents() -> Path:
    """
    Return the Documents folder, honouring a redirected location.

    Windows lets a user (or OneDrive) move Documents elsewhere, and the
    real location lives in the registry. Assuming ``~/Documents`` puts
    the cache somewhere the user will never find to delete it.
    """

    try:
        # Reached through importlib because the module does not exist off
        # Windows: a direct import breaks every other platform.
        import importlib

        winreg = importlib.import_module("winreg")
        key = (
            r"Software\Microsoft\Windows\CurrentVersion"
            r"\Explorer\User Shell Folders"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            raw, _ = winreg.QueryValueEx(handle, "Personal")
        expanded = os.path.expandvars(str(raw))
        if expanded:
            return Path(expanded)
    except (ImportError, OSError, ValueError, AttributeError):
        pass

    return Path.home() / "Documents"


def _linux_documents() -> Path:
    """Return the XDG documents directory, or ``~/Documents``."""

    configured = os.environ.get("XDG_DOCUMENTS_DIR")

    if configured:
        return Path(os.path.expandvars(configured)).expanduser()

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"

    try:
        for line in (base / "user-dirs.dirs").read_text(
            encoding="utf-8"
        ).splitlines():
            if not line.startswith("XDG_DOCUMENTS_DIR"):
                continue
            value = line.split("=", 1)[1].strip().strip('"')
            return Path(os.path.expandvars(value)).expanduser()
    except (OSError, IndexError):
        pass

    return Path.home() / "Documents"


def documents_dir() -> Path:
    """Return the user's Documents folder for this platform."""

    if sys.platform == "win32":
        return _windows_documents()

    if sys.platform == "darwin":
        return Path.home() / "Documents"

    return _linux_documents()


def app_root() -> Path:
    """
    Return the directory EDSX keeps its own files in.

    Writing beside the executable is not safe as a default: on Windows a
    binary in Program Files cannot write to its own folder, and on macOS
    writing inside a signed bundle breaks its signature. Documents is
    where the other ED tools put their per-user data, and it is somewhere
    the user can actually find the cache in order to delete it.

    ``EDSX_HOME`` overrides it, which makes tests hermetic and lets the
    cache sit on removable media beside a portable binary.
    """

    override = os.environ.get("EDSX_HOME")

    if override:
        return Path(override).expanduser()

    return documents_dir() / APP_NAME


_BASE_DIR = app_root()
DATA_DIR = _BASE_DIR / "cache"
EDSY_EDDB_CACHE = DATA_DIR / "eddb.js"
FDEVIDS_OUTFITTING_CACHE = DATA_DIR / "outfitting.csv"
FDEVIDS_SHIPYARD_CACHE = DATA_DIR / "shipyard.csv"
REFERENCE_META = DATA_DIR / "reference-meta.json"
RAW_DIR = _BASE_DIR / "raw"
OUTPUT_DIR = _BASE_DIR / "slef"
FAILURE_FILE = _BASE_DIR / "raw""edsx-failures.txt"

# ── Version ─────────────────────────────────────────────────────────────────
# The version string has exactly one home: the plain-text file named `version`
# at the repository root, next to this script. Nothing in this file carries a
# copy, so there is no constant to drift out of step with it and no
# cross-check for CI to make.
#
# Three layouts resolve:
#
#   source checkout   ./version sits next to edsx.py, so __file__'s parent is
#                     the root. Works from any working directory.
#   frozen binary     PyInstaller unpacks data files under sys._MEIPASS, and
#                     packaging/edsx.spec places `version` at the top of the
#                     bundle, so _MEIPASS is the root.
#   released script   the release workflow publishes edsx.py on its own, with
#                     no version file beside it, so it substitutes the real
#                     version for the placeholder below as it copies. That
#                     copy is a build artefact — the same thing the frozen
#                     binary does with its bundled copy — and what is
#                     committed here is always the placeholder.
_VERSION_STAMP = "@@EDSX_VERSION@@"


def _read_version() -> str:
    """Return the application version, read from <root>/version."""
    if not _VERSION_STAMP.startswith("@@"):
        return _VERSION_STAMP
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if bundle else Path(__file__).resolve().parent
    try:
        text = (root / "version").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text or "unknown"


VERSION = _read_version()
USER_AGENT = f"EDSX/{VERSION}"

EDSY_EDDB_URL = (
    "https://raw.githubusercontent.com/taleden/EDSY/master/eddb.js"
)

FDEVIDS_OUTFITTING_URL = (
    "https://raw.githubusercontent.com/EDCD/FDevIDs/master/outfitting.csv"
)

FDEVIDS_SHIPYARD_URL = (
    "https://raw.githubusercontent.com/EDCD/FDevIDs/master/shipyard.csv"
)


# ===========================================================================
# HTTP
# ===========================================================================

def make_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    request_headers = {"User-Agent": USER_AGENT}

    if headers:
        request_headers.update(headers)

    return urllib.request.Request(
        url,
        headers=request_headers,
        method=method,
    )


def http_get(url: str, timeout: int = 30) -> tuple[bytes, dict[str, str]]:
    req = make_request(url)

    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(), dict(response.headers.items())


def http_head(url: str, timeout: int = 20) -> dict[str, str]:
    req = make_request(url, method="HEAD")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        # Some raw GitHub endpoints don't like HEAD.
        if exc.code in (405, 501):
            return {}
        raise


def get_redirect_location(url: str) -> str:
    """
    urllib normally refuses to follow an unknown edomh:// scheme.

    Deliberately disable redirect handling and inspect Location.
    """

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirect)

    req = make_request(url)

    try:
        with opener.open(req, timeout=20):
            raise RuntimeError("EDOMH link unexpectedly returned HTTP 200")

    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise

        location = exc.headers.get("Location")

        if not location:
            raise RuntimeError(
                "EDOMH server returned a redirect without Location"
            )

        return location


# ===========================================================================
# Reference-data cache
# ===========================================================================

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_reference_meta() -> dict[str, Any]:
    if not REFERENCE_META.exists():
        return {}

    try:
        return json.loads(REFERENCE_META.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_reference_meta(meta: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    REFERENCE_META.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def reference_is_current(
    url: str,
    path: Path,
    meta: dict[str, Any],
) -> bool:
    """
    Determine whether the local reference file appears current.

    We deliberately don't require the server to support HEAD/ETag.
    If HEAD gives us useful metadata, compare it. Otherwise retain the
    local cache until --refresh-data is requested.
    """

    if not path.exists():
        return False

    entry = meta.get(url)

    if not isinstance(entry, dict):
        return False

    try:
        headers = http_head(url)
    except Exception:
        # Network unavailable: local cache is still useful.
        return True

    local_size = path.stat().st_size

    remote_size = headers.get("Content-Length")

    if remote_size:
        try:
            if int(remote_size) != local_size:
                return False
        except ValueError:
            pass

    remote_etag = headers.get("ETag")
    local_etag = entry.get("etag")

    if remote_etag and local_etag:
        return remote_etag == local_etag

    remote_last_modified = headers.get("Last-Modified")
    local_last_modified = entry.get("last_modified")

    if remote_last_modified and local_last_modified:
        return remote_last_modified == local_last_modified

    return True


def download_reference(
    url: str,
    path: Path,
    refresh: bool = False,
) -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    meta = load_reference_meta()

    if not refresh and reference_is_current(url, path, meta):
        print(f"Using cached reference data: {path}", file=sys.stderr)
        return path.read_bytes()

    print(f"Downloading {url} ...", file=sys.stderr)

    data, headers = http_get(url)

    path.write_bytes(data)

    meta[url] = {
        "url": url,
        "path": str(path),
        "downloaded_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
        "sha256": sha256_bytes(data),
        "size": len(data),
        "etag": headers.get("ETag"),
        "last_modified": headers.get("Last-Modified"),
    }

    save_reference_meta(meta)

    return data


# ===========================================================================
# Minimal JavaScript object-literal reader
#
# eddb.js is hand-maintained data, not arbitrary JS. We only need to walk
# balanced braces and read scalar fields, so a small reader is both
# sufficient and far more reliable than scraping bare fdname strings.
# ===========================================================================

def match_braces(text: str, open_pos: int) -> tuple[str, int]:
    """
    Return (block_including_braces, index_after_close) for the block whose
    opening brace is at open_pos. String literals are skipped so a brace
    inside a quoted value cannot unbalance the scan.
    """

    depth = 0
    i = open_pos
    length = len(text)

    while i < length:
        ch = text[i]

        if ch in "'\"":
            quote = ch
            i += 1
            while i < length:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
            i += 1
            continue

        if ch == "{":
            depth += 1

        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_pos:i + 1], i + 1

        i += 1

    raise ValueError("Unbalanced braces in reference data")


def line_is_commented(text: str, pos: int) -> bool:
    start = text.rfind("\n", 0, pos) + 1
    return text[start:pos].lstrip().startswith("//")


RECORD_START = re.compile(r"(?<![\w'\"])([A-Za-z0-9_]+)\s*:\s*\{")


def iter_records(body: str) -> Iterator[tuple[str, str]]:
    """
    Yield (key, inner_body) for every `key : { ... }` at the top level of
    the supplied block body. Nested blocks are skipped, and commented-out
    records are ignored.
    """

    i = 0

    while True:
        match = RECORD_START.search(body, i)

        if not match:
            return

        open_pos = match.end() - 1

        try:
            block, after = match_braces(body, open_pos)
        except ValueError:
            return

        if not line_is_commented(body, match.start()):
            yield match.group(1), block[1:-1]

        i = after


def js_string(body: str, field: str) -> str | None:
    match = re.search(
        rf"(?:^|[,{{\s]){re.escape(field)}\s*:\s*'((?:[^'\\]|\\.)*)'",
        body,
    )

    if not match:
        return None

    return (
        match.group(1)
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def js_number(body: str, field: str) -> float | None:
    match = re.search(
        rf"(?:^|[,{{\s]){re.escape(field)}\s*:\s*(-?\d+(?:\.\d+)?)",
        body,
    )

    if not match:
        return None

    return float(match.group(1))


def js_int(body: str, field: str) -> int | None:
    value = js_number(body, field)
    return None if value is None else int(value)


def js_string_array(body: str, field: str) -> list[str]:
    match = re.search(
        rf"(?:^|[,{{\s]){re.escape(field)}\s*:\s*\[([^\]]*)\]",
        body,
    )

    return re.findall(r"'([^']*)'", match.group(1)) if match else []


def js_int_array(body: str, field: str) -> list[int]:
    match = re.search(
        rf"(?:^|[,{{\s]){re.escape(field)}\s*:\s*\[([^\]]*)\]",
        body,
    )

    return [int(v) for v in re.findall(r"-?\d+", match.group(1))] if match \
        else []


# ===========================================================================
# Normalization
# ===========================================================================

def normalize_symbol(value: str) -> str:
    """
    Normalize Frontier/EDSY identifiers and display names for comparison.

    Punctuation, spacing and case are removed; nothing semantic is changed.
    """

    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


PAREN = re.compile(r"\s*\([^)]*\)")

#: Matches the hull-specific bulkhead symbols, e.g. Corsair_Armour_Grade3.
ARMOUR_SYMBOL = re.compile(
    r"_Armour_(?:Grade\d+|Mirrored|Reactive)$",
    re.I,
)


def strip_parens(name: str) -> str:
    """
    EDSY embeds detail in display names: 'Cargo Rack (Cap: 128)'.

    That detail is absent from EDOMH identifiers, so we index both the full
    name and this reduced form. The full form always ranks higher, which
    keeps 'Frame Shift Drive (SCO)' distinct from 'Frame Shift Drive'.
    """

    return PAREN.sub("", name or "").strip()


# ===========================================================================
# Reference records
# ===========================================================================

class RefModule:
    __slots__ = (
        "fdname", "name", "mtype", "cls", "rating",
        "mount", "cargocap", "source",
    )

    def __init__(
        self,
        fdname: str,
        name: str | None = None,
        mtype: str | None = None,
        cls: int | None = None,
        rating: str | None = None,
        mount: str | None = None,
        cargocap: float | None = None,
        source: str = "edsy",
    ) -> None:
        self.fdname = fdname
        self.name = name
        self.mtype = mtype
        self.cls = cls
        self.rating = rating
        self.mount = mount
        self.cargocap = cargocap
        self.source = source

    def __repr__(self) -> str:
        return f"<RefModule {self.fdname}>"


class RefShip:
    __slots__ = ("fdname", "name", "cost", "slots", "slotnames")

    def __init__(
        self,
        fdname: str,
        name: str | None,
        cost: int | None,
        slots: dict[str, list[int]],
        slotnames: dict[str, list[str]] | None = None,
    ) -> None:
        self.fdname = fdname
        self.name = name
        self.cost = cost
        self.slots = slots
        # EDSY overrides the generated slot names for hulls whose journal
        # names do not follow the usual pattern. Empty for most ships.
        self.slotnames = slotnames or {}


# ===========================================================================
# EDSY database
# ===========================================================================

class EdsyDatabase:
    """
    Structural view of EDSY's eddb.js.

    Everything the converter needs comes from here: module records with
    class/rating/mount/mtype, the blueprint and experimental-effect tables,
    the mtype table saying which of those are legal for each kind of
    module, and each hull's slot layout.
    """

    def __init__(self, raw: bytes) -> None:
        text = raw.decode("utf-8-sig")

        sections = self._split_sections(text)

        self.modules: list[RefModule] = []
        self.ships: dict[str, RefShip] = {}
        self.blueprints: dict[str, dict[str, str | None]] = {}
        self.expeffects: dict[str, dict[str, str | None]] = {}
        self.mtypes: dict[str, dict[str, Any]] = {}

        if "module" in sections:
            self._read_modules(sections["module"])

        if "ship" in sections:
            self._read_ships(sections["ship"])

        if "blueprint" in sections:
            self.blueprints = self._read_named(sections["blueprint"])

        if "expeffect" in sections:
            self.expeffects = self._read_named(sections["expeffect"])

        if "mtype" in sections:
            self._read_mtypes(sections["mtype"])

        self._infer_missing_mtypes()

    # -- parsing ----------------------------------------------------------

    def _infer_missing_mtypes(self) -> None:
        """
        Hull-specific armour lives in each ship's nested module block, where
        EDSY has no need to repeat mtype. Engineering resolution needs it,
        so recover it by finding the module type whose blueprints are the
        Armour_* family.
        """

        bulkheads = None

        for key, entry in self.mtypes.items():
            fdnames = [
                self.blueprints.get(bp, {}).get("fdname") or ""
                for bp in entry["blueprints"]
            ]

            if fdnames and all(fd.startswith("Armour_") for fd in fdnames):
                bulkheads = key
                break

        if not bulkheads:
            return

        for module in self.modules:
            if module.mtype is None and ARMOUR_SYMBOL.search(module.fdname):
                module.mtype = bulkheads

    @staticmethod
    def _split_sections(text: str) -> dict[str, str]:
        sections: dict[str, str] = {}

        for match in re.finditer(r"^\t([a-zA-Z_]+) ?: ?\{", text, re.M):
            if line_is_commented(text, match.start()):
                continue

            try:
                block, _ = match_braces(text, match.end() - 1)
            except ValueError:
                continue

            sections[match.group(1)] = block[1:-1]

        return sections

    @staticmethod
    def _module_from(record: str) -> RefModule | None:
        fdname = js_string(record, "fdname")

        if not fdname:
            return None

        return RefModule(
            fdname=fdname,
            name=js_string(record, "name"),
            mtype=js_string(record, "mtype"),
            cls=js_int(record, "class"),
            rating=js_string(record, "rating"),
            mount=js_string(record, "mount"),
            cargocap=js_number(record, "cargocap"),
            source="edsy",
        )

    def _read_modules(self, body: str) -> None:
        for _key, record in iter_records(body):
            module = self._module_from(record)

            if module:
                self.modules.append(module)

    def _read_ships(self, body: str) -> None:
        for _key, record in iter_records(body):
            fdname = js_string(record, "fdname")

            if not fdname:
                continue

            slots: dict[str, list[int]] = {}

            slot_match = re.search(r"slots\s*:\s*\{", record)

            if slot_match:
                try:
                    block, _ = match_braces(record, slot_match.end() - 1)
                except ValueError:
                    block = "{}"

                for group in (
                    "hardpoint",
                    "utility",
                    "component",
                    "military",
                    "internal",
                ):
                    slots[group] = js_int_array(block, group)

            # Some hulls carry dedicated bays whose journal slot names do
            # not follow the generated pattern -- the Panther's Cargo01,
            # the Lynx's Passenger01, the Type-11's FighterBay01 -- and
            # some number their remaining slots with gaps. EDSY records
            # the real names in a slotnames{} block, which is authoritative
            # wherever it is present.
            slotnames: dict[str, list[str]] = {}

            names_match = re.search(r"slotnames\s*:\s*\{", record)

            if names_match and not line_is_commented(
                record, names_match.start()
            ):
                try:
                    block, _ = match_braces(record, names_match.end() - 1)
                except ValueError:
                    block = "{}"

                for group in (
                    "hardpoint",
                    "utility",
                    "component",
                    "military",
                    "internal",
                ):
                    names = js_string_array(block, group)

                    if names:
                        slotnames[group] = names

            self.ships[fdname] = RefShip(
                fdname=fdname,
                name=js_string(record, "name"),
                cost=js_int(record, "cost"),
                slots=slots,
                slotnames=slotnames,
            )

            # Hull-restricted modules (armour, and anything else Frontier
            # ties to one ship) live in a nested module{} block.
            module_match = re.search(r"module\s*:\s*\{", record)

            if not module_match:
                continue

            try:
                block, _ = match_braces(record, module_match.end() - 1)
            except ValueError:
                continue

            for _mkey, mrecord in iter_records(block[1:-1]):
                module = self._module_from(mrecord)

                if module:
                    self.modules.append(module)

    @staticmethod
    def _read_named(body: str) -> dict[str, dict[str, str | None]]:
        out: dict[str, dict[str, str | None]] = {}

        for key, record in iter_records(body):
            fdname = js_string(record, "fdname")
            name = js_string(record, "name")

            if fdname or name:
                out[key] = {"name": name, "fdname": fdname}

        return out

    def _read_mtypes(self, body: str) -> None:
        for key, record in iter_records(body):
            self.mtypes[key] = {
                "name": js_string(record, "name"),
                "blueprints": js_string_array(record, "blueprints"),
                "expeffects": js_string_array(record, "expeffects"),
            }


# ===========================================================================
# FDevIDs
# ===========================================================================

def load_fdevids(
    refresh: bool = False,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:

    outfitting_raw = download_reference(
        FDEVIDS_OUTFITTING_URL,
        FDEVIDS_OUTFITTING_CACHE,
        refresh,
    )

    shipyard_raw = download_reference(
        FDEVIDS_SHIPYARD_URL,
        FDEVIDS_SHIPYARD_CACHE,
        refresh,
    )

    def rows(raw: bytes) -> list[dict[str, str]]:
        return list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))

    return rows(outfitting_raw), rows(shipyard_raw)


FDEV_MOUNTS = {
    "FIXED": "F",
    "GIMBAL": "G",
    "GIMBALLED": "G",
    "TURRET": "T",
    "TURRETED": "T",
}


def fdevids_modules(rows: list[dict[str, str]]) -> list[RefModule]:
    out: list[RefModule] = []

    for row in rows:
        symbol = (row.get("symbol") or "").strip()

        if not symbol:
            continue

        try:
            cls: int | None = int((row.get("class") or "").strip())
        except ValueError:
            cls = None

        out.append(
            RefModule(
                fdname=symbol,
                name=(row.get("name") or "").strip() or None,
                cls=cls,
                rating=(row.get("rating") or "").strip() or None,
                mount=FDEV_MOUNTS.get(
                    (row.get("mount") or "").strip().upper()
                ),
                source="fdevids",
            )
        )

    return out


# ===========================================================================
# EDOMH identifier grammar
# ===========================================================================

#: Suffix tokens EDOMH appends after <class>_<rating>. None of these change
#: which module is meant except FREE (a genuinely distinct zero-cost
#: symbol) and the mount letter. PRE and MERC describe how the module was
#: acquired; any pre-engineering rides along as modifiers.
FLAG_PATTERNS = [
    ("mkii", re.compile(r"^MK_?II$")),
    ("pre", re.compile(r"^PRE$")),
    ("merc", re.compile(r"^MERC$")),
    ("free", re.compile(r"^FREE$")),
    ("variant", re.compile(r"^V\d+$")),
]

MOUNTS = {"F", "G", "T"}

EDOMH_ID = re.compile(
    r"^(?P<base>.+?)_(?P<cls>\d+)_(?P<rating>[A-I])"
    r"(?P<rest>(?:_[A-Z0-9]+)*)$"
)

ARMOUR_ID = re.compile(
    r"^(?P<ship>.+?)_ARMOUR_"
    r"(?:GRADE_(?P<grade>\d+)|(?P<special>REACTIVE|MIRRORED))$"
)


class EdomhModuleId:
    __slots__ = ("raw", "base", "cls", "rating", "mount", "flags", "unknown")

    def __init__(
        self,
        raw: str,
        base: str,
        cls: int | None,
        rating: str | None,
        mount: str | None,
        flags: set[str],
        unknown: list[str],
    ) -> None:
        self.raw = raw
        self.base = base
        self.cls = cls
        self.rating = rating
        self.mount = mount
        self.flags = flags
        self.unknown = unknown


def parse_edomh_id(item_id: str) -> EdomhModuleId:
    upper = item_id.upper()

    match = EDOMH_ID.fullmatch(upper)

    if not match:
        # Sizeless modules such as CARGO_HATCH or SUPERCRUISE_ASSIST.
        return EdomhModuleId(upper, upper, None, None, None, set(), [])

    tokens = [t for t in (match.group("rest") or "").split("_") if t]

    # MK_II arrives as two tokens; rejoin before classifying.
    joined: list[str] = []
    index = 0

    while index < len(tokens):
        if (
            tokens[index] == "MK"
            and index + 1 < len(tokens)
            and tokens[index + 1] == "II"
        ):
            joined.append("MK_II")
            index += 2
            continue

        joined.append(tokens[index])
        index += 1

    flags: set[str] = set()
    unknown: list[str] = []
    mount: str | None = None

    for token in joined:
        if token in MOUNTS and mount is None:
            mount = token
            continue

        for flag, pattern in FLAG_PATTERNS:
            if pattern.fullmatch(token):
                flags.add(flag)
                break
        else:
            unknown.append(token)

    return EdomhModuleId(
        raw=upper,
        base=match.group("base"),
        cls=int(match.group("cls")),
        rating=match.group("rating"),
        mount=mount,
        flags=flags,
        unknown=unknown,
    )


# ===========================================================================
# EDOMH -> reference name aliases
#
# EDOMH derives most identifiers from the in-game outfitting name, so the
# large majority resolve on name/class/rating with no help at all. These
# are the cases where EDOMH's wording and Frontier's genuinely differ.
# ===========================================================================

NAME_ALIASES: dict[str, list[str]] = {
    # EDOMH drops the "Guardian" qualifier.
    "FRAMESHIFTDRIVEBOOSTER": ["Guardian Frame Shift Drive Booster"],

    # EDOMH keeps the internal "Overcharge" wording; Frontier ships "(SCO)".
    "FRAMESHIFTDRIVEOVERCHARGE": ["Frame Shift Drive (SCO)"],

    # Fighter bays are "Vessel Hangar" in game.
    "FIGHTERHANGAR": ["Vessel Hangar"],
    "MKIIFIGHTERHANGAR": ["Mk II Vessel Hangar"],

    # The high-capacity racks are branded "Mk II", not "Large".
    "LARGECARGORACK": ["Mk II Cargo Rack"],

    # EDOMH says "Package"; FDevIDs drops it, EDSY keeps it.
    "GUARDIANSHIELDREINFORCEMENTPACKAGE": [
        "Guardian Shield Reinforcement Package",
        "Guardian Shield Reinforcement",
    ],
    "HULLREINFORCEMENTPACKAGE": [
        "Hull Reinforcement Package",
        "Hull Reinforcement",
    ],
    "MODULEREINFORCEMENTPACKAGE": [
        "Module Reinforcement Package",
        "Module Reinforcement",
    ],
    "GUARDIANMODULEREINFORCEMENTPACKAGE": [
        "Guardian Module Reinforcement Package",
        "Guardian Module Reinforcement",
    ],
    "GUARDIANHULLREINFORCEMENTPACKAGE": [
        "Guardian Hull Reinforcement Package",
        "Guardian Hull Reinforcement",
    ],
}

#: Alternatives tried when the Mk II flag is set. Class and rating still
#: pick between them, so listing every variant is safe.
MKII_ALIASES: dict[str, list[str]] = {
    "THRUSTERS": [
        "Mk II Agile Boost Thrusters",
        "Mk II Gravity Optimised Thrusters",
        "Mk II Enhanced Performance Thrusters",
    ],
    "FRAMESHIFTDRIVEOVERCHARGE": [
        "Mk II Supercharge Optimised FSD (SCO)",
        "Mk II Supercharge Optimised Frame Shift Drive (SCO)",
    ],
}


# ===========================================================================
# Module index
# ===========================================================================

SIZE_IN_SYMBOL = re.compile(r"SIZE(\d+)", re.I)


def symbol_size(fdname: str) -> int | None:
    match = SIZE_IN_SYMBOL.search(fdname.replace("_", ""))
    return int(match.group(1)) if match else None


class ModuleIndex:
    """
    Display-name lookup over the merged EDSY + FDevIDs module tables.

    EDSY entries outrank FDevIDs ones because EDSY is what consumes the
    output, and full display names outrank the parenthesis-stripped form.
    """

    def __init__(
        self,
        edsy_modules: list[RefModule],
        fdev_modules: list[RefModule],
    ) -> None:
        self.modules = list(edsy_modules) + list(fdev_modules)

        self.by_name: dict[str, list[tuple[int, RefModule]]] = {}
        self.by_fdname: dict[str, RefModule] = {}

        for module in self.modules:
            key = normalize_symbol(module.fdname)

            existing = self.by_fdname.get(key)

            if existing is None or (
                existing.source != "edsy" and module.source == "edsy"
            ):
                self.by_fdname[key] = module

            if not module.name:
                continue

            source_rank = 2 if module.source == "edsy" else 1

            full = normalize_symbol(module.name)
            reduced = normalize_symbol(strip_parens(module.name))

            self.by_name.setdefault(full, []).append(
                (source_rank + 2, module)
            )

            if reduced and reduced != full:
                self.by_name.setdefault(reduced, []).append(
                    (source_rank, module)
                )

    def candidates(self, name: str) -> list[tuple[int, RefModule]]:
        return self.by_name.get(normalize_symbol(name), [])


def resolve_module(
    item_id: str,
    index: ModuleIndex,
    ship_symbol: str | None,
) -> tuple[str, RefModule | None]:
    """
    Resolve an EDOMH module identifier to its Frontier/EDSY symbol.

    Returns (symbol, reference_record). Raises ValueError rather than
    guessing when the module cannot be identified confidently.
    """

    upper = item_id.upper()

    # -- hull-specific armour ---------------------------------------------

    armour = ARMOUR_ID.fullmatch(upper)

    if armour:
        if not ship_symbol:
            raise ValueError(
                f"Cannot resolve armour {item_id} without a ship type"
            )

        if armour.group("grade"):
            variant = f"Grade{int(armour.group('grade'))}"
        else:
            variant = armour.group("special").title()

        symbol = f"{ship_symbol}_Armour_{variant}"
        record = index.by_fdname.get(normalize_symbol(symbol))

        if record:
            return record.fdname, record

        raise ValueError(
            f"No {variant} armour listed for hull {ship_symbol} "
            f"(from {item_id})"
        )

    parsed = parse_edomh_id(item_id)

    # -- candidate display names ------------------------------------------

    base_key = normalize_symbol(parsed.base)

    names: list[str] = []

    if "mkii" in parsed.flags:
        names.extend(MKII_ALIASES.get(base_key, []))

    names.extend(NAME_ALIASES.get(base_key, []))

    # The identifier itself is a display name in SCREAMING_SNAKE, so it is
    # tried last; the alias lists above cover only genuine divergences.
    names.append(parsed.base)

    scored: list[tuple[int, RefModule]] = []

    for position, name in enumerate(names):
        # Earlier names are more specific; weight them above later ones.
        bonus = (len(names) - position) * 100

        for score, module in index.candidates(name):
            scored.append((score + bonus, module))

    if not scored:
        raise ValueError(
            f"No module named like {item_id} in EDSY or FDevIDs data"
        )

    # -- filter on everything the identifier tells us ----------------------

    wants_free = "free" in parsed.flags

    def acceptable(module: RefModule) -> bool:
        if parsed.mount and module.mount and module.mount != parsed.mount:
            return False

        if module.fdname.upper().endswith("_FREE") != wants_free:
            return False

        return True

    viable = [entry for entry in scored if acceptable(entry[1])]

    if not viable:
        viable = scored

    def rank_key(entry: tuple[int, RefModule]) -> tuple:
        score, module = entry

        # Class and rating from the identifier are the strongest signal,
        # but FDevIDs carries a few transcription errors in its class
        # column, so a Size<n> embedded in the symbol counts equally.
        class_hit = parsed.cls is not None and module.cls == parsed.cls

        size_hit = (
            parsed.cls is not None
            and symbol_size(module.fdname) == parsed.cls
        )

        rating_hit = (
            parsed.rating is not None
            and module.rating is not None
            and module.rating.upper() == parsed.rating
        )

        mount_hit = (
            parsed.mount is not None and module.mount == parsed.mount
        )

        return (
            int(class_hit or size_hit),
            int(rating_hit),
            int(size_hit),
            int(mount_hit),
            score,
            int(module.source == "edsy"),
        )

    viable.sort(key=rank_key, reverse=True)

    _score, best = viable[0]

    # A module may be matched via its FDevIDs row (EDSY sometimes spells the
    # same module differently, e.g. "Cargo Rack (Cap: 128)"). Swap in EDSY's
    # record for the same symbol when there is one: it carries the mtype and
    # capacity the rest of the conversion needs, and EDSY's own casing.
    equivalent = index.by_fdname.get(normalize_symbol(best.fdname))

    if equivalent is not None and equivalent.source == "edsy":
        best = equivalent

    # Refuse to emit a module of the wrong size. Silently substituting one
    # would produce a loadout the commander never built.
    if parsed.cls is not None:
        size = symbol_size(best.fdname)

        if best.cls != parsed.cls and size != parsed.cls:
            raise ValueError(
                f"Best match for {item_id} is {best.fdname}, which is "
                f"class {best.cls} not {parsed.cls}"
            )

    return best.fdname, best


# ===========================================================================
# Ship resolution
# ===========================================================================

#: EDOMH uses Frontier's marketing names for hulls whose internal symbol
#: differs and whose shipyard name does not normalise onto it. Everything
#: else falls out of the ship tables automatically.
SHIP_ALIASES: dict[str, str] = {
    "LYNX_HIGHLINER": "MediumTransport01",
    "KESTREL_MK_II": "SmallCombat01_NX",
    "PANTHER_CLIPPER_MK_II": "PantherMkII",
    "TYPE_9_MILITARY": "Type9_Military",
}


def find_ship_symbol(
    edomh_ship_type: str,
    edsy: EdsyDatabase,
    shipyard: list[dict[str, str]],
) -> str | None:
    upper = edomh_ship_type.upper()

    alias = SHIP_ALIASES.get(upper)

    if alias and alias in edsy.ships:
        return alias

    target = normalize_symbol(edomh_ship_type)

    # EDSY's own ship table first: its fdname is what SLEF must carry.
    for fdname in edsy.ships:
        if normalize_symbol(fdname) == target:
            return fdname

    for fdname, ship in edsy.ships.items():
        if ship.name and normalize_symbol(ship.name) == target:
            return fdname

    # FDevIDs shipyard as a secondary index onto the same symbols.
    for row in shipyard:
        symbol = (row.get("symbol") or "").strip()
        name = (row.get("name") or "").strip()

        if not symbol:
            continue

        if target not in (normalize_symbol(symbol), normalize_symbol(name)):
            continue

        for fdname in edsy.ships:
            if normalize_symbol(fdname) == normalize_symbol(symbol):
                return fdname

        return symbol

    return alias


# ===========================================================================
# Slot layout
#
# Journal slot names describe the hull's slot, not the fitted module. A
# size 4 rack in a size 6 bay is still Slot0N_Size6, so the layout has to
# come from the ship record rather than from the module identifier.
# ===========================================================================

CORE_SLOTS = [
    "Armour",
    "PowerPlant",
    "MainEngines",
    "FrameShiftDrive",
    "LifeSupport",
    "PowerDistributor",
    "Radar",
    "FuelTank",
]

HARDPOINT_SIZES = {1: "Small", 2: "Medium", 3: "Large", 4: "Huge"}


def override_name(ship: RefShip, group: str, position: int) -> str | None:
    """
    The name EDSY records for this hull's `group` slot at `position`
    (1-based), or None where EDSY leaves the name to be generated.

    Overrides are positional against the matching slots{} array, so a
    short or absent list simply leaves the later slots generated.
    """

    names = ship.slotnames.get(group, [])

    if 1 <= position <= len(names):
        return names[position - 1]

    return None


def optional_slot_names(ship: RefShip) -> list[str]:
    """
    EDOMH indexes optional slots across the hull's internal and military
    bays merged and ordered by descending size, internal bays ahead of
    military ones of equal size. Journal names number the two families
    separately.

    Generating those names from the slot sizes is right for most hulls
    but wrong for any hull EDSY gives a slotnames{} override: dedicated
    bays are named for their purpose rather than numbered (Cargo01,
    Passenger01, FighterBay01, LimpetController01) and are skipped by the
    numbering of the ordinary slots around them, which can also run with
    gaps. Take EDSY's name wherever it has one, and note that overrides
    are positional against the unsorted slots{} array, so they have to be
    resolved before the merge below reorders anything.
    """

    entries: list[tuple[int, int, str]] = []

    for position, size in enumerate(ship.slots.get("internal", []), 1):
        name = override_name(ship, "internal", position)
        entries.append((size, 0, name or f"Slot{position:02d}_Size{size}"))

    for position, size in enumerate(ship.slots.get("military", []), 1):
        name = override_name(ship, "military", position)
        entries.append((size, 1, name or f"Military{position:02d}"))

    entries.sort(key=lambda entry: (-entry[0], entry[1]))

    return [name for _size, _kind, name in entries]


def hardpoint_slot_names(ship: RefShip) -> list[str]:
    counters: dict[str, int] = {}
    names: list[str] = []

    for position, size in enumerate(ship.slots.get("hardpoint", []), 1):
        label = HARDPOINT_SIZES.get(size, f"Size{size}")

        # Counted even when overridden, so that any slot past the end of
        # a short override list still numbers from the right place.
        counters[label] = counters.get(label, 0) + 1

        name = override_name(ship, "hardpoint", position)
        names.append(name or f"{label}Hardpoint{counters[label]}")

    return names


def utility_slot_names(ship: RefShip) -> list[str]:
    names: list[str] = []

    for position, _size in enumerate(ship.slots.get("utility", []), 1):
        name = override_name(ship, "utility", position)
        names.append(name or f"TinyHardpoint{position}")

    return names


SLOT_NAMERS = {
    "optional": optional_slot_names,
    "hardpoint": hardpoint_slot_names,
    "utility": utility_slot_names,
}


def slot_name(group: str, index: int, ship: RefShip) -> str:
    if group == "core":
        if 0 <= index < len(CORE_SLOTS):
            return CORE_SLOTS[index]

        raise ValueError(f"Unknown EDOMH core slot index: {index}")

    table = SLOT_NAMERS[group](ship)

    if 0 <= index < len(table):
        return table[index]

    raise ValueError(
        f"EDOMH {group} slot index {index} is outside this hull's "
        f"{len(table)} slot(s)"
    )


# ===========================================================================
# Engineering
#
# EDOMH names blueprints and experimental effects by their in-game label.
# EDSY's mtype table lists exactly which blueprints and effects are legal
# for each kind of module, so scoping the name match to the resolved
# module's mtype disambiguates shared labels ("Heavy Duty", "Lightweight",
# "Stripped Down") with no per-module special-casing.
# ===========================================================================

BLUEPRINT_ALIASES: dict[str, list[str]] = {
    "AMMO_CAPACITY": ["Ammo Capacity"],
    "ARMOURED": ["Armoured"],
    "CHARGE_ENHANCED": ["Charge Enhanced"],
    "DIRTY_DRIVE_TUNING": ["Dirty Tuning"],
    "CLEAN_DRIVE_TUNING": ["Clean Tuning"],
    "DRIVE_STRENGTHENING": ["Strengthening"],
    "ENGINE_FOCUSED": ["Engine Focused"],
    "ENHANCED_LOW_POWER_SHIELDS": ["Enhanced, Low Power"],
    "EXPANDED_CAPTURE_ARC": ["Expanded Capture Arc"],
    "EXPANDED_PROBE_SCANNING_RADIUS": ["Expanded Radius"],
    "FAST_SCAN": ["Fast Scan"],
    "HEAVY_DUTY": ["Heavy Duty"],
    "HIGH_CAPACITY_MAGAZINE": ["High Capacity"],
    "INCREASED_CARGO_CAPACITY": ["Expanded Cargo Rack"],
    "INCREASED_FSD_RANGE": ["Increased Range"],
    "INCREASED_FSD_RANGE_FASTER_FSD_BOOT_SEQUENCE": ["Increased Range"],
    "KINETIC_RESISTANT": ["Kinetic Resistant"],
    "BLAST_RESISTANT": ["Blast Resistant"],
    "LIGHTWEIGHT": ["Lightweight", "Light Weight"],
    "LIGHT_WEIGHT_SCANNER": ["Light Weight", "Lightweight"],
    "LONG_RANGE_SCANNER": ["Long Range"],
    "LONG_RANGE_WEAPON": ["Long Range"],
    "LOW_EMISSIONS": ["Low Emissions"],
    "MERC_EXTENDED_CARGO_RACK": ["Extended Cargo Rack"],
    "MERC_SCOOP_RATE_ENHANCED_FUEL_SCOOP": ["Scoop Rate Enhanced"],
    "OVERCHARGED": ["Overcharged"],
    "RAPID_CHARGE": ["Rapid Charge"],
    "RAPID_FIRE_MODIFICATION": ["Rapid Fire"],
    "REINFORCED_SHIELDS": ["Reinforced"],
    "RESISTANCE_AUGMENTED": ["Resistance Augmented"],
    "SCOOP_RATE_ENHANCED": ["Scoop Rate Enhanced"],
    "SHIELDED": ["Shielded"],
    "SHORT_RANGE_BLASTER": ["Short Range"],
    "SPECIALISED": ["Specialised"],
    "STURDY_MOUNT": ["Sturdy"],
    "SYSTEM_FOCUSED": ["System Focused"],
    "THERMAL_RESISTANT": ["Thermal Resistant"],
    "THERMAL_RESISTANT_HULL_REINFORCEMENT": ["Thermal Resistant"],
    "THERMAL_RESISTANT_SHIELDS": ["Thermal Resistant"],
    "WEAPON_FOCUSED": ["Weapon Focused"],
    "WIDE_ANGLE": ["Wide Angle"],
}

EXPERIMENTAL_ALIASES: dict[str, list[str]] = {
    "ANGLED_PLATING": ["Angled Plating"],
    "AUTO_LOADER": ["Auto Loader"],
    "BOSS_CELLS": ["Boss Cells"],
    "CLUSTER_CAPACITORS": ["Cluster Capacitors"],
    "CORROSIVE_SHELL": ["Corrosive Shell"],
    "DEEP_CHARGE": ["Deep Charge"],
    "DEEP_PLATING": ["Deep Plating"],
    "DOUBLE_BRACED": ["Double Braced"],
    "DRAG_DRIVES": ["Drag Drives"],
    "DRAG_MUNITIONS": ["Drag Munitions"],
    "DRIVE_DISTRIBUTORS": ["Drive Distributors"],
    "EMISSIVE_MUNITIONS": ["Emissive Munitions"],
    "FAST_CHARGE": ["Fast Charge"],
    "FLOW_CONTROL": ["Flow Control"],
    "FORCE_BLOCK": ["Force Block"],
    "HI_CAP": ["Hi-Cap"],
    "HIGH_YIELD_SHELL": ["High Yield Shell"],
    "INCENDIARY_ROUNDS": ["Incendiary Rounds"],
    "LAYERED_PLATING": ["Layered Plating"],
    "LO_DRAW": ["Lo-Draw"],
    "MASS_MANAGER": ["Mass Manager"],
    "MONSTERED": ["Monstered"],
    "MULTI_SERVOS": ["Multi-Servos"],
    "MULTI_WEAVE": ["Multi-Weave"],
    "OVERSIZED": ["Oversized"],
    "PHASING_SEQUENCE": ["Phasing Sequence"],
    "RECYCLING_CELL": ["Recycling Cell"],
    "REFLECTIVE_PLATING": ["Reflective Plating"],
    "SCRAMBLE_SPECTRUM": ["Scramble Spectrum"],
    "SMART_ROUNDS": ["Smart Rounds"],
    "STRIPPED_DOWN": ["Stripped Down"],
    "SUPER_CAPACITOR": ["Super Capacitors"],
    "SUPER_CAPACITORS": ["Super Capacitors"],
    "SUPER_CONDUITS": ["Super Conduits"],
    "SUPER_PENETRATOR": ["Super Penetrator", "Super Penetrator (Pre-Eng)"],
    "THERMAL_CASCADE": ["Thermal Cascade"],
    "THERMAL_CONDUIT": ["Thermal Conduit"],
    "THERMAL_SHOCK": ["Thermal Shock"],
    "THERMAL_SPREAD": ["Thermal Spread"],
    "THERMAL_VENT": ["Thermal Vent"],
    "THERMO_BLOCK": ["Thermo Block"],
}


def candidate_labels(
    raw_type: str,
    aliases: dict[str, list[str]],
) -> list[str]:
    key = raw_type.upper()

    if key in aliases:
        return aliases[key]

    # EDOMH sometimes appends the module the blueprint came from, e.g.
    # MERC_EXTENDED_CARGO_RACK_6E. Trim trailing qualifiers and retry.
    trimmed = key

    while "_" in trimmed:
        trimmed = trimmed.rsplit("_", 1)[0]

        if trimmed in aliases:
            return aliases[trimmed]

    # Last resort: treat the identifier itself as the label.
    return [key.replace("_", " ").title()]


def lookup_engineering(
    raw_type: str,
    module: RefModule | None,
    edsy: EdsyDatabase,
    kind: str,
) -> str | None:
    """
    Map an EDOMH blueprint or experimental identifier to its Journal name,
    preferring entries legal for this module's mtype.
    """

    is_blueprint = kind == "blueprint"

    table = edsy.blueprints if is_blueprint else edsy.expeffects
    aliases = BLUEPRINT_ALIASES if is_blueprint else EXPERIMENTAL_ALIASES
    field = "blueprints" if is_blueprint else "expeffects"

    labels = [
        normalize_symbol(label)
        for label in candidate_labels(raw_type, aliases)
    ]

    allowed: list[str] = []

    if module and module.mtype and module.mtype in edsy.mtypes:
        allowed = edsy.mtypes[module.mtype][field]

    # Scoped to this module type first. This is what makes shared labels
    # such as "Heavy Duty" resolve to the correct family.
    for label in labels:
        for key in allowed:
            entry = table.get(key)

            if entry and entry.get("name") and entry.get("fdname"):
                if normalize_symbol(entry["name"]) == label:
                    return entry["fdname"]

    # Unscoped fallback, but only where it is unambiguous.
    for label in labels:
        matches = {
            entry["fdname"]
            for entry in table.values()
            if entry.get("name")
            and entry.get("fdname")
            and normalize_symbol(entry["name"]) == label
        }

        if len(matches) == 1:
            return matches.pop()

    return None


# ===========================================================================
# Modifiers
# ===========================================================================

MODIFIER_LABELS: dict[str, str] = {
    "AMMO_CLIP_SIZE": "AmmoClipSize",
    "AMMO_MAXIMUM": "AmmoMaximum",
    "BOOT_TIME": "BootTime",
    "BROKEN_REGEN_RATE": "BrokenRegenRate",
    "BURST_RATE_OF_FIRE": "BurstRateOfFire",
    "BURST_SIZE": "BurstSize",
    "CARGO_CAPACITY": "CargoCapacity",
    "DAMAGE": "Damage",
    # EDSY writes the fdattr of each attribute, not its display name and
    # not the aliases it accepts on the way in. dmgfall is fdattr
    # 'FalloffRange'; 'DamageFalloffRange' is only an inbound alias in
    # eddb.fdfieldattr{}.
    "DAMAGE_FALLOFF_START": "FalloffRange",
    "DAMAGE_PER_SECOND": "DamagePerSecond",
    "DISTRIBUTOR_DRAW": "DistributorDraw",
    "DSS_PATCH_RADIUS": "DSS_PatchRadius",
    "ENERGY_PER_REGEN": "EnergyPerRegen",
    "ENGINES_CAPACITY": "EnginesCapacity",
    "ENGINES_RECHARGE": "EnginesRecharge",
    "ENGINE_OPTIMAL_MASS": "EngineOptimalMass",
    "ENGINE_THERMAL_LOAD": "EngineHeatRate",
    "EXPLOSIVE_RESISTANCE": "ExplosiveResistance",
    "HEAT_EFFICIENCY": "HeatEfficiency",
    "HULL_BOOST": "DefenceModifierHealthMultiplier",
    "HULL_REINFORCEMENT": "DefenceModifierHealthAddition",
    "INTEGRITY": "Integrity",
    "JITTER": "Jitter",
    "KINETIC_RESISTANCE": "KineticResistance",
    "MASS": "Mass",
    "MAXIMUM_RANGE": "MaximumRange",
    "POWER_CAPACITY": "PowerCapacity",
    "POWER_DRAW": "PowerDraw",
    "RATE_OF_FIRE": "RateOfFire",
    "REGEN_RATE": "RegenRate",
    "RELOAD_TIME": "ReloadTime",
    "SCAN_ANGLE": "SensorTargetScanAngle",
    "SHIELDGEN_OPTIMAL_MASS": "ShieldGenOptimalMass",
    "SHIELDGEN_OPTIMAL_STRENGTH": "ShieldGenStrength",
    "SHIELD_BOOST": "DefenceModifierShieldMultiplier",
    "SHOT_SPEED": "ShotSpeed",
    "SYSTEMS_CAPACITY": "SystemsCapacity",
    "SYSTEMS_RECHARGE": "SystemsRecharge",
    "THERMAL_LOAD": "ThermalLoad",
    "THERMAL_RESISTANCE": "ThermicResistance",
    # typemis is fdattr 'Range'. 'Typical Emission' is its display name,
    # which EDSY neither writes nor reads.
    "TYPICAL_EMISSION_RANGE": "Range",
    "WEAPONS_CAPACITY": "WeaponsCapacity",
    "WEAPONS_RECHARGE": "WeaponsRecharge",
}

#: OPTIMAL_MULTIPLIER is reused across families; its meaning depends on the
#: module carrying it.
OPTIMAL_MULTIPLIER_BY_MTYPE = {
    "ct": "EngineOptPerformance",
    "cfsd": "FSDOptimalMass",
    "cfsdo": "FSDOptimalMass",
    "isg": "ShieldGenStrength",
}


def modifier_label(
    raw_label: str,
    module: RefModule | None,
    item_id: str,
) -> str | None:
    label = raw_label.upper()

    if label != "OPTIMAL_MULTIPLIER":
        return MODIFIER_LABELS.get(label)

    if module and module.mtype in OPTIMAL_MULTIPLIER_BY_MTYPE:
        return OPTIMAL_MULTIPLIER_BY_MTYPE[module.mtype]

    family = item_id.upper()

    if family.startswith("THRUSTERS"):
        return "EngineOptPerformance"

    if family.startswith("FRAME_SHIFT_DRIVE"):
        return "FSDOptimalMass"

    if "SHIELD_GENERATOR" in family:
        return "ShieldGenStrength"

    return None


def journal_modifiers(
    module_data: dict[str, Any],
    reference: RefModule | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    item_id = str(module_data.get("id", ""))

    for raw_label, value in (module_data.get("modifiers") or {}).items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue

        label = modifier_label(str(raw_label), reference, item_id)

        if not label:
            # Better to omit than to invent a label EDSY would misread.
            warnings.append(
                f"unmapped modifier {raw_label} on {item_id} (omitted)"
            )
            continue

        out.append({"Label": label, "Value": round(numeric, 6)})

    return out


def engineering_from_module(
    module_data: dict[str, Any],
    reference: RefModule | None,
    edsy: EdsyDatabase,
    warnings: list[str],
) -> dict[str, Any] | None:

    item_id = str(module_data.get("id", ""))

    engineering: dict[str, Any] = {}

    mods = module_data.get("modification") or []

    if mods:
        modification = mods[0]

        raw_type = str(modification.get("type") or "")

        if raw_type:
            blueprint = lookup_engineering(
                raw_type, reference, edsy, "blueprint"
            )

            if blueprint:
                engineering["BlueprintName"] = blueprint
            else:
                warnings.append(
                    f"unknown blueprint {raw_type} on {item_id} (omitted)"
                )

        grade = re.search(r"(\d+)", str(modification.get("grade", "")))

        if grade:
            engineering["Level"] = int(grade.group(1))

        quality = modification.get("percentComplete")

        if quality is not None:
            engineering["Quality"] = float(quality)

    exps = module_data.get("experimentalEffect") or []

    if exps:
        raw_type = str(exps[0].get("type") or "")

        if raw_type:
            special = lookup_engineering(
                raw_type, reference, edsy, "experimental"
            )

            if special:
                engineering["ExperimentalEffect"] = special
            else:
                warnings.append(
                    f"unknown experimental effect {raw_type} on "
                    f"{item_id} (omitted)"
                )

    modifiers = journal_modifiers(module_data, reference, warnings)

    if modifiers:
        engineering["Modifiers"] = modifiers

    return engineering or None


# ===========================================================================
# EDOMH decoding
# ===========================================================================

EDOMH_PREFIX = "edomh://ship/?"


def validate_edomh(obj: dict[str, Any]) -> dict[str, Any]:
    if obj.get("event") != "ship":
        raise ValueError(
            f"Expected EDOMH event 'ship', got {obj.get('event')!r}"
        )

    if obj.get("version") != 2:
        raise ValueError(
            f"Expected EDOMH ship version 2, got {obj.get('version')!r}"
        )

    if "shipConfiguration" not in obj:
        raise ValueError("EDOMH payload has no shipConfiguration")

    return obj


def decode_edomh_url(edomh_url: str) -> dict[str, Any]:
    """
    Decode:

        edomh://ship/?<base64url(zlib(json))>
    """

    if not edomh_url.startswith(EDOMH_PREFIX):
        raise ValueError(f"Unsupported EDOMH URI: {edomh_url[:100]!r}")

    encoded = edomh_url[len(EDOMH_PREFIX):]
    encoded += "=" * (-len(encoded) % 4)

    try:
        compressed = base64.urlsafe_b64decode(encoded)
    except Exception as exc:
        raise ValueError("Invalid EDOMH base64 payload") from exc

    try:
        raw = zlib.decompress(compressed)
    except zlib.error as exc:
        raise ValueError("EDOMH payload is not valid zlib data") from exc

    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(
            "EDOMH payload does not contain valid JSON"
        ) from exc

    return validate_edomh(obj)


def fetch_edomh_ship(url: str) -> tuple[dict[str, Any], str]:
    location = get_redirect_location(url)

    if not location.startswith(EDOMH_PREFIX):
        raise ValueError(f"Unexpected redirect from {url}: {location[:200]}")

    return decode_edomh_url(location), location


def load_edomh_file(path: Path) -> tuple[dict[str, Any], str | None]:
    """
    Re-read a payload this tool previously preserved, so a conversion can
    be retried without going back to the network.
    """

    obj = json.loads(path.read_text(encoding="utf-8"))

    meta = obj.pop("_edsx", {}) or {}

    return validate_edomh(obj), meta.get("redirect")


# ===========================================================================
# Raw payload preservation
# ===========================================================================

def safe_filename(name: str) -> str:
    name = name.strip() or "Unnamed Ship"
    name = re.sub(r"[^\w .()\-]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:100]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    for extension in (".slef.json", ".edomh.json"):
        if path.name.endswith(extension):
            base = path.name[: -len(extension)]
            break
    else:
        base = path.stem
        extension = path.suffix

    index = 2

    while True:
        candidate = path.parent / f"{base} ({index}){extension}"

        if not candidate.exists():
            return candidate

        index += 1


def save_raw_edomh(
    edomh: dict[str, Any],
    source_url: str,
    location: str | None,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = edomh.get("shipConfiguration", {})

    name = str(cfg.get("name") or "Unnamed Ship")
    uuid = str(cfg.get("uuid") or "")

    filename = safe_filename(name)

    if uuid:
        filename += f" [{uuid[:8]}]"

    path = unique_path(output_dir / f"{filename}.edomh.json")

    saved = {
        "_edsx": {
            "sourceUrl": source_url,
            "redirect": location,
            "decodedAt": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
        },
        **edomh,
    }

    path.write_text(
        json.dumps(saved, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return path


# ===========================================================================
# Whole-ship conversion
# ===========================================================================

SLOT_GROUPS = [
    ("coreSlots", "core"),
    ("optionalSlots", "optional"),
    ("hardpointSlots", "hardpoint"),
    ("utilitySlots", "utility"),
]

KNOWN_SLOT_KEYS = {key for key, _group in SLOT_GROUPS} | {"cargoHatch"}


def convert_ship(
    edomh: dict[str, Any],
    edsy: EdsyDatabase,
    index: ModuleIndex,
    shipyard: list[dict[str, str]],
    warnings: list[str],
) -> dict[str, Any]:

    cfg = edomh["shipConfiguration"]

    ship_type = str(cfg["shipType"])
    ship_name = str(cfg.get("name") or "")

    ship_symbol = find_ship_symbol(ship_type, edsy, shipyard)

    if not ship_symbol:
        raise ValueError(f"Could not resolve ship type {ship_type!r}")

    ship = edsy.ships.get(ship_symbol)

    if ship is None:
        raise ValueError(
            f"EDSY has no slot layout for hull {ship_symbol!r}; "
            f"try --refresh-data"
        )

    print(
        f"  Ship: {ship_name or '(unnamed)'} "
        f"[{ship_type} -> {ship_symbol}]",
        file=sys.stderr,
    )

    modules: list[dict[str, Any]] = []
    cargo_capacity = 0.0

    def add(module_data: dict[str, Any], slot: str) -> None:
        nonlocal cargo_capacity

        item_id = str(module_data["id"])

        symbol, reference = resolve_module(item_id, index, ship_symbol)

        entry: dict[str, Any] = {
            "Slot": slot,
            "Item": symbol.lower(),
            "On": bool(module_data.get("powered", True)),
            "Priority": max(0, int(module_data.get("powerGroup", 1)) - 1),
            "Value": int(module_data.get("buyPrice", 0) or 0),
        }

        engineering = engineering_from_module(
            module_data, reference, edsy, warnings
        )

        if engineering:
            entry["Engineering"] = engineering

        modules.append(entry)

        if reference and reference.cargocap:
            cargo_capacity += reference.cargocap

    hatch = cfg.get("cargoHatch")

    if hatch and hatch.get("id"):
        add(hatch, "CargoHatch")

    for key, group in SLOT_GROUPS:
        for slot in cfg.get(key) or []:
            if not slot.get("id"):
                continue

            add(slot, slot_name(group, int(slot.get("index", 0)), ship))

    # Don't silently drop a slot family EDOMH adds later.
    unexpected = sorted(
        key
        for key in cfg
        if key.endswith(("Slot", "Slots"))
        and key not in KNOWN_SLOT_KEYS
        and cfg.get(key)
    )

    if unexpected:
        raise ValueError(
            "EDOMH payload contains slot groups not yet mapped: "
            + ", ".join(unexpected)
        )

    return {
        "event": "Loadout",
        "Ship": ship_symbol.lower(),
        "ShipName": ship_name,
        "ShipIdent": "",
        "HullValue": int(ship.cost or 0),
        "ModulesValue": int(sum(int(m.get("Value", 0)) for m in modules)),
        "UnladenMass": float(cfg.get("mass", 0) or 0),
        "CargoCapacity": int(cargo_capacity),
        "MaxJumpRange": float(cfg.get("jumpRange", 0) or 0),
        "FuelCapacity": {
            "Main": float(cfg.get("currentFuel", 0) or 0),
            "Reserve": float(cfg.get("currentFuelReserve", 0) or 0),
        },
        "Modules": modules,
    }


def make_slef(loadout: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "header": {
                "appName": "EDSX",
                "appVersion": VERSION,
            },
            "data": loadout,
        }
    ]


# ===========================================================================
# Input handling
# ===========================================================================

def read_inputs(args: list[str]) -> list[str]:
    """
    Accept EDOMH URLs, files listing them, and previously-decoded
    .edomh.json payloads.
    """

    out: list[str] = []

    for arg in args:
        path = Path(arg)

        if path.is_file() and path.name.endswith(".edomh.json"):
            out.append(str(path))
            continue

        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()

                if line and not line.startswith("#"):
                    out.append(line)

            continue

        out.append(arg)

    return out


# ===========================================================================
# Failure reporting
# ===========================================================================

def append_failure(source: str, message: str) -> None:
    with FAILURE_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{source}\n  ERROR: {message}\n\n")


# ===========================================================================
# Selftest
#
# A frozen build can start cleanly and still be broken: the reference
# parser walks eddb.js by hand, and resolution depends on that parse. This
# runs the real code paths against a miniature eddb-shaped fixture, so it
# needs no network and no cached reference data.
# ===========================================================================

SELFTEST_EDDB = (
    "var eddb = {\n"
    "\tship : {\n"
    "\t\t 1 : {\n"
    "\t\t\tfdname:'TestHull', name:'Test Hull', cost:1234567,\n"
    "\t\t\tslots:{\n"
    "\t\t\t\thardpoint:[3,2,1],\n"
    "\t\t\t\tutility  :[0,0],\n"
    "\t\t\t\tcomponent:[1,4,4,4,3,4,3,4],\n"
    "\t\t\t\tmilitary :[4],\n"
    "\t\t\t\tinternal :[5,4,3,2,1],\n"
    "\t\t\t},\n"
    "\t\t\tmodule:{\n"
    "\t\t\t\t40131 : { cost:0, mass:0.00, "
    "fdname:'TestHull_Armour_Grade1' },\n"
    "\t\t\t},\n"
    "\t\t},\n"
    # A hull of the awkward kind: a dedicated bay named for its purpose,
    # ordinary slots numbered around it with a gap, a renamed hardpoint,
    # and a short hardpoint override whose tail must still generate.
    "\t\t 2 : {\n"
    "\t\t\tfdname:'TestHullNamed', name:'Test Hull Named', cost:7654321,\n"
    "\t\t\tslots:{\n"
    "\t\t\t\thardpoint:[3,2],\n"
    "\t\t\t\tutility  :[0],\n"
    "\t\t\t\tcomponent:[1,4,4,4,3,4,3,4],\n"
    "\t\t\t\tmilitary :[4],\n"
    "\t\t\t\tinternal :[5,4,3,2,1],\n"
    "\t\t\t},\n"
    "\t\t\tslotnames:{\n"
    "\t\t\t\thardpoint:['LargeMiningHardpoint1'],\n"
    "\t\t\t\tinternal :['Cargo01','Slot01_Size4','Slot02_Size3',"
    "'Slot05_Size2','Slot06_Size1'],\n"
    "\t\t\t},\n"
    "\t\t},\n"
    "\t},\n"
    "\tblueprint : {\n"
    "\t\tcbh_hd : { name:'Heavy Duty', fdname:'Armour_HeavyDuty' },\n"
    "\t\tusb_hd : { name:'Heavy Duty', "
    "fdname:'ShieldBooster_HeavyDuty' },\n"
    "\t\tmisc_lw : { name:'Lightweight', fdname:'Misc_LightWeight' },\n"
    "\t},\n"
    "\texpeffect : {\n"
    "\t\tcbhx_dp : { name:'Deep Plating', "
    "fdname:'special_armour_chunky' },\n"
    "\t},\n"
    "\tmtype : {\n"
    "\t\tcbh : { name:'Bulkheads', blueprints:['cbh_hd'], "
    "expeffects:['cbhx_dp'] },\n"
    "\t\tusb : { name:'Shield Boosters', blueprints:['usb_hd'] },\n"
    "\t\ticr : { name:'Cargo Racks', blueprints:[] },\n"
    "\t\thpl : { name:'Pulse Lasers', blueprints:[] },\n"
    "\t\tifh : { name:'Vessel Hangars', blueprints:[] },\n"
    "\t},\n"
    "\tmodule : {\n"
    "\t\t  750 : { mtype:'icr', name:'Cargo Rack (Cap: 32)', class:4, "
    "rating:'E', cargocap:32, fdname:'Int_CargoRack_Size4_Class1' },\n"
    "\t\t62171 : { mtype:'hpl', name:'Pulse Laser', mount:'G', class:1, "
    "rating:'G', fdname:'Hpt_PulseLaser_Gimbal_Small' },\n"
    "\t\t62172 : { mtype:'hpl', name:'Pulse Laser', mount:'T', class:1, "
    "rating:'G', fdname:'Hpt_PulseLaser_Turret_Small' },\n"
    "\t\t 8001 : { mtype:'usb', name:'Shield Booster', class:0, "
    "rating:'A', fdname:'Hpt_ShieldBooster_Size0_Class5' },\n"
    "\t\t 7643 : { mtype:'ifh', name:'Vessel Hangar', class:5, "
    "rating:'D', fdname:'Int_FighterBay_Size5_Class1' },\n"
    "\t\t 7644 : { mtype:'ifh', name:'Vessel Hangar', class:5, "
    "rating:'D', fdname:'Int_FighterBay_Size5_Class1_Free' },\n"
    "\t\t49180 : { mtype:'cch', name:'Cargo Hatch', class:1, "
    "rating:'H', fdname:'ModularCargoBayDoor' },\n"
    "\t},\n"
    "};\n"
)


def selftest() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, got: Any, want: Any = True) -> None:
        ok = got == want
        detail = "" if ok else f"got {got!r}, expected {want!r}"
        checks.append((name, ok, detail))

    try:
        edsy = EdsyDatabase(SELFTEST_EDDB.encode("utf-8"))
    except Exception as exc:
        print(f"selftest: reference parser failed outright: {exc}")
        return 1

    # -- structural parsing ------------------------------------------------

    check("parse: ships", len(edsy.ships), 2)
    check("parse: modules", len(edsy.modules), 8)
    check("parse: blueprints", len(edsy.blueprints), 3)
    check("parse: experimental effects", len(edsy.expeffects), 1)
    check("parse: module types", len(edsy.mtypes), 5)

    ship = edsy.ships.get("TestHull")

    check("parse: hull cost", ship.cost if ship else None, 1234567)
    check(
        "parse: internal slot sizes",
        ship.slots.get("internal") if ship else None,
        [5, 4, 3, 2, 1],
    )

    armour = next(
        (m for m in edsy.modules if m.fdname == "TestHull_Armour_Grade1"),
        None,
    )

    check("parse: hull armour found", armour is not None)
    check(
        "parse: hull armour mtype inferred",
        armour.mtype if armour else None,
        "cbh",
    )

    # -- identifier grammar ------------------------------------------------

    parsed = parse_edomh_id("FRAME_SHIFT_DRIVE_OVERCHARGE_5_A_PRE")

    check("grammar: base", parsed.base, "FRAME_SHIFT_DRIVE_OVERCHARGE")
    check("grammar: class", parsed.cls, 5)
    check("grammar: rating", parsed.rating, "A")
    check("grammar: pre flag", "pre" in parsed.flags)

    mkii = parse_edomh_id("THRUSTERS_7_A_MK_II")

    check("grammar: Mk II flag", "mkii" in mkii.flags)
    check("grammar: no stray tokens", mkii.unknown, [])

    turret = parse_edomh_id("PULSE_LASER_1_G_T")

    check("grammar: mount letter", turret.mount, "T")
    check("grammar: rating not eaten by mount", turret.rating, "G")

    # -- module resolution -------------------------------------------------

    index = ModuleIndex(edsy.modules, [])

    def resolves(item: str, hull: str | None = None) -> str:
        try:
            return resolve_module(item, index, hull)[0]
        except Exception as exc:
            return f"<error: {exc}>"

    check(
        "resolve: parenthesised name",
        resolves("CARGO_RACK_4_E"),
        "Int_CargoRack_Size4_Class1",
    )
    check(
        "resolve: mount disambiguates",
        resolves("PULSE_LASER_1_G_T"),
        "Hpt_PulseLaser_Turret_Small",
    )
    check(
        "resolve: free variant",
        resolves("FIGHTER_HANGAR_5_D_FREE"),
        "Int_FighterBay_Size5_Class1_Free",
    )
    check(
        "resolve: non-free variant",
        resolves("FIGHTER_HANGAR_5_D"),
        "Int_FighterBay_Size5_Class1",
    )
    check(
        "resolve: hull armour",
        resolves("TESTHULL_ARMOUR_GRADE_1", "TestHull"),
        "TestHull_Armour_Grade1",
    )
    check(
        "resolve: sizeless module",
        resolves("CARGO_HATCH"),
        "ModularCargoBayDoor",
    )

    # A wrong-size match must fail rather than substitute a near miss.
    check(
        "resolve: refuses wrong size",
        resolves("CARGO_RACK_7_E").startswith("<error:"),
    )

    # -- slot naming -------------------------------------------------------

    if ship:
        check(
            "slots: military interleaved by size",
            optional_slot_names(ship),
            [
                "Slot01_Size5",
                "Slot02_Size4",
                "Military01",
                "Slot03_Size3",
                "Slot04_Size2",
                "Slot05_Size1",
            ],
        )
        check(
            "slots: hardpoints numbered per size",
            hardpoint_slot_names(ship),
            ["LargeHardpoint1", "MediumHardpoint1", "SmallHardpoint1"],
        )
        check(
            "slots: utilities",
            utility_slot_names(ship),
            ["TinyHardpoint1", "TinyHardpoint2"],
        )
        check("slots: core", slot_name("core", 2, ship), "MainEngines")

    # Hulls EDSY gives a slotnames{} override: dedicated bays are named
    # rather than numbered, the ordinary slots around them can number with
    # gaps, and the overrides are positional against the unsorted slots{}
    # array, so they have to survive the merge that interleaves military
    # bays by size.
    named = edsy.ships.get("TestHullNamed")

    check("parse: slotnames read", bool(named and named.slotnames))

    if named:
        check(
            "slots: named bays and gapped numbering",
            optional_slot_names(named),
            [
                "Cargo01",
                "Slot01_Size4",
                "Military01",
                "Slot02_Size3",
                "Slot05_Size2",
                "Slot06_Size1",
            ],
        )
        check(
            "slots: hardpoint override, short list generates the tail",
            hardpoint_slot_names(named),
            ["LargeMiningHardpoint1", "MediumHardpoint1"],
        )
        check(
            "slots: override reaches slot_name()",
            slot_name("optional", 0, named),
            "Cargo01",
        )

    # EDSY writes each attribute's fdattr. Guard against reaching for a
    # display name or an inbound-only alias instead.
    check(
        "modifiers: canonical falloff label",
        MODIFIER_LABELS["DAMAGE_FALLOFF_START"],
        "FalloffRange",
    )
    check(
        "modifiers: canonical emission label",
        MODIFIER_LABELS["TYPICAL_EMISSION_RANGE"],
        "Range",
    )

    # -- engineering, scoped by module type --------------------------------

    booster = next(
        (m for m in edsy.modules if m.mtype == "usb"),
        None,
    )

    check(
        "engineering: Heavy Duty on bulkheads",
        lookup_engineering("HEAVY_DUTY", armour, edsy, "blueprint"),
        "Armour_HeavyDuty",
    )
    check(
        "engineering: same label on shield booster",
        lookup_engineering("HEAVY_DUTY", booster, edsy, "blueprint"),
        "ShieldBooster_HeavyDuty",
    )
    check(
        "engineering: experimental effect",
        lookup_engineering("DEEP_PLATING", armour, edsy, "experimental"),
        "special_armour_chunky",
    )

    # -- end-to-end --------------------------------------------------------

    payload = {
        "event": "ship",
        "version": 2,
        "shipConfiguration": {
            "name": "Selftest",
            "shipType": "TEST_HULL",
            "cargoHatch": {"index": 0, "id": "CARGO_HATCH"},
            "coreSlots": [
                {"index": 0, "id": "TESTHULL_ARMOUR_GRADE_1"},
            ],
            "optionalSlots": [
                {"index": 3, "id": "CARGO_RACK_4_E"},
            ],
            "hardpointSlots": [
                {"index": 2, "id": "PULSE_LASER_1_G_T"},
            ],
            "utilitySlots": [
                {"index": 1, "id": "SHIELD_BOOSTER_0_A"},
            ],
        },
    }

    stderr = sys.stderr

    try:
        # convert_ship narrates progress; the selftest has its own report.
        sys.stderr = io.StringIO()
        loadout = convert_ship(payload, edsy, index, [], [])
        slots = [m["Slot"] for m in loadout["Modules"]]
        error = ""
    except Exception as exc:
        loadout, slots, error = None, [], str(exc)
    finally:
        sys.stderr = stderr

    check(
        "end-to-end: converts" + (f" ({error})" if error else ""),
        loadout is not None,
    )
    check(
        "end-to-end: slot names",
        slots,
        [
            "CargoHatch",
            "Armour",
            "Slot03_Size3",
            "SmallHardpoint1",
            "TinyHardpoint2",
        ],
    )
    check(
        "end-to-end: cargo capacity summed",
        loadout["CargoCapacity"] if loadout else None,
        32,
    )
    check(
        "end-to-end: items lowercased for the Journal",
        all(m["Item"] == m["Item"].lower() for m in loadout["Modules"])
        if loadout
        else False,
    )

    # -- report ------------------------------------------------------------

    failed = [entry for entry in checks if not entry[1]]

    for name, ok, detail in checks:
        if not ok:
            print(f"  FAIL  {name}: {detail}")

    print(
        f"selftest: {len(checks) - len(failed)}/{len(checks)} checks passed"
    )

    if failed:
        print("selftest: FAILED")
        return 1

    print("selftest: OK")
    return 0


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "EDSX — convert EDOMH ship share links to EDSY SLEF files."
        )
    )

    parser.add_argument(
        "inputs",
        nargs="*",
        help=(
            "EDOMH URLs, files containing EDOMH URLs, or previously "
            "decoded .edomh.json payloads"
        ),
    )

    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the version and exit",
    )

    parser.add_argument(
        "--selftest",
        action="store_true",
        help=(
            "Check that reference parsing, module resolution, slot naming "
            "and engineering lookup all work, then exit. Runs offline"
        ),
    )

    parser.add_argument(
        "--refresh-data",
        action="store_true",
        help="Force-refresh all local reference data",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_DIR,
        help=f"SLEF output directory (default: ./{OUTPUT_DIR})",
    )

    parser.add_argument(
        "--raw-output",
        type=Path,
        default=RAW_DIR,
        help=f"Decoded EDOMH output directory (default: ./{RAW_DIR})",
    )

    parser.add_argument(
        "--no-fdevids",
        action="store_true",
        help="Do not download/use FDevIDs; use EDSY data only",
    )

    args = parser.parse_args()

    if args.version:
        print(VERSION)
        return 0

    if args.selftest:
        return selftest()

    sources = read_inputs(args.inputs)

    if not sources:
        print("No URLs supplied.", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    args.raw_output.mkdir(parents=True, exist_ok=True)

    if FAILURE_FILE.exists():
        FAILURE_FILE.unlink()

    # -- reference data ----------------------------------------------------

    print("Loading current EDSY reference data...", file=sys.stderr)

    try:
        edsy_raw = download_reference(
            EDSY_EDDB_URL,
            EDSY_EDDB_CACHE,
            args.refresh_data,
        )
    except Exception as exc:
        print(f"ERROR: Could not obtain EDSY data: {exc}", file=sys.stderr)
        return 1

    edsy = EdsyDatabase(edsy_raw)

    print(
        f"EDSY: {len(edsy.modules)} modules, {len(edsy.ships)} ships, "
        f"{len(edsy.blueprints)} blueprints, "
        f"{len(edsy.expeffects)} experimental effects.",
        file=sys.stderr,
    )

    outfitting: list[dict[str, str]] = []
    shipyard: list[dict[str, str]] = []

    if not args.no_fdevids:
        print("Loading FDevIDs reference data...", file=sys.stderr)

        try:
            outfitting, shipyard = load_fdevids(args.refresh_data)
        except Exception as exc:
            print(f"WARNING: FDevIDs unavailable: {exc}", file=sys.stderr)
            print("Continuing with EDSY data only.", file=sys.stderr)

    print(
        f"FDevIDs: {len(outfitting)} outfitting, {len(shipyard)} ships.",
        file=sys.stderr,
    )

    index = ModuleIndex(edsy.modules, fdevids_modules(outfitting))

    # -- convert -----------------------------------------------------------

    success = 0
    failures = 0

    for number, source in enumerate(sources, 1):
        print(f"\n[{number}/{len(sources)}] {source}", file=sys.stderr)

        edomh: dict[str, Any] | None = None
        warnings: list[str] = []

        try:
            if source.endswith(".edomh.json"):
                edomh, _location = load_edomh_file(Path(source))
                print("  Loaded preserved payload", file=sys.stderr)
            else:
                edomh, location = fetch_edomh_ship(source)

                raw_path = save_raw_edomh(
                    edomh, source, location, args.raw_output
                )

                print(f"  Decoded -> {raw_path}", file=sys.stderr)

            loadout = convert_ship(edomh, edsy, index, shipyard, warnings)

            name = safe_filename(
                loadout.get("ShipName")
                or loadout.get("Ship")
                or "Unnamed Ship"
            )

            output = unique_path(args.output / f"{name}.slef.json")

            output.write_text(
                json.dumps(
                    make_slef(loadout),
                    indent=2,
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )

            for warning in warnings:
                print(f"  WARNING: {warning}", file=sys.stderr)

            print(
                f"  OK -> {output} ({len(loadout['Modules'])} modules)",
                file=sys.stderr,
            )

            success += 1

        except Exception as exc:
            message = str(exc)

            print(f"  ERROR: {message}", file=sys.stderr)

            if edomh is not None:
                print(
                    "  NOTE: decoded EDOMH payload was preserved.",
                    file=sys.stderr,
                )

            append_failure(source, message)

            failures += 1

    print(
        f"\nFinished: {success} succeeded, {failures} failed.",
        file=sys.stderr,
    )

    if failures:
        print(f"Failure report: {FAILURE_FILE}", file=sys.stderr)
        print("\nDecoded payloads are in:", file=sys.stderr)
        print(f"  {args.raw_output}", file=sys.stderr)
        print("\nNo failed ship was written as SLEF.", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
