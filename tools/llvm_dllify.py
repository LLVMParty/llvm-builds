#!/usr/bin/env python3
"""Build an MSVC-compatible LLVM.dll from LLVM static component libraries.

This is an external implementation of the LLVM-DLL.md plan.  It intentionally
lives outside the downloaded LLVM source tree: it consumes an already-built LLVM
static build and emits:

  * bin/LLVM.dll plus lib/LLVM.lib (import library)
  * replacement component .lib files containing resolver thunks
  * optional LLVM-C.dll forwarder

The implementation targets x86_64/AMD64 COFF.  It writes the large thunk and
resolver-table objects directly as COFF so it is not limited by MASM's maximum
identifier/line length for very long MSVC-mangled C++ names.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import struct
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

# COFF constants used by the tiny object writer below.
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_SCN_CNT_CODE = 0x00000020
IMAGE_SCN_CNT_INITIALIZED_DATA = 0x00000040
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_READ = 0x40000000
IMAGE_SCN_MEM_WRITE = 0x80000000
IMAGE_SCN_LNK_INFO = 0x00000200
IMAGE_SCN_LNK_REMOVE = 0x00000800
IMAGE_SCN_LNK_COMDAT = 0x00001000
IMAGE_SCN_ALIGN_1BYTES = 0x00100000
IMAGE_SCN_ALIGN_8BYTES = 0x00400000
IMAGE_SCN_ALIGN_16BYTES = 0x00500000
IMAGE_SYM_UNDEFINED = 0
IMAGE_SYM_CLASS_EXTERNAL = 2
IMAGE_SYM_CLASS_STATIC = 3
IMAGE_SYM_DTYPE_FUNCTION = 0x20
IMAGE_REL_AMD64_ADDR64 = 0x0001
IMAGE_REL_AMD64_REL32 = 0x0004

TEXT_CHARS = IMAGE_SCN_CNT_CODE | IMAGE_SCN_MEM_EXECUTE | IMAGE_SCN_MEM_READ | IMAGE_SCN_ALIGN_16BYTES
TEXT_COMDAT_CHARS = TEXT_CHARS | IMAGE_SCN_LNK_COMDAT
RDATA_CHARS = IMAGE_SCN_CNT_INITIALIZED_DATA | IMAGE_SCN_MEM_READ | IMAGE_SCN_ALIGN_16BYTES
DATA_CHARS = IMAGE_SCN_CNT_INITIALIZED_DATA | IMAGE_SCN_MEM_READ | IMAGE_SCN_MEM_WRITE | IMAGE_SCN_ALIGN_8BYTES
CRT_CHARS = IMAGE_SCN_CNT_INITIALIZED_DATA | IMAGE_SCN_MEM_READ | IMAGE_SCN_ALIGN_8BYTES
DRECTVE_CHARS = IMAGE_SCN_LNK_INFO | IMAGE_SCN_LNK_REMOVE | IMAGE_SCN_ALIGN_1BYTES


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def die(message: str) -> None:
    raise SystemExit(f"error: {message}")


def msvc_env() -> dict[str, str]:
    """Return an environment that does not let MSYS rewrite /Fo or /OUT args."""
    env = os.environ.copy()
    env.setdefault("MSYS2_ARG_CONV_EXCL", "*")
    return env


def quote_rsp_arg(arg: str | os.PathLike[str]) -> str:
    s = str(arg)
    if not s:
        return '""'
    if any(ch.isspace() for ch in s) or any(ch in s for ch in '"'):
        return '"' + s.replace('"', r'\"') + '"'
    return s


def run(cmd: Sequence[str | os.PathLike[str]], *, cwd: Path | None = None, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    if not quiet:
        print("+", " ".join(quote_rsp_arg(str(c)) for c in cmd))
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, check=True, text=True, env=msvc_env())


def capture(cmd: Sequence[str | os.PathLike[str]]) -> str:
    p = subprocess.run([str(c) for c in cmd], check=False, text=True, capture_output=True, env=msvc_env())
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(map(str, cmd))}\n{p.stderr}")
    return p.stdout


def which(names: Iterable[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_vs_tool(names: Sequence[str], explicit: str | None = None) -> str:
    if explicit:
        return explicit
    found = which(names)
    if found:
        return found
    die(f"could not find any of: {', '.join(names)}. Run from a VS developer environment or pass the tool path explicitly.")


def read_libsfile(path: Path) -> list[Path]:
    libs: list[Path] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().strip('"')
        if not line:
            continue
        libs.append(Path(line))
    return libs


def dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = str(p.resolve()).lower() if p.exists() else str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def is_c_api_symbol(name: str) -> bool:
    # On x64 Windows C exports are undecorated.  Keep the predicate deliberately
    # narrow for LLVM.dll; clang_* is used when the same machinery is reused for
    # clang-cpp.dll/libclang work later.
    return not name.startswith("?") and (name.startswith("LLVM") or name.startswith("clang_"))


def is_exportable_data_symbol(name: str) -> bool:
    """Filter COFF data symbols down to the ones useful across a DLL boundary.

    llvm-nm reports tens of thousands of public string-literal COMDATs as R data
    (??_C@...).  Exporting those would waste PE export slots and is not needed
    for consumers.  RTTI, vtables, static members, and exception metadata are
    kept so C++ consumers can link and RTTI/dynamic_cast can work across the
    DLL boundary.
    """
    if name.startswith("??_C@"):  # compiler-generated string literal COMDAT
        return False
    if name.startswith("__IMPORT_DESCRIPTOR") or name.startswith("__NULL_IMPORT"):
        return False
    if name.startswith("__imp_"):
        return False
    if name in {"@feat.00", "@comp.id"}:
        return False
    if name.startswith("??_7") or name.startswith("??_R"):
        return True
    if name.startswith("?"):
        # Static data members and other MSVC-decorated data.  Exclude dynamic
        # initializer/atexit helper names; they are internal implementation
        # details and should not be imported by consumers.
        if name.startswith("??__"):
            return False
        return True
    if name.startswith(("_CT", "_CTA", "_TI", "_CTA", "_Catchable", "_TypeDescriptor")):
        return True
    # LLVM has a small number of unmangled globals (for example target module
    # anchors) that are real data definitions.
    return True


def fnv1a32(name: str) -> int:
    h = 0x811C9DC5
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


@dataclasses.dataclass(frozen=True)
class SymbolDef:
    name: str
    type_code: str
    lib: str


@dataclasses.dataclass
class SymbolInventory:
    per_lib: dict[str, dict[str, str]]
    owners: dict[str, str]
    functions: set[str]
    c_api: set[str]
    data: set[str]
    raw_type_counts: Counter[str]
    data_sizes: dict[str, int] = dataclasses.field(default_factory=dict)

    def manifest(self) -> dict[str, object]:
        duplicate_count = 0
        seen: dict[str, int] = defaultdict(int)
        for syms in self.per_lib.values():
            for name in syms:
                seen[name] += 1
        duplicate_count = sum(1 for count in seen.values() if count > 1)
        return {
            "libraries": len(self.per_lib),
            "raw_type_counts": dict(sorted(self.raw_type_counts.items())),
            "unique_symbols": len(self.owners),
            "duplicate_symbols": duplicate_count,
            "functions_for_resolver": len(self.functions),
            "c_api_exports": len(self.c_api),
            "data_exports": len(self.data),
            "data_proxy_bytes": sum(self.data_sizes.get(sym, 0) for sym in self.data),
            "data_proxy_symbols_sized": sum(1 for sym in self.data if sym in self.data_sizes),
            "total_named_exports": len(self.c_api) + len(self.data) + 1,
        }


def extract_symbols_from_lib(nm: str, lib: Path) -> dict[str, str]:
    out = capture([nm, "-P", "--defined-only", "--extern-only", str(lib)])
    symbols: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line.endswith(":"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, type_code = parts[0], parts[1]
        if not name:
            continue
        # Keep first type seen within this library.  COMDAT duplicates in one
        # archive can appear more than once; their public symbol name is what
        # matters for linking.
        symbols.setdefault(name, type_code)
    return symbols


def build_inventory(nm: str, libs: Sequence[Path], *, compute_data_sizes: bool = False) -> SymbolInventory:
    per_lib: dict[str, dict[str, str]] = {}
    owners: dict[str, str] = {}
    raw_type_counts: Counter[str] = Counter()

    for lib in libs:
        lib_name = lib.name
        symbols = extract_symbols_from_lib(nm, lib)
        per_lib[lib_name] = symbols
        raw_type_counts.update(symbols.values())
        for name in symbols:
            owners.setdefault(name, lib_name)

    c_api: set[str] = set()
    functions: set[str] = set()
    data: set[str] = set()
    for name, owner in owners.items():
        typ = per_lib[owner][name]
        upper = typ.upper()
        if upper == "T":
            if is_c_api_symbol(name):
                c_api.add(name)
            else:
                functions.add(name)
        elif is_c_api_symbol(name):
            # Defensive: LLVM C API should be code, but exporting it by name is
            # still the desired ABI if a tool reports an unusual code.
            c_api.add(name)
        elif upper in {"R", "D", "B", "S", "G"} and is_exportable_data_symbol(name):
            data.add(name)

    data_sizes = collect_data_symbol_sizes(libs, owners, data) if compute_data_sizes else {}
    return SymbolInventory(
        per_lib=per_lib,
        owners=owners,
        functions=functions,
        c_api=c_api,
        data=data,
        raw_type_counts=raw_type_counts,
        data_sizes=data_sizes,
    )


def iter_archive_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield COFF object members from a regular COFF archive (.lib)."""
    data = path.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        yield path.name, data
        return

    pos = 8
    longnames = b""
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        pos += 60
        raw_name = header[0:16].decode("ascii", errors="ignore").strip()
        try:
            size = int(header[48:58].decode("ascii", errors="ignore").strip() or "0")
        except ValueError:
            break
        member = data[pos:pos + size]
        pos += size + (size & 1)

        name = raw_name
        if name == "//":
            longnames = member
            continue
        if name == "/" or name.startswith("/ "):
            # Linker member / symbol table.
            continue
        if name.startswith("/") and name[1:].isdigit() and longnames:
            off = int(name[1:])
            end = longnames.find(b"/\n", off)
            if end < 0:
                end = longnames.find(b"\0", off)
            if end < 0:
                end = len(longnames)
            name = longnames[off:end].decode("utf-8", errors="replace")
        elif name.endswith("/"):
            name = name[:-1]

        if len(member) >= 2:
            machine = struct.unpack_from("<H", member, 0)[0]
            if machine == IMAGE_FILE_MACHINE_AMD64:
                yield name, member


