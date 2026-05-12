#!/usr/bin/env python3
"""Smoke-test tools/llvm_dllify.py with a tiny MSVC static library.

The test validates the two important transparent-link behaviours:
  * calls through an existing component .lib are resolved through LLVM.dll;
  * a non-dllimport data reference links and is initialized from the DLL copy.

Run from a Visual Studio developer environment:
    python tests/toy_dllify_smoke.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_tool(name: str, *, prefer_msvc: bool = False) -> str:
    candidates: list[str] = []
    found = shutil.which(name)
    if found:
        candidates.append(found)
    # Common fallback used by the repository's Windows build host.
    vs_root = Path(os.environ.get("VSINSTALLDIR", r"C:\Program Files\Microsoft Visual Studio\2022\Community"))
    tools = vs_root / "VC" / "Tools" / "MSVC"
    if tools.exists():
        for child in sorted(tools.iterdir(), reverse=True):
            p = child / "bin" / "HostX64" / "x64" / name
            if p.exists():
                candidates.append(str(p))
                break
    if prefer_msvc:
        candidates.sort(key=lambda p: 0 if "VC\\Tools\\MSVC" in p or "VC/Tools/MSVC" in p else 1)
    for p in candidates:
        if name.lower() == "link.exe" and "Git\\usr\\bin" in p:
            continue
        return p
    raise SystemExit(f"could not find {name}; run from a VS developer environment")


def run(args: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args))
    subprocess.run(args, check=True, env=env)


def main() -> int:
    cl = find_tool("cl.exe")
    lib = find_tool("lib.exe")
    link = find_tool("link.exe", prefer_msvc=True)
    env = os.environ.copy()
    env.setdefault("MSYS2_ARG_CONV_EXCL", "*")

    with tempfile.TemporaryDirectory(prefix="dllify-toy-") as td:
        t = Path(td)
        (t / "toy.cpp").write_text(
            "int global_value = 41;\n"
            "int add_one(int x) { return x + 1; }\n"
            "int read_global() { return global_value; }\n",
            encoding="utf-8",
        )
        (t / "use.cpp").write_text(
            "#include <stdio.h>\n"
            "extern int global_value;\n"
            "int add_one(int);\n"
            "int read_global();\n"
            "int main() {\n"
            "  int a = add_one(41);\n"
            "  int b = read_global();\n"
            "  printf(\"a=%d b=%d global=%d\\n\", a, b, global_value);\n"
            "  return (a == 42 && b == 41 && global_value == 41) ? 0 : 2;\n"
            "}\n",
            encoding="utf-8",
        )

        run([cl, "/nologo", "/c", "/EHsc", "/MD", f"/Fo{t / 'toy.obj'}", str(t / "toy.cpp")], env=env)
        run([lib, "/nologo", f"/OUT:{t / 'toy.lib'}", str(t / "toy.obj")], env=env)
        (t / "libs.txt").write_text(str(t / "toy.lib") + "\n", encoding="utf-8")

        run([
            sys.executable,
            str(ROOT / "tools" / "llvm_dllify.py"),
            "build",
            "--libsfile", str(t / "libs.txt"),
            "--output-dir", str(t / "out"),
            "--link", link,
            "--extra-link-lib", "msvcrt.lib",
        ], env=env)

        run([cl, "/nologo", "/c", "/EHsc", "/MD", f"/Fo{t / 'use.obj'}", str(t / "use.cpp")], env=env)
        run([cl, "/nologo", f"/Fe{t / 'use.exe'}", str(t / "use.obj"), str(t / "out" / "lib" / "toy.lib"), "/link"], env=env)
        run_env = env.copy()
        run_env["PATH"] = str(t / "out" / "bin") + os.pathsep + run_env.get("PATH", "")
        run([str(t / "use.exe")], env=run_env)

    print("toy dllify smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
