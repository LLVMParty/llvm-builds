# Windows LLVM.dll / clang-cpp.dll / lld.dll implementation

This repository implements an **external, post-build approximation of
`LLVM_BUILD_LLVM_DYLIB=ON` + `LLVM_LINK_LLVM_DYLIB=ON` for MSVC Windows**.
LLVM itself disables the normal dylib flow on Windows, so the implementation
keeps the downloaded LLVM source tree untouched and rewrites the built artifacts
from the outside.

The entry point is:

```bat
build-windows.bat --llvm-dll
```

or equivalently:

```bat
set LLVM_BUILD_LLVM_DYLIB=ON
build-windows.bat
```

## Current output layout

The dynamic package contains:

```text
bin/LLVM.dll          monolithic LLVM code DLL
bin/LLVM-C.dll        C API forwarder DLL to LLVM.dll
bin/clang-cpp.dll     monolithic clang C++ DLL, imports LLVM.dll
bin/lld.dll           monolithic lld DLL, imports LLVM.dll
bin/libclang.dll      clang C API DLL, relinked to clang-cpp.dll + LLVM.dll
bin/LTO.dll           LTO C API DLL, relinked to LLVM.dll

lib/LLVM.lib          import lib for LLVM.dll named exports/resolver
lib/LLVM-C.lib        import lib for LLVM-C.dll forwarder
lib/clang-cpp.lib     import lib for clang-cpp.dll named exports/resolver
lib/lld.lib           import lib for lld.dll named exports/resolver
lib/LLVM*.lib         replacement LLVM component stub libraries
lib/clang*.lib        replacement clang component stub libraries
lib/lld*.lib          replacement lld component stub libraries

share/llvm-dllify-manifest.json
share/clang-cpp-dllify-manifest.json
share/lld-dllify-manifest.json
```

The package is named with a `-dll` suffix, for example:

```text
dist/llvm-21.1.6-windows-x86_64-dll.zip
```

Observed in the current 21.1.6 x64 release build:

- `LLVM.dll` is about 136 MB.
- `clang-cpp.dll` is about 58 MB and imports `LLVM.dll`.
- `lld.dll` is about 6 MB and imports `LLVM.dll`.
- `libclang.dll` is about 1.3 MB and imports `clang-cpp.dll` and `LLVM.dll`.
- `LTO.dll` is about 0.3 MB and imports `LLVM.dll`.
- Most installed executables are small and import one or more of
  `LLVM.dll`, `clang-cpp.dll`, or `lld.dll`. Tiny utilities with no LLVM
  dependency, such as `count.exe` and `llvm-PerfectShuffle.exe`, remain static.

## Why this is needed

On ELF/Mach-O, LLVM can build a large shared library and link tools against it.
On MSVC Windows that does not work out of the box because:

1. MSVC does not auto-export C++ symbols from a DLL.
2. MSVC does not auto-import non-`dllimport` function/data references the way
   MinGW does.
3. PE/COFF named exports are practically capped at 65,535 names, while modern
   LLVM has far more public C++ symbols than that.

The solution here avoids named PE exports for most C++ functions. The DLL owns
all real code, and component libraries are replaced with small stub archives that
resolve function addresses through a hash resolver at runtime.

## Build flow

`build-windows.bat --llvm-dll` performs these steps:

1. Configure and build LLVM normally as a static MSVC tree.
   - The downloaded LLVM source tree is not patched.
   - `LLVM_BUILD_LLVM_C_DYLIB=ON` is still enabled so LLVM's normal C API build
     metadata is available.
2. Run `tools/llvm_dllify.py` for LLVM.
   - Input libraries come from `build/libllvm-c.args`.
   - `LLVMTableGen.lib` is excluded from `LLVM.dll`.
   - Outputs: `LLVM.dll`, `LLVM.lib`, replacement `LLVM*.lib` component stubs,
     and `LLVM-C.dll`/`LLVM-C.lib` forwarders.
3. Run `tools/llvm_dllify.py` for clang.
   - Input libraries are `clang*.lib` from the build lib directory.
   - `clang-repl.lib` is excluded.
   - Outputs: `clang-cpp.dll`, `clang-cpp.lib`, replacement `clang*.lib`
     component stubs.
   - The DLL links against the already-generated LLVM stubs/import libraries and
     therefore imports `LLVM.dll`.
4. Run `tools/llvm_dllify.py` for lld.
   - Input libraries are `lld*.lib`.
   - Outputs: `lld.dll`, `lld.lib`, replacement `lld*.lib` component stubs.
   - The DLL imports `LLVM.dll`.
5. Run `tools/relink_ninja_exes.py`.
   - It parses CMake/Ninja link rules and re-invokes only final link commands,
     without rebuilding generated headers or re-running tablegen edges.
   - It relinks all linked executables under `build/bin` against the replacement
     component stubs.
   - It also relinks `bin/libclang.dll` and `bin/LTO.dll`, so those DLLs become
     thin importers instead of large static DLLs.
   - `llvm-profgen.exe` is relinked with a small no-op `atexit` shim because the
     normal MSVC CRT `atexit` registration path crashed before `main()` after
     dynamic relinking. LLVM command-line tools do not rely on process-exit
     destructors for correctness.