def _coff_name(raw: bytes, string_table: bytes) -> str:
    if raw[:4] == b"\0\0\0\0":
        off = struct.unpack_from("<I", raw, 4)[0]
        if off >= 4 and off < len(string_table):
            end = string_table.find(b"\0", off)
            if end < 0:
                end = len(string_table)
            return string_table[off:end].decode("utf-8", errors="replace")
        return ""
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def coff_data_symbol_sizes(obj: bytes, wanted: set[str]) -> dict[str, int]:
    """Infer sizes for public COFF data symbols in one object file.

    COFF does not store a size on ordinary symbols, so the best available
    object-level estimate is the distance to the next symbol in the same section
    (or to the end of the section).  This is exact for the common LLVM/MSVC
    cases: COMDAT data, vtables/RTTI, and simple globals.
    """
    if len(obj) < 20:
        return {}
    machine, nsec, _time, sym_ptr, nsym, opt_size, _chars = struct.unpack_from("<HHIIIHH", obj, 0)
    if machine != IMAGE_FILE_MACHINE_AMD64 or sym_ptr <= 0 or nsym <= 0:
        return {}
    sec_base = 20 + opt_size
    if sec_base + nsec * 40 > len(obj):
        return {}

    section_sizes: dict[int, int] = {}
    for i in range(nsec):
        off = sec_base + i * 40
        size_of_raw_data = struct.unpack_from("<I", obj, off + 16)[0]
        section_sizes[i + 1] = size_of_raw_data

    sym_end = sym_ptr + nsym * 18
    if sym_end + 4 > len(obj):
        return {}
    string_table = obj[sym_end:]

    by_section: dict[int, list[int]] = defaultdict(list)
    candidates: list[tuple[str, int, int, int]] = []
    i = 0
    while i < nsym:
        off = sym_ptr + i * 18
        entry = obj[off:off + 18]
        name = _coff_name(entry[:8], string_table)
        value, section_number, typ, storage_class, aux_count = struct.unpack_from("<IhHBB", entry, 8)
        if section_number > 0 and section_number in section_sizes:
            by_section[section_number].append(value)
            is_function = (typ & IMAGE_SYM_DTYPE_FUNCTION) == IMAGE_SYM_DTYPE_FUNCTION
            if storage_class == IMAGE_SYM_CLASS_EXTERNAL and not is_function and name in wanted:
                candidates.append((name, section_number, value, section_sizes[section_number]))
        i += 1 + aux_count

    for sec_no, offsets in by_section.items():
        offsets.append(section_sizes[sec_no])
        offsets[:] = sorted(set(o for o in offsets if 0 <= o <= section_sizes[sec_no]))

    sizes: dict[str, int] = {}
    for name, sec_no, value, sec_size in candidates:
        offsets = by_section.get(sec_no, [sec_size])
        next_offsets = [o for o in offsets if o > value]
        if next_offsets:
            size = next_offsets[0] - value
        else:
            size = sec_size - value
        sizes[name] = max(1, size)
    return sizes


