#!/usr/bin/env python3
"""Relink CMake/Ninja MSVC binary targets without rebuilding dependencies.

LLVM.dll mode replaces static component libraries with DLL stub libraries after
CMake has already generated the Ninja graph.  Running plain ``ninja`` after that
also reruns generated-header/tablegen edges because the host tools were touched.
This helper materializes Ninja's linker response file for selected executable
outputs and invokes only the final CMake ``vs_link_exe`` command for each one.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class Rule:
    name: str
    vars: dict[str, str] = field(default_factory=dict)


@dataclass
class BuildEdge:
    outputs: list[str]
    rule: str
    explicit_inputs: list[str]
    vars: dict[str, str] = field(default_factory=dict)


def die(msg: str) -> None:
    raise SystemExit(f"error: {msg}")


def logical_lines(path: Path) -> Iterable[str]:
    pending = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip("\n")
        # Ninja continuations use a trailing unescaped '$'.  CMake's generated
        # build.ninja rarely needs them for these link edges, but handling them
        # makes the parser safer.
        if line.endswith("$") and not line.endswith("$$"):
            pending += line[:-1]
            continue
        if pending:
            line = pending + line
            pending = ""
        yield line
    if pending:
        yield pending


def parse_rules(path: Path) -> dict[str, Rule]:
    rules: dict[str, Rule] = {}
    current: Rule | None = None
    for line in logical_lines(path):
        if line.startswith("rule "):
            name = line.split(None, 1)[1]
            current = Rule(name)
            rules[name] = current
            continue
        if current and line.startswith("  ") and "=" in line:
            key, value = line.strip().split("=", 1)
            current.vars[key.strip()] = value.strip()
            continue
        if line and not line.startswith("  "):
            current = None
    return rules


def split_ninja_words(s: str) -> list[str]:
    # Good enough for CMake-generated build edges: paths with spaces are escaped
    # as '$ ', literal '$' as '$$', and ':' as '$:'.
    words: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "$" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in " $:":
                cur.append(nxt)
                i += 2
                continue
        if ch.isspace():
            if cur:
                words.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        words.append("".join(cur))
    return words


def parse_build_edges(path: Path) -> dict[str, BuildEdge]:
    edges: dict[str, BuildEdge] = {}
    current: BuildEdge | None = None
    for line in logical_lines(path):
        if line.startswith("build "):
            rest = line[len("build "):]
            if ":" not in rest:
                current = None
                continue
            outs_part, rhs = rest.split(":", 1)
            rhs_words = split_ninja_words(rhs.strip())
            if not rhs_words:
                current = None
                continue
            rule = rhs_words[0]
            input_words = rhs_words[1:]
            explicit: list[str] = []
            for word in input_words:
                if word in {"|", "||"}:
                    break
                explicit.append(word)
            current = BuildEdge(split_ninja_words(outs_part), rule, explicit)
            for out in current.outputs:
                edges[normalize_target(out)] = current
            continue
        if current and line.startswith("  ") and "=" in line:
            key, value = line.strip().split("=", 1)
            current.vars[key.strip()] = value.strip()
            continue
        if line and not line.startswith("  "):
            current = None
    return edges


def normalize_target(s: str | Path) -> str:
    return str(s).replace("\\", "/").lower()


_var_re = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z0-9_.-]+)")


def expand(value: str, variables: dict[str, str]) -> str:
    sentinel = "\u0000DOLLAR\u0000"
    value = value.replace("$$", sentinel)

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or ""
        return variables.get(name, "")

    prev = None
    cur = value
    # A couple of passes handles variables that contain other variables without
    # trying to implement all of Ninja's expansion semantics.
    for _ in range(4):
        if cur == prev:
            break
        prev = cur
        cur = _var_re.sub(repl, cur)
    return cur.replace(sentinel, "$")


def relink_edge(
    build_dir: Path,
    rules: dict[str, Rule],
    edge: BuildEdge,
    *,
    extra_objects: list[Path] | None = None,
    extra_link_flags: list[str] | None = None,
    dry_run: bool = False,
) -> int:
    rule = rules.get(edge.rule)
    if not rule:
        print(f"skip {edge.outputs[0]}: rule {edge.rule!r} not found", file=sys.stderr)
        return 0
    if not edge.rule.startswith(("CXX_EXECUTABLE_LINKER", "CXX_SHARED_LIBRARY_LINKER")):
        print(f"skip {edge.outputs[0]}: not a CXX executable/shared-library link rule ({edge.rule})")
        return 0

    variables: dict[str, str] = {}
    variables.update(edge.vars)
    variables.setdefault("in", " ".join(edge.explicit_inputs))
    variables.setdefault("out", " ".join(edge.outputs))
    variables.setdefault("LINK_PATH", "")
    variables.setdefault("LINK_LIBRARIES", "")
    variables.setdefault("MANIFESTS", "")
    variables.setdefault("PRE_LINK", "cd .")
    variables.setdefault("POST_BUILD", "cd .")
    if extra_objects:
        variables["in"] = " ".join(str(p) for p in extra_objects) + " " + variables["in"]
    if extra_link_flags:
        variables["LINK_FLAGS"] = (variables.get("LINK_FLAGS", "") + " " + " ".join(extra_link_flags)).strip()

    rsp_name = expand(rule.vars.get("rspfile", ""), variables)
    rsp_content = expand(rule.vars.get("rspfile_content", ""), variables)
    if rsp_name:
        rsp_path = build_dir / rsp_name.replace("\\", os.sep)
        if dry_run:
            print(f"write {rsp_path}: {len(rsp_content)} bytes")
        else:
            rsp_path.parent.mkdir(parents=True, exist_ok=True)
            rsp_path.write_text(rsp_content + "\n", encoding="utf-8")

    command = expand(rule.vars.get("command", ""), variables)
    if not command:
        print(f"skip {edge.outputs[0]}: empty link command", file=sys.stderr)
        return 0
    print(f"=== relinking {edge.outputs[0]} ===")
    print(command)
    if dry_run:
        return 0
    env = os.environ.copy()
    env.setdefault("MSYS2_ARG_CONV_EXCL", "*")
    return subprocess.run(command, cwd=str(build_dir), shell=True, env=env).returncode


def make_noop_atexit_obj(build_dir: Path) -> Path:
    """Build a small MSVC object that disables atexit registration.

    llvm-profgen is the only observed tool needing this: when linked against the
    DLL stubs, MSVC's CRT atexit registration crashes before main().  LLVM tools
    do not require process-exit destructors for correctness, so using this object
    for llvm-profgen keeps the executable dynamically linked and usable.
    """
    work_dir = build_dir / "dllify-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    src = work_dir / "noop_atexit.c"
    obj = work_dir / "noop_atexit.obj"
    src.write_text(
        "typedef void (__cdecl *atexit_func_t)(void);\n"
        "int __cdecl atexit(atexit_func_t f) { (void)f; return 0; }\n"
        "int __cdecl _crt_atexit(atexit_func_t f) { (void)f; return 0; }\n",
        encoding="utf-8",
    )
    cl = shutil.which("cl.exe") or shutil.which("cl")
    if not cl:
        die("cl.exe not found; run from a Visual Studio developer environment")
    run = subprocess.run([cl, "/nologo", "/MD", "/c", f"/Fo{obj}", str(src)])
    if run.returncode != 0:
        die("failed to compile noop_atexit.obj")
    return obj


def selected_targets(args: argparse.Namespace, edges: dict[str, BuildEdge]) -> list[str]:
    targets: list[str] = []
    if args.all_bin_exes:
        bin_dir = Path(args.bin_dir) if args.bin_dir else Path(args.build_dir) / "bin"
        for exe in sorted(bin_dir.glob("*.exe")):
            rel = Path("bin") / exe.name
            targets.append(str(rel))
    targets.extend(args.target or [])

    excludes = args.exclude or []
    out: list[str] = []
    seen: set[str] = set()
    for target in targets:
        name = Path(str(target).replace("\\", "/")).name
        if any(fnmatch.fnmatchcase(name.lower(), pat.lower()) for pat in excludes):
            continue
        key = normalize_target(target)
        if key not in edges:
            # Build/bin contains symlink placeholder files such as clang++.exe;
            # they are copied/created by install rules, not linked directly.
            print(f"skip {target}: no Ninja build edge")
            continue
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, help="CMake/Ninja build directory")
    parser.add_argument("--bin-dir", help="bin directory to scan with --all-bin-exes (default: BUILD_DIR/bin)")
    parser.add_argument("--all-bin-exes", action="store_true", help="relink every linked .exe in BUILD_DIR/bin")
    parser.add_argument("--target", action="append", help="specific Ninja output to relink, e.g. bin/clang.exe or bin/libclang.dll")
    parser.add_argument("--exclude", action="append", help="basename glob to skip, e.g. *-tblgen.exe")
    parser.add_argument(
        "--msvc-noop-atexit",
        action="append",
        default=[],
        metavar="EXE",
        help="for this executable basename, prepend a no-op atexit object and link with /force:multiple",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    build_dir = Path(args.build_dir).resolve()
    rules_path = build_dir / "CMakeFiles" / "rules.ninja"
    build_path = build_dir / "build.ninja"
    if not rules_path.exists() or not build_path.exists():
        die(f"{build_dir} does not look like a CMake Ninja build directory")

    rules = parse_rules(rules_path)
    edges = parse_build_edges(build_path)
    targets = selected_targets(args, edges)
    if not targets:
        die("no targets selected")

    noop_atexit_obj: Path | None = None
    noop_atexit_targets = {name.lower() for name in args.msvc_noop_atexit}
    for key in targets:
        name = Path(key).name.lower()
        extra_objects: list[Path] = []
        extra_link_flags: list[str] = []
        if name in noop_atexit_targets:
            if not args.dry_run:
                noop_atexit_obj = noop_atexit_obj or make_noop_atexit_obj(build_dir)
                extra_objects.append(noop_atexit_obj)
            extra_link_flags.append("/force:multiple")
        rc = relink_edge(
            build_dir,
            rules,
            edges[key],
            extra_objects=extra_objects,
            extra_link_flags=extra_link_flags,
            dry_run=args.dry_run,
        )
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