6. Install with `cmake --install`, then copy the generated DLL/import-lib and
   manifest artifacts over the installed tree.
7. Package the install tree as a zip.

## Symbol model

Each generated DLL has three categories of public symbols.

### 1. Resolver export

Each DLL exports one resolver function:

```text
LLVM.dll       __llvm_resolve(uint64_t hash)
clang-cpp.dll  __clang_resolve(uint64_t hash)
lld.dll        __lld_resolve(uint64_t hash)
```

The resolver searches a sorted table of FNV-1a 64-bit hashes and returns the
real function address from the DLL.

64-bit hashes are used because the real clang/LLVM symbol set produced 32-bit
hash collisions.

### 2. Named C API exports

C API symbols are normal named PE exports:

- LLVM uses the `LLVM*` prefix.
- clang support is configurable with `--c-api-prefix clang_`; the current
  `clang-cpp.dll` build does not depend on this for `libclang.dll` because
  `libclang.dll` remains the public C API DLL.
- lld has no C API prefix in this build.

`LLVM-C.dll` is a forwarder DLL whose exports forward to `LLVM.dll`, for
example:

```text
LLVMContextCreate = LLVM.LLVMContextCreate
```

### 3. Named data exports

Selected data symbols are real PE `DATA` exports. This includes RTTI, vtables,
exception metadata, static data members, and other link-visible data needed for
MSVC C++ consumers.

Compiler-generated string literal COMDATs and import-table internals are filtered
out to stay below the PE named export limit.

## Component stub libraries

The replacement component libraries preserve LLVM's usual component link
surface:

```text
LLVMCore.lib
LLVMIRReader.lib
LLVMObject.lib
clangAST.lib
lldCOFF.lib
...
```

They are no longer static code archives. They are generated archives containing
just enough COFF to satisfy normal MSVC links and redirect to the DLL.

For each component library, `llvm_dllify.py` emits some combination of:

- a resolver helper object that imports the DLL resolver directly;
- a small resolver import library, such as `LLVMResolve.lib`;
- function thunk objects;
- separate C API thunk objects;
- data proxy objects;
- data-copy initializer objects;
- an empty dummy object if the component has no exported symbols.

### Function thunks

For each owned function symbol, the component stub defines the original MSVC
mangled symbol name. The thunk is lazy:

1. Load the cached function pointer slot.
2. If non-null, jump directly to it.
3. Otherwise preserve Windows x64 argument registers (`rcx`, `rdx`, `r8`, `r9`,
   and `xmm0`-`xmm3`).
4. Call the resolver helper with the symbol's FNV-1a 64-bit hash.
5. Cache the returned function pointer.
6. Restore registers and tail-jump to the resolved function.

Every thunk is placed in a pick-any COMDAT `.text$mn` section. This lets the MSVC
linker discard duplicate inline/template definitions instead of failing when a
consumer object and a stub library both provide the same COMDAT symbol.

The resolver helper imports the resolver symbol directly, rather than using
`LoadLibrary`/`GetProcAddress`. This makes tools show a real `LLVM.dll`,
`clang-cpp.dll`, or `lld.dll` dependency in `dumpbin /DEPENDENTS`.

### C API thunks

C API thunks are emitted in separate `*.c_api_stubs.obj` archive members. This is
important because consumers can link both `LLVM-C.lib` and component libraries;
keeping C API and C++ thunks separate avoids duplicate-definition/replacement
problems when the linker extracts archive members.

### Data proxies

MSVC objects compiled without `dllimport` cannot transparently reference DLL
data. The generated component stubs therefore define local data proxy symbols.

Each data proxy is emitted in its own pick-any COMDAT `.data$D` section. A
companion CRT initializer runs from `.CRT$XCT` and copies initial bytes from the
DLL for data that is safe to copy.

Only safe immutable ABI data is copied. Mutable LLVM globals such as command-line
options, registries, `ManagedStatic` state, and pass IDs are intentionally not
byte-copied because their internal pointers refer to the DLL's own object and can
crash or hang consumers. Constant pointer data (`@@3PEB` / `@@3QEB` MSVC-mangled
forms) is copied because it is needed by option constructors and is safe.

## `LLVM.lib` versus component stubs

Important current limitation:

`lib/LLVM.lib` is the normal import library produced for `LLVM.dll`'s named PE
exports plus the resolver. It is **not** currently a fat all-C++-symbols stub
library.

Therefore, for C++ consumers, this is not enough today:

```cmake
target_link_libraries(mytool PRIVATE LLVM)
```

unless the `LLVM` CMake target also carries the component stub libraries in its
`INTERFACE_LINK_LIBRARIES`.

What works today is either:

```cmake
target_link_libraries(mytool PRIVATE LLVMCore LLVMObject LLVMSupport ...)
```

using the installed component stub libraries, or a wrapper target that fakes
`LLVM_LINK_LLVM_DYLIB=ON` by mapping `LLVM` to both `LLVM.lib` and the component
stubs.