def collect_data_symbol_sizes(libs: Sequence[Path], owners: dict[str, str], data_symbols: set[str]) -> dict[str, int]:
    wanted_by_lib: dict[str, set[str]] = defaultdict(set)
    for sym in data_symbols:
        owner = owners.get(sym)
        if owner:
            wanted_by_lib[owner].add(sym)

    sizes: dict[str, int] = {}
    for lib in libs:
        wanted = wanted_by_lib.get(lib.name)
        if not wanted:
            continue
        remaining = set(wanted)
        for _member_name, obj in iter_archive_members(lib):
            found = coff_data_symbol_sizes(obj, remaining)
            sizes.update(found)
            remaining.difference_update(found)
            if not remaining:
                break
        for sym in remaining:
            # Keep the build moving if a size could not be inferred (for example
            # from an unusual object member).  Eight bytes is safe for address-only
            # uses and the manifest exposes how many symbols used a fallback.
            sizes.setdefault(sym, 8)
    return sizes


def verify_hashes(symbols: Iterable[str]) -> dict[int, str]:
    seen: dict[int, str] = {}
    for sym in symbols:
        h = fnv1a32(sym)
        other = seen.get(h)
        if other is not None and other != sym:
            die(f"FNV-1a 32-bit collision: {other!r} and {sym!r} -> 0x{h:08x}")
        seen[h] = sym
    return seen


@dataclasses.dataclass
class CoffReloc:
    offset: int
    symbol: str
    type: int


@dataclasses.dataclass
class CoffSection:
    name: str
    data: bytearray
    characteristics: int
    relocs: list[CoffReloc] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CoffSymbol:
    name: str
    value: int
    section_number: int
    type: int
    storage_class: int
    aux: bytes = b""