A future upstream-quality implementation should make the public `LLVM.lib` (or
the exported `LLVM` target) provide all C++ thunk definitions so consumers can
link only `LLVM` just like they do with `libLLVM.so` on Unix.

## CMake export behavior in the current package

The current external implementation intentionally does not patch LLVM's source
CMake files or installed `LLVMConfig.cmake` logic. Installed LLVM component
CMake targets still exist and point at the replacement stub libraries because the
files are overwritten after the static build.

Consequences:

- `llvm_map_components_to_libnames(...)` continues to work; it returns component
  library names, but those libraries are DLL stubs.
- `LLVM_LINK_LLVM_DYLIB=ON` is not truly implemented in installed CMake metadata
  yet.
- Consumers that create a fake imported `LLVM` target on Windows should include
  component stubs transitively if they need C++ API symbols.
- `LLVM-C` can be modeled as either the `LLVM-C.dll` forwarder target or as an
  interface to `LLVM`, since all LLVM C API functions are exported from
  `LLVM.dll`.

## Relinked binaries and DLLs

After dllification, the build relinks:

- all linked `.exe` files under `build/bin`;
- `bin/libclang.dll`;
- `bin/LTO.dll`.

Expected dependencies include:

```text
clang.exe        -> LLVM.dll, clang-cpp.dll
clang-cl.exe     -> LLVM.dll, clang-cpp.dll
clang-format.exe -> LLVM.dll, clang-cpp.dll
opt.exe          -> LLVM.dll
llc.exe          -> LLVM.dll
llvm-as.exe      -> LLVM.dll
llvm-profgen.exe -> LLVM.dll
lld.exe          -> LLVM.dll, lld.dll
lld-link.exe     -> LLVM.dll, lld.dll
ld.lld.exe       -> LLVM.dll, lld.dll
wasm-ld.exe      -> LLVM.dll, lld.dll
FileCheck.exe    -> LLVM.dll
llvm-tblgen.exe  -> LLVM.dll
clang-tblgen.exe -> LLVM.dll
libclang.dll     -> LLVM.dll, clang-cpp.dll
LTO.dll          -> LLVM.dll
```

Small tools with no LLVM dependency may remain non-dynamic.

## Manifests

Each dllify run writes a manifest:

```text
share/llvm-dllify-manifest.json
share/clang-cpp-dllify-manifest.json
share/lld-dllify-manifest.json
```

The manifests record:

- input libraries;
- output DLL/import/stub paths;
- number of component stub libraries;
- number of generated function thunks;
- number and total size of data proxies;
- number of data symbols copied at startup;
- hash algorithm (`fnv1a64`);
- architecture (`x86_64-pc-windows-msvc`).

These manifests are useful for auditing, but they are not a substitute for
checking actual binaries with `dumpbin /DEPENDENTS` and running key tools.

## Validation commands

Useful quick checks:

```bat
build-windows.bat --llvm-dll --jobs 8

python -m py_compile tools\llvm_dllify.py tools\relink_ninja_exes.py
python tests\toy_dllify_smoke.py

dumpbin /DEPENDENTS .llvm-install\bin\clang.exe
dumpbin /DEPENDENTS .llvm-install\bin\lld-link.exe
dumpbin /DEPENDENTS .llvm-install\bin\libclang.dll
dumpbin /DEPENDENTS .llvm-install\bin\LTO.dll

.llvm-install\bin\clang.exe --version
.llvm-install\bin\opt.exe --version
.llvm-install\bin\lld-link.exe --version
.llvm-install\bin\llvm-profgen.exe --version
```

For consumer-side testing, `llvm-nanobind` is useful because it exercises both C
API and C++ API usage. With the current package, a C++ consumer must link the
component stubs directly or through a wrapper target that adds them transitively.

## Current limitations and upstream path

This implementation is intentionally external and Windows-x64-specific.

Known limitations:

- Only AMD64 COFF is implemented.
- The LLVM source tree and upstream CMake dylib logic are not patched.
- Installed CMake metadata does not yet provide a true Unix-like
  `LLVM_LINK_LLVM_DYLIB=ON` experience.
- `LLVM.lib` is not yet a single fat C++ stub archive.
- Data proxies are a pragmatic compatibility layer for non-`dllimport` MSVC
  objects, not a native PE/COFF feature.

A proper upstream implementation would integrate these ideas into LLVM's build
system:

1. Build static component libraries.
2. Produce `LLVM.dll`, `clang-cpp.dll`, and `lld.dll` from component archives.
3. Generate public import/stub libraries.
4. Teach `llvm_config()` / `llvm_map_components_to_libnames()` / exported CMake
   targets to map component usage to the dylib when `LLVM_LINK_LLVM_DYLIB=ON`.
5. Make `$<TARGET_RUNTIME_DLLS>` work for `LLVM.dll`, `clang-cpp.dll`, `lld.dll`,
   `LLVM-C.dll`, `libclang.dll`, and `LTO.dll`.
6. Keep component stubs available for compatibility with consumers that still
   link component names explicitly.

The long-term cleanest consumer experience is that `target_link_libraries(foo
PRIVATE LLVM)` is sufficient for C++ API users on Windows, matching `libLLVM.so`
usage on Unix.