class CoffObject:
    def __init__(self) -> None:
        self.sections: list[CoffSection] = []
        self.symbols: list[CoffSymbol] = []
        self.symbol_index: dict[str, int] = {}
        self._next_symbol_index = 0

    def add_section(self, name: str, characteristics: int) -> int:
        if len(name) > 8:
            die(f"internal error: section name {name!r} is longer than 8 bytes; long section names are not implemented")
        self.sections.append(CoffSection(name=name, data=bytearray(), characteristics=characteristics))
        return len(self.sections)  # COFF section numbers are 1-based.

    def section(self, section_number: int) -> CoffSection:
        return self.sections[section_number - 1]

    def _append_symbol(self, sym: CoffSymbol, *, index_name: str | None = None) -> None:
        if len(sym.aux) % 18:
            die(f"internal error: aux symbol data for {sym.name} is not 18-byte aligned")
        if index_name is not None:
            if index_name in self.symbol_index:
                die(f"duplicate symbol in generated object: {index_name}")
            self.symbol_index[index_name] = self._next_symbol_index
        self.symbols.append(sym)
        self._next_symbol_index += 1 + (len(sym.aux) // 18)

    def define(self, name: str, section_number: int, value: int, *, external: bool = True, function: bool = False) -> None:
        typ = IMAGE_SYM_DTYPE_FUNCTION if function else 0
        storage = IMAGE_SYM_CLASS_EXTERNAL if external else IMAGE_SYM_CLASS_STATIC
        self._append_symbol(CoffSymbol(name, value, section_number, typ, storage), index_name=name)

    def define_comdat_section_symbol(self, section_number: int, *, selection: int = 2, associative_section: int = 0) -> None:
        sec = self.section(section_number)
        aux = struct.pack("<LHHIHB3s", len(sec.data), len(sec.relocs), 0, 0, associative_section, selection, b"\0\0\0")
        # Multiple COFF sections can legitimately have the same name, so do not
        # add section symbols to symbol_index.
        self._append_symbol(CoffSymbol(sec.name, 0, section_number, 0, IMAGE_SYM_CLASS_STATIC, aux))

    def extern(self, name: str, *, function: bool = False) -> None:
        if name in self.symbol_index:
            return
        typ = IMAGE_SYM_DTYPE_FUNCTION if function else 0
        self._append_symbol(CoffSymbol(name, 0, IMAGE_SYM_UNDEFINED, typ, IMAGE_SYM_CLASS_EXTERNAL), index_name=name)

    def add_reloc(self, section_number: int, offset: int, symbol: str, reloc_type: int) -> None:
        self.section(section_number).relocs.append(CoffReloc(offset, symbol, reloc_type))

    def _add_string(self, strings: bytearray, offsets: dict[str, int], s: str) -> int:
        if s not in offsets:
            offsets[s] = 4 + len(strings)
            strings.extend(s.encode("utf-8") + b"\0")
        return offsets[s]

    def write(self, path: Path) -> None:
        strings = bytearray()
        string_offsets: dict[str, int] = {}

        def name_field(name: str) -> bytes:
            b = name.encode("utf-8")
            if len(b) <= 8:
                return b + b"\0" * (8 - len(b))
            off = self._add_string(strings, string_offsets, name)
            return struct.pack("<II", 0, off)

        # Reserve string-table entries for all long names before headers/symbols
        # are serialized.  (All section names currently fit in 8 bytes.)
        for sym in self.symbols:
            if len(sym.name.encode("utf-8")) > 8:
                self._add_string(strings, string_offsets, sym.name)

        file_header_size = 20
        section_header_size = 40 * len(self.sections)
        cursor = file_header_size + section_header_size
        raw_ptrs: list[int] = []
        reloc_ptrs: list[int] = []
        reloc_counts: list[int] = []

        for sec in self.sections:
            if sec.data:
                cursor = align(cursor, 4)
                raw_ptrs.append(cursor)
                cursor += len(sec.data)
            else:
                raw_ptrs.append(0)
            if sec.relocs:
                cursor = align(cursor, 4)
                reloc_ptrs.append(cursor)
                reloc_counts.append(len(sec.relocs))
                cursor += 10 * len(sec.relocs)
            else:
                reloc_ptrs.append(0)
                reloc_counts.append(0)

        symbol_table_ptr = align(cursor, 4)
        number_of_symbols = self._next_symbol_index
        string_table = struct.pack("<I", 4 + len(strings)) + bytes(strings)

        with path.open("wb") as f:
            f.write(struct.pack(
                "<HHIIIHH",
                IMAGE_FILE_MACHINE_AMD64,
                len(self.sections),
                0,
                symbol_table_ptr,
                number_of_symbols,
                0,
                0,
            ))

            for idx, sec in enumerate(self.sections):
                nreloc = reloc_counts[idx]
                if nreloc > 0xFFFF:
                    die(f"section {sec.name} has {nreloc} relocations; split the generated object into smaller sections")
                f.write(struct.pack(
                    "<8sIIIIIIHHI",
                    name_field(sec.name),
                    0,
                    0,
                    len(sec.data),
                    raw_ptrs[idx],
                    reloc_ptrs[idx],
                    0,
                    nreloc,
                    0,
                    sec.characteristics,
                ))

            for idx, sec in enumerate(self.sections):
                if sec.data:
                    pad_to(f, raw_ptrs[idx])
                    f.write(sec.data)
                if sec.relocs:
                    pad_to(f, reloc_ptrs[idx])
                    for reloc in sec.relocs:
                        try:
                            sym_index = self.symbol_index[reloc.symbol]
                        except KeyError:
                            die(f"internal error: relocation references unknown symbol {reloc.symbol}")
                        f.write(struct.pack("<IIH", reloc.offset, sym_index, reloc.type))

            pad_to(f, symbol_table_ptr)
            for sym in self.symbols:
                aux_count = len(sym.aux) // 18
                f.write(name_field(sym.name))
                f.write(struct.pack("<IhHBB", sym.value, sym.section_number, sym.type, sym.storage_class, aux_count))
                if sym.aux:
                    f.write(sym.aux)
            f.write(string_table)


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def pad_to(f, offset: int) -> None:
    pos = f.tell()
    if pos > offset:
        die(f"internal error: wrote past requested offset {offset}")
    if pos < offset:
        f.write(b"\0" * (offset - pos))


def make_resolver_source(path: Path) -> None:
    path.write_text(
        r'''#include <stdint.h>
#include <stddef.h>

struct SymEntry {
  uint32_t hash;
  uintptr_t addr;
};

extern const struct SymEntry __llvm_symtab[];
extern const uint32_t __llvm_symtab_count;

#ifdef _MSC_VER
__declspec(dllexport)
#endif
void *__cdecl __llvm_resolve(uint32_t hash) {
  uint32_t lo = 0;
  uint32_t hi = __llvm_symtab_count;
  while (lo < hi) {
    uint32_t mid = lo + ((hi - lo) >> 1);
    if (__llvm_symtab[mid].hash < hash)
      lo = mid + 1;
    else
      hi = mid;
  }
  if (lo < __llvm_symtab_count && __llvm_symtab[lo].hash == hash)
    return (void *)__llvm_symtab[lo].addr;
  return (void *)0;
}
''',
        encoding="utf-8",
    )


def make_symtab_obj(path: Path, functions: Sequence[str]) -> None:
    entries = sorted((fnv1a32(sym), sym) for sym in functions)
    coff = CoffObject()
    chunk_size = 60000
    first_section: int | None = None
    for chunk_index in range(0, len(entries), chunk_size):
        chunk = entries[chunk_index:chunk_index + chunk_size]
        suffix = chr(ord('A') + chunk_index // chunk_size)
        sec_no = coff.add_section(f".rdata${suffix}", RDATA_CHARS)
        if first_section is None:
            first_section = sec_no
            coff.define("__llvm_symtab", sec_no, 0, external=True, function=False)
        sec = coff.section(sec_no)
        for h, sym in chunk:
            off = len(sec.data)
            sec.data.extend(struct.pack("<IIQ", h, 0, 0))
            coff.extern(sym, function=True)
            coff.add_reloc(sec_no, off + 8, sym, IMAGE_REL_AMD64_ADDR64)

    count_sec = coff.add_section(".rdata$Z", RDATA_CHARS)
    coff.define("__llvm_symtab_count", count_sec, 0, external=True, function=False)
    coff.section(count_sec).data.extend(struct.pack("<I", len(entries)))
    coff.write(path)


def append_rel32_call(coff: CoffObject, sec_no: int, target: str) -> None:
    sec = coff.section(sec_no)
    off = len(sec.data)
    sec.data.extend(b"\xE8\x00\x00\x00\x00")  # call rel32
    coff.extern(target, function=True)
    coff.add_reloc(sec_no, off + 1, target, IMAGE_REL_AMD64_REL32)


def append_rip_store_rax(coff: CoffObject, sec_no: int, target: str) -> None:
    sec = coff.section(sec_no)
    off = len(sec.data)
    sec.data.extend(b"\x48\x89\x05\x00\x00\x00\x00")  # mov qword ptr [rip+rel32], rax
    coff.add_reloc(sec_no, off + 3, target, IMAGE_REL_AMD64_REL32)


def append_rip_jmp(coff: CoffObject, sec_no: int, target: str) -> None:
    sec = coff.section(sec_no)
    off = len(sec.data)
    sec.data.extend(b"\xFF\x25\x00\x00\x00\x00")  # jmp qword ptr [rip+rel32]
    coff.add_reloc(sec_no, off + 2, target, IMAGE_REL_AMD64_REL32)


def make_stub_obj(path: Path, lib_stem: str, functions: Sequence[str], resolver_helper_name: str) -> None:
    coff = CoffObject()
    init_text = coff.add_section(".text", TEXT_CHARS)
    data = coff.add_section(".data", DATA_CHARS)
    crt = coff.add_section(".CRT$XCU", CRT_CHARS)

    safe_stem = ''.join(ch if ch.isalnum() else '_' for ch in lib_stem)
    init_name = f"__llvm_dllify_init_{safe_stem}"
    slot_names: list[tuple[str, str, int]] = []

    data_sec = coff.section(data)
    for i, sym in enumerate(functions):
        slot = f"$llvmdll${safe_stem}${i:08x}"
        slot_off = len(data_sec.data)
        data_sec.data.extend(b"\0" * 8)
        coff.define(slot, data, slot_off, external=False, function=False)
        slot_names.append((sym, slot, fnv1a32(sym)))

    # Put every thunk in its own pick-any COMDAT section.  MSVC/clang-cl emit
    # many inline/template functions as COMDATs in consumers; making thunks
    # COMDAT too lets the linker discard duplicate local inline copies instead
    # of failing with duplicate-symbol errors when a component stub object is
    # extracted.
    for sym, slot, _h in slot_names:
        thunk_text = coff.add_section(".text$mn", TEXT_COMDAT_CHARS)
        append_rip_jmp(coff, thunk_text, slot)
        coff.define_comdat_section_symbol(thunk_text, selection=2)
        coff.define(sym, thunk_text, 0, external=True, function=True)

    text_sec = coff.section(init_text)
    init_off = len(text_sec.data)
    coff.define(init_name, init_text, init_off, external=False, function=True)
    # Windows x64 ABI: reserve 32-byte shadow space and keep stack 16-byte aligned.
    text_sec.data.extend(b"\x48\x83\xEC\x28")  # sub rsp, 40
    for _sym, slot, h in slot_names:
        text_sec.data.extend(b"\xB9" + struct.pack("<I", h))  # mov ecx, imm32
        append_rel32_call(coff, init_text, resolver_helper_name)
        append_rip_store_rax(coff, init_text, slot)
    text_sec.data.extend(b"\x48\x83\xC4\x28\xC3")  # add rsp, 40; ret

    crt_sec = coff.section(crt)
    crt_sec.data.extend(b"\0" * 8)
    coff.add_reloc(crt, 0, init_name, IMAGE_REL_AMD64_ADDR64)
    coff.write(path)


def make_resolve_helper_source(path: Path, helper_name: str) -> None:
    path.write_text(
        f'''#include <stdint.h>\n#include <windows.h>\n\n'''
        f'''typedef void *(__cdecl *__llvm_dllify_resolver_t)(uint32_t);\n\n'''
        f'''void *__cdecl {helper_name}(uint32_t hash) {{\n'''
        f'''  static __llvm_dllify_resolver_t resolver;\n'''
        f'''  if (!resolver) {{\n'''
        f'''    HMODULE h = GetModuleHandleA("LLVM.dll");\n'''
        f'''    if (!h) h = LoadLibraryA("LLVM.dll");\n'''
        f'''    if (h) resolver = (__llvm_dllify_resolver_t)GetProcAddress(h, "__llvm_resolve");\n'''
        f'''  }}\n'''
        f'''  return resolver ? resolver(hash) : (void *)0;\n'''
        f'''}}\n''',
        encoding="utf-8",
    )


def make_dummy_obj(path: Path) -> None:
    coff = CoffObject()
    text = coff.add_section(".text", TEXT_CHARS)
    coff.define("$llvmdll_forwarder_dummy", text, 0, external=False, function=True)
    coff.section(text).data.extend(b"\xC3")
    coff.write(path)


def make_data_proxy_obj(path: Path, lib_stem: str, data_sizes: dict[str, int]) -> tuple[str, str, str]:
    """Create a COFF object defining local mirrors for exported DLL data.

    MSVC import libraries cannot transparently satisfy non-dllimport data
    references.  These proxy definitions make existing component libraries
    linkable without header annotations; a CRT initializer copies the initial
    bytes from LLVM.dll into each proxy before main().
    """
    coff = CoffObject()
    data_sec_no = coff.add_section(".data", DATA_CHARS)
    rdata_sec_no = coff.add_section(".rdata", RDATA_CHARS)
    safe_stem = ''.join(ch if ch.isalnum() else '_' for ch in lib_stem)
    table_name = f"__llvm_dllify_data_entries_{safe_stem}"
    count_name = f"__llvm_dllify_data_count_{safe_stem}"
    anchor_name = f"__llvm_dllify_data_anchor_{safe_stem}"

    # If a consumer only references data from this component, the linker will
    # extract this proxy object but not necessarily the companion CRT initializer
    # object.  A .drectve /include forces that helper member into the link.
    drectve = coff.add_section(".drectve", DRECTVE_CHARS)
    coff.section(drectve).data.extend(f" /include:{anchor_name}".encode("ascii"))

    data_sec = coff.section(data_sec_no)
    for sym, size in sorted(data_sizes.items()):
        while len(data_sec.data) % 16:
            data_sec.data.append(0)
        off = len(data_sec.data)
        coff.define(sym, data_sec_no, off, external=True, function=False)
        data_sec.data.extend(b"\0" * max(1, size))

    rdata_sec = coff.section(rdata_sec_no)
    string_labels: dict[str, str] = {}
    for i, sym in enumerate(sorted(data_sizes)):
        label = f"$llvmdll_name${safe_stem}${i:08x}"
        while len(rdata_sec.data) % 1:
            rdata_sec.data.append(0)
        string_labels[sym] = label
        coff.define(label, rdata_sec_no, len(rdata_sec.data), external=False, function=False)
        rdata_sec.data.extend(sym.encode("utf-8") + b"\0")

    while len(rdata_sec.data) % 8:
        rdata_sec.data.append(0)
    coff.define(table_name, rdata_sec_no, len(rdata_sec.data), external=True, function=False)
    for sym, size in sorted(data_sizes.items()):
        off = len(rdata_sec.data)
        rdata_sec.data.extend(struct.pack("<QQII", 0, 0, max(1, size), 0))
        coff.add_reloc(rdata_sec_no, off, sym, IMAGE_REL_AMD64_ADDR64)
        coff.add_reloc(rdata_sec_no, off + 8, string_labels[sym], IMAGE_REL_AMD64_ADDR64)

    while len(rdata_sec.data) % 4:
        rdata_sec.data.append(0)
    coff.define(count_name, rdata_sec_no, len(rdata_sec.data), external=True, function=False)
    rdata_sec.data.extend(struct.pack("<I", len(data_sizes)))
    coff.write(path)
    return table_name, count_name, anchor_name


def make_data_helper_source(path: Path, table_name: str, count_name: str, anchor_name: str) -> None:
    path.write_text(
        f'''#include <stdint.h>\n#include <string.h>\n#include <windows.h>\n\n'''
        f'''struct __llvm_dllify_data_entry {{\n  void *dst;\n  const char *name;\n  uint32_t size;\n  uint32_t reserved;\n}};\n\n'''
        f'''extern const struct __llvm_dllify_data_entry {table_name}[];\n'''
        f'''extern const uint32_t {count_name};\n\n'''
        f'''static void __cdecl __llvm_dllify_copy_data(void);\n'''
        f'''void *{anchor_name} = (void *)&__llvm_dllify_copy_data;\n\n'''
        f'''static void __cdecl __llvm_dllify_copy_data(void) {{\n'''
        f'''  HMODULE h = GetModuleHandleA("LLVM.dll");\n'''
        f'''  if (!h) h = LoadLibraryA("LLVM.dll");\n'''
        f'''  if (!h) return;\n'''
        f'''  for (uint32_t i = 0; i < {count_name}; ++i) {{\n'''
        f'''    const struct __llvm_dllify_data_entry *e = &{table_name}[i];\n'''
        f'''    void *src = (void *)GetProcAddress(h, e->name);\n'''
        f'''    if (src) memcpy(e->dst, src, e->size);\n'''
        f'''  }}\n'''
        f'''}}\n\n'''
        f'''#pragma section(".CRT$XCU", read)\n'''
        f'''__declspec(allocate(".CRT$XCU"))\n'''
        f'''static void (__cdecl *__llvm_dllify_copy_data_init)(void) = __llvm_dllify_copy_data;\n''',
        encoding="utf-8",
    )


def compile_c(cl: str, src: Path, obj: Path, *, optimize: bool = True) -> None:
    flags = ["/O2"] if optimize else []
    run([cl, "/nologo", "/c", "/MD", *flags, f"/Fo{obj}", str(src)])


def write_def(path: Path, dll_name: str, c_api: Sequence[str], data: Sequence[str], resolver_name: str = "__llvm_resolve") -> None:
    lines = [f"LIBRARY {dll_name}", "EXPORTS", f"    {resolver_name}"]
    for sym in sorted(c_api):
        lines.append(f"    {sym}")
    for sym in sorted(data):
        lines.append(f"    {sym} DATA")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_forwarder_def(path: Path, dll_name: str, target_dll: str, exports: Sequence[str]) -> None:
    lines = [f"LIBRARY {dll_name}", "EXPORTS"]
    for sym in sorted(exports):
        lines.append(f"    {sym}={target_dll}.{sym}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_response_file(path: Path, args: Sequence[str | Path]) -> None:
    path.write_text("\n".join(quote_rsp_arg(str(a)) for a in args) + "\n", encoding="utf-8")


def compile_resolver(cl: str, src: Path, obj: Path) -> None:
    run([cl, "/nologo", "/c", "/O2", "/MD", f"/Fo{obj}", str(src)])


def link_dll(
    linker: str,
    out_dll: Path,
    implib: Path,
    def_file: Path,
    objects: Sequence[Path],
    libs: Sequence[Path],
    extra_libs: Sequence[str],
    work_dir: Path,
    *,
    whole_archive: bool = False,
    no_entry: bool = False,
) -> None:
    rsp = work_dir / f"{out_dll.stem}.link.rsp"
    args: list[str | Path] = [
        "/NOLOGO",
        "/DLL",
        "/MACHINE:X64",
        "/INCREMENTAL:NO",
        f"/OUT:{out_dll}",
        f"/IMPLIB:{implib}",
        f"/DEF:{def_file}",
    ]
    if no_entry:
        args.append("/NOENTRY")
    args += list(objects)
    if whole_archive:
        args += [f"/WHOLEARCHIVE:{lib}" for lib in libs]
    else:
        args += list(libs)
    args += list(extra_libs)
    write_response_file(rsp, args)
    run([linker, f"@{rsp}"])


def make_lib(lib_tool: str, out_lib: Path, members: Sequence[Path]) -> None:
    if out_lib.exists():
        out_lib.unlink()
    rsp = out_lib.with_suffix(out_lib.suffix + ".rsp")
    args: list[str | Path] = ["/NOLOGO", f"/OUT:{out_lib}"] + list(members)
    write_response_file(rsp, args)
    run([lib_tool, f"@{rsp}"], quiet=True)


def owner_functions_for_lib(inventory: SymbolInventory, lib_name: str) -> list[str]:
    out: list[str] = []
    for sym, typ in inventory.per_lib.get(lib_name, {}).items():
        if inventory.owners.get(sym) == lib_name and sym in inventory.functions:
            out.append(sym)
    return sorted(out)


def default_system_libs() -> list[str]:
    # Matches the system libraries LLVM's own MSVC build uses for LLVM-C.dll.
    return [
        "psapi.lib",
        "shell32.lib",
        "ole32.lib",
        "uuid.lib",
        "advapi32.lib",
        "ws2_32.lib",
        "ntdll.lib",
        "delayimp.lib",
        "/delayload:shell32.dll",
        "/delayload:ole32.dll",
    ]


def write_manifest(path: Path, inventory: SymbolInventory, libs: Sequence[Path], outputs: dict[str, str]) -> None:
    data = inventory.manifest()
    data.update({
        "input_libraries": [str(p) for p in libs],
        "outputs": outputs,
        "hash": "fnv1a32",
        "arch": "x86_64-pc-windows-msvc",
        "notes": [
            "Non-C API functions are resolved through __llvm_resolve(hash).",
            "C API functions and selected data symbols are real PE exports.",
            "Component .lib files contain owner-only thunks/data proxies and load LLVM.dll directly; consumers keep linking LLVMCore/LLVMIRReader/etc.",
        ],
    })
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def command_census(args: argparse.Namespace) -> None:
    libs = discover_libs(args)
    nm = find_vs_tool(["llvm-nm.exe", "llvm-nm"], args.nm)
    inventory = build_inventory(nm, libs)
    verify_hashes(inventory.functions)
    print(json.dumps(inventory.manifest(), indent=2, sort_keys=True))


def discover_libs(args: argparse.Namespace) -> list[Path]:
    if args.libsfile:
        libs = read_libsfile(Path(args.libsfile))
    elif args.lib_dir:
        lib_dir = Path(args.lib_dir)
        # pathlib's Windows globbing is case-insensitive, so filter the basename
        # explicitly.  Lowercase llvm-*.lib files are import libraries for tools
        # such as llvm-jitlink-executor.exe, not LLVM component archives.
        libs = sorted(
            p for p in lib_dir.glob("*.lib")
            if p.name.startswith("LLVM") and p.name not in {"LLVM.lib", "LLVM-C.lib"}
        )
    else:
        die("pass --libsfile or --lib-dir")
    libs = dedupe_paths(libs)
    missing = [str(p) for p in libs if not p.exists()]
    if missing:
        die("missing input libraries:\n  " + "\n  ".join(missing[:20]))
    return libs


def command_build(args: argparse.Namespace) -> None:
    libs = discover_libs(args)
    nm = find_vs_tool(["llvm-nm.exe", "llvm-nm"], args.nm)
    cl = find_vs_tool(["cl.exe", "cl"], args.cl)
    linker = find_vs_tool(["lld-link.exe", "link.exe"], args.link)
    lib_tool = find_vs_tool(["lib.exe", "llvm-lib.exe", "llvm-lib"], args.lib_tool)

    out_root = Path(args.output_dir)
    bin_dir = Path(args.bin_dir) if args.bin_dir else out_root / "bin"
    lib_dir = Path(args.out_lib_dir) if args.out_lib_dir else out_root / "lib"
    work_dir = Path(args.work_dir) if args.work_dir else out_root / "dllify-work"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== inventorying {len(libs)} LLVM libraries ===")
    inventory = build_inventory(nm, libs, compute_data_sizes=True)
    verify_hashes(inventory.functions)
    manifest = inventory.manifest()
    print(json.dumps(manifest, indent=2, sort_keys=True))
    if manifest["total_named_exports"] >= 65535:
        die(f"PE named export count would be {manifest['total_named_exports']}, exceeding the 65,535 limit")

    print("=== generating LLVM.dll resolver objects ===")
    resolver_c = work_dir / "llvm_resolver.c"
    resolver_obj = work_dir / "llvm_resolver.obj"
    symtab_obj = work_dir / "llvm_symtab.obj"
    llvm_def = work_dir / "LLVM.def"
    make_resolver_source(resolver_c)
    compile_resolver(cl, resolver_c, resolver_obj)
    make_symtab_obj(symtab_obj, sorted(inventory.functions))
    write_def(llvm_def, "LLVM", sorted(inventory.c_api), sorted(inventory.data))

    out_dll = bin_dir / "LLVM.dll"
    import_lib = lib_dir / "LLVM.lib"
    print("=== linking LLVM.dll ===")
    link_dll(
        linker,
        out_dll,
        import_lib,
        llvm_def,
        [resolver_obj, symtab_obj],
        libs,
        args.extra_link_lib or default_system_libs(),
        work_dir,
        whole_archive=True,
    )

    print("=== generating component stub libraries ===")
    stub_count = 0
    thunk_count = 0
    data_proxy_count = 0
    data_proxy_bytes = 0
    for lib in libs:
        funcs = owner_functions_for_lib(inventory, lib.name)
        owned_data = {
            sym: inventory.data_sizes.get(sym, 8)
            for sym in inventory.per_lib.get(lib.name, {})
            if inventory.owners.get(sym) == lib.name and sym in inventory.data
        }
        members: list[Path] = []
        if funcs:
            obj = work_dir / f"{lib.stem}.stubs.obj"
            helper_c = work_dir / f"{lib.stem}.resolve_helper.c"
            helper_obj = work_dir / f"{lib.stem}.resolve_helper.obj"
            safe_stem = ''.join(ch if ch.isalnum() else '_' for ch in lib.stem)
            helper_name = f"__llvm_dllify_resolve_{safe_stem}"
            make_stub_obj(obj, lib.stem, funcs, helper_name)
            make_resolve_helper_source(helper_c, helper_name)
            compile_c(cl, helper_c, helper_obj)
            members.extend([obj, helper_obj])
            stub_count += 1
            thunk_count += len(funcs)
        if owned_data:
            data_obj = work_dir / f"{lib.stem}.data.obj"
            helper_c = work_dir / f"{lib.stem}.data_init.c"
            helper_obj = work_dir / f"{lib.stem}.data_init.obj"
            table_name, count_name, anchor_name = make_data_proxy_obj(data_obj, lib.stem, owned_data)
            make_data_helper_source(helper_c, table_name, count_name, anchor_name)
            compile_c(cl, helper_c, helper_obj)
            members.extend([data_obj, helper_obj])
            data_proxy_count += len(owned_data)
            data_proxy_bytes += sum(max(1, size) for size in owned_data.values())
        if not members:
            empty_obj = work_dir / f"{lib.stem}.empty.obj"
            make_dummy_obj(empty_obj)
            members.append(empty_obj)
        # Existing LLVMCore/LLVMIRReader/etc. libraries remain the public link
        # surface; users do not need to link a combined LLVM.lib.  The thunk and
        # data helpers load LLVM.dll and resolve __llvm_resolve with Win32 APIs.
        make_lib(lib_tool, lib_dir / lib.name, members)

    forwarder = None
    if args.llvm_c_forwarder:
        exports = sorted(sym for sym in inventory.c_api if sym.startswith("LLVM"))
        fwd_def = work_dir / "LLVM-C.forwarder.def"
        fwd_dll = bin_dir / "LLVM-C.dll"
        fwd_lib = lib_dir / "LLVM-C.lib"
        write_forwarder_def(fwd_def, "LLVM-C", "LLVM", exports)
        # lld-link does not emit a DLL for a pure-forwarder .def unless at least
        # one object is present; link.exe handles pure .def forwarders only when
        # no object is present.  Select the compatible form for each linker.
        fwd_objects: list[Path] = []
        if "lld" in Path(linker).name.lower():
            fwd_dummy = work_dir / "forwarder_dummy.obj"
            make_dummy_obj(fwd_dummy)
            fwd_objects.append(fwd_dummy)
        link_dll(linker, fwd_dll, fwd_lib, fwd_def, fwd_objects, [], [], work_dir, no_entry=True)
        forwarder = str(fwd_dll)

    outputs = {
        "LLVM.dll": str(out_dll),
        "LLVM.lib": str(import_lib),
        "stub_library_dir": str(lib_dir),
        "work_dir": str(work_dir),
        "component_stub_libraries": str(len(libs)),
        "component_stub_objects": str(stub_count),
        "component_thunks": str(thunk_count),
        "data_proxy_symbols": str(data_proxy_count),
        "data_proxy_bytes": str(data_proxy_bytes),
    }
    if forwarder:
        outputs["LLVM-C.dll"] = forwarder
    write_manifest(work_dir / "manifest.json", inventory, libs, outputs)
    print("=== done ===")
    print(json.dumps(outputs, indent=2, sort_keys=True))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--libsfile", help="newline-separated list of LLVM component .lib files (for example build/libllvm-c.args)")
        sp.add_argument("--lib-dir", help="directory containing LLVM*.lib files; used if --libsfile is omitted")
        sp.add_argument("--nm", help="path to llvm-nm.exe")

    c = sub.add_parser("census", help="inventory symbols and verify hash collisions")
    add_common(c)
    c.set_defaults(func=command_census)

    b = sub.add_parser("build", help="build LLVM.dll and replacement stub libraries")
    add_common(b)
    b.add_argument("--output-dir", required=True, help="root output directory (bin/, lib/, dllify-work/ are created below it unless overridden)")
    b.add_argument("--bin-dir", help="directory for LLVM.dll")
    b.add_argument("--out-lib-dir", help="directory for LLVM.lib and component stub .lib files")
    b.add_argument("--work-dir", help="directory for generated sources/objects/manifest")
    b.add_argument("--cl", help="path to cl.exe")
    b.add_argument("--link", help="path to lld-link.exe or link.exe")
    b.add_argument("--lib-tool", help="path to lib.exe or llvm-lib.exe")
    b.add_argument("--extra-link-lib", action="append", help="additional linker input for LLVM.dll; if omitted, LLVM's standard Windows system libs are used")
    b.add_argument("--llvm-c-forwarder", action="store_true", help="also emit LLVM-C.dll as a forwarder to LLVM.dll")
    b.set_defaults(func=command_build)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except subprocess.CalledProcessError as exc:
        eprint(f"command failed with exit code {exc.returncode}")
        return exc.returncode or 1
    except RuntimeError as exc:
        eprint(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
