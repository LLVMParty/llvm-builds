# Plan: LLVM Shared Library Builds on MSVC Windows

## Implementation status in this repo

- `tools/llvm_dllify.py` implements the external dllify flow without patching the downloaded LLVM source tree.
- `build-windows.bat --llvm-dll` (or `LLVM_BUILD_LLVM_DYLIB=ON` in the environment) enables the MSVC `LLVM_BUILD_LLVM_DYLIB` equivalent: it builds LLVM static libraries, emits `LLVM.dll`, replaces LLVM component libraries with transparent stubs, then builds the remaining tools against those stubs.
- Component libraries such as `LLVMCore.lib`, `LLVMIRReader.lib`, and `LLVMCGData.lib` remain the consumer-facing link inputs; consumers do not need to link a combined `LLVM.lib` for C++ component use.
- `LLVM.lib` is still emitted as the normal DLL import library for direct/dllimport use, and `LLVM-C.dll`/`LLVM-C.lib` can be emitted as C API forwarders.

## Goal

Achieve feature parity with `LLVM_BUILD_LLVM_DYLIB=ON` / `LLVM_LINK_LLVM_DYLIB=ON` on Windows using the MSVC toolchain, without modifying LLVM source code or requiring `__declspec(dllexport/dllimport)` annotations.

## Background: Why This Doesn't Work Today

LLVM's shared library build (`LLVM_BUILD_LLVM_DYLIB`) is explicitly disabled on Windows. The MSYS2/MinGW community has it working, but only because:

1. The GNU linker auto-exports all global symbols without `__declspec(dllexport)`.
2. The GNU linker auto-imports symbols without `__declspec(dllimport)` by creating thunks (for functions) and fake IAT entries + runtime pseudo-relocations (for data).
3. MinGW's CRT has a startup fixup routine that processes pseudo-relocations before `main()`.

MSVC's `link.exe` provides none of this. Additionally, the PE/COFF format limits named exports to 65,535 per DLL (the `AddressOfNameOrdinals` table uses 16-bit indices). Modern LLVM has well over 65K symbols.

## Architecture Overview

The solution uses a post-build tool (`llvm_dllify`) that takes LLVM's normal static `.lib` build output and produces:

1. Shared DLLs containing all the code
2. Drop-in replacement stub `.lib` files that transparently redirect to the DLLs
3. Forwarder DLLs for backwards compatibility

The key insight: the 65K PE export limit only matters for symbols in the PE export table. We bypass it by exporting a single resolver function that performs hash-based lookup at init time. Only data symbols and C API functions use real PE exports.

### Target Layout (matching Linux parity)

```
Linux                              Windows (our design)
─────                              ──────────────────────
libLLVM-22.so                  →   LLVM.dll
libclang-cpp.so                →   clang-cpp.dll (imports from LLVM.dll)
libclang.so                    →   libclang.dll (imports clang-cpp.dll + LLVM.dll)
libLTO.so                      →   LTO.dll (imports LLVM.dll)
LLVM-C.dll                     →   (forwarder to LLVM.dll)
```

### How Function Resolution Works

Each DLL exports a resolver function and an internal symbol hash table:

```c
// Inside LLVM.dll
__declspec(dllexport) void* __llvm_resolve(uint32_t hash) {
    // binary search over sorted {hash, func_ptr} array
}
```

Each stub `.lib` contains, per function symbol:
- A thunk: `LLVMSomeFunction: jmp [__fp_LLVMSomeFunction]`
- A function pointer slot: `__fp_LLVMSomeFunction dq 0`

Plus a `.CRT$XCU` initializer that calls the resolver to fill in all function pointers at startup, before `main()`.

Data symbols (vtables, RTTI, static members) are exported as real PE exports with the `DATA` keyword in the `.def` file, since they cannot be stubbed through code.

C API functions (`LLVM*` from llvm-c, `clang_*` from clang-c) are also real PE exports, enabling `GetProcAddress` and standard dynamic loading.

---

## Phase 1: Codebase Exploration

### 1.1 Understand LLVM's Existing Dylib Infrastructure

Examine these files to understand how the Linux/macOS dylib build works:

```
llvm/cmake/modules/AddLLVM.cmake
  - Find: add_llvm_library, llvm_config, LLVM_LINK_LLVM_DYLIB
  - Understand how tool targets switch between static libs and the dylib
  - Find the LLVM_DYLIB_COMPONENTS variable and how components are collected

llvm/tools/llvm-shlib/CMakeLists.txt
  - This is where libLLVM.so is actually created
  - Study how it collects all component libs and links them
  - Note the version script / exported symbols handling
  - Note the `if(NOT WIN32)` guards

clang/tools/clang-shlib/CMakeLists.txt
  - This creates libclang-cpp.so
  - Note: `if(UNIX OR (MINGW AND LLVM_LINK_LLVM_DYLIB))`
  - Study how it collects clang component libs
  - Study how it links against libLLVM

clang/tools/libclang/CMakeLists.txt
  - This creates libclang.so / libclang.dll
  - This ALREADY works on MSVC Windows
  - Study CINDEX_LINKAGE macro in clang/include/clang-c/Platform.h
  - Understand how it exports symbols
```

### 1.2 Understand the Build Dependency Graph

```
llvm/CMakeLists.txt
  - Find where LLVM_BUILD_LLVM_DYLIB is defined and guarded
  - Find where LLVM_LINK_LLVM_DYLIB is defined and guarded
  - Search for `if(NOT WIN32)` and `if(NOT MSVC)` around dylib logic

llvm/cmake/modules/LLVMConfig.cmake.in
  - This is what consumers see after `find_package(LLVM)`
  - Understand LLVM_AVAILABLE_LIBS, LLVM_DYLIB_COMPONENTS
  
llvm/cmake/modules/LLVM-Config.cmake
  - Find llvm_config() function
  - This is where USE_SHARED logic redirects from component libs to LLVM
```

### 1.3 Catalog the Static Libraries

Run a test build and inventory the output:

```bash
cmake -S llvm -B build -G Ninja ^
    -DLLVM_ENABLE_PROJECTS="clang;lld" ^
    -DLLVM_BUILD_TOOLS=OFF ^
    -DLLVM_INCLUDE_TESTS=OFF ^
    -DLLVM_INCLUDE_BENCHMARKS=OFF ^
    -DLLVM_TARGETS_TO_BUILD=X86 ^
    -DCMAKE_BUILD_TYPE=Release
cmake --build build

# Then inventory:
dir /b build\lib\LLVM*.lib > llvm_libs.txt
dir /b build\lib\clang*.lib > clang_libs.txt
dir /b build\lib\lld*.lib > lld_libs.txt
```

### 1.4 Symbol Census

This is critical data gathering. For each `.lib`:

```bash
# Count total symbols
dumpbin /SYMBOLS LLVMCore.lib | findstr "External" | measure

# Separate functions from data  
# Functions: SECT + notype() or notype
# Data: SECT + notype (no parens)
# Look at the symbol type field

# Also try:
dumpbin /LINKERMEMBER build\lib\LLVMCore.lib
# This lists all public symbols - easier to parse
```

Questions to answer:
- Total number of function symbols across all LLVM libs?
- Total number of data symbols across all LLVM libs?
- Same for clang libs?
- How many C API symbols (unmangled, `LLVM*` prefix)?
- How many C API symbols (`clang_*` prefix)?
- Do any individual data symbol counts approach 65K?

---

## Phase 2: Prototype the Core Tool

### 2.1 Symbol Extraction and Classification

Write `llvm_dllify.py` starting with symbol extraction:

```python
# Input: list of .lib files
# Output: classified symbol list

import subprocess, re

def extract_symbols(lib_path):
    """Run dumpbin /LINKERMEMBER on a .lib, return list of symbols."""
    result = subprocess.run(
        ['dumpbin', '/LINKERMEMBER:1', lib_path],
        capture_output=True, text=True
    )
    # Parse the output - format is:
    #   offset  symbol_name
    # After "public symbols" header
    symbols = []
    in_symbols = False
    for line in result.stdout.splitlines():
        line = line.strip()
        if 'public symbols' in line.lower():
            in_symbols = True
            continue
        if in_symbols and line:
            parts = line.split(None, 1)
            if len(parts) == 2:
                symbols.append(parts[1])
    return symbols

def classify_symbol(name):
    """Classify a symbol as function, data, or c_api."""
    # C API: unmangled names starting with LLVM or clang_
    if not name.startswith('?') and not name.startswith('_Z'):
        if name.startswith('LLVM') or name.startswith('clang_'):
            return 'c_api'
    
    # MSVC mangled data patterns:
    # ?name@namespace@@3<type> - static data member
    # ??_7ClassName@@6B@ - vtable (vftable)
    # ??_R0 - RTTI type descriptor
    # ??_R1, ??_R2, ??_R3, ??_R4 - RTTI related
    
    # This needs refinement based on actual symbol analysis.
    # Key approach: use dumpbin /SYMBOLS to get the full type info
    # or use undname to demangle and check for () indicating function
    
    return 'function'  # default assumption

def is_data_symbol(name):
    """
    More precise: run undname.exe on the symbol.
    If the demangled form contains '(' it's a function.
    If it says "class", "struct" etc. without "()" it's data.
    Vtable symbols (??_7) are always data.
    RTTI symbols (??_R*) are always data.
    """
    if name.startswith('??_7') or name.startswith('??_R'):
        return True
    # Run undname for complex cases
    result = subprocess.run(['undname', name], capture_output=True, text=True)
    demangled = result.stdout.strip()
    # Functions have parameter lists
    if '(' in demangled and 'operator()' not in demangled:
        return False
    return True
```

**Experiment**: Run this on the actual build output. Answer:
- How many total symbols per partition (LLVM vs Clang)?
- How many data symbols? (Expected: <5K)
- How many C API symbols? (Expected: 2-3K)
- Are there any symbol classification edge cases?

### 2.2 Hash Function Selection

Requirements:
- Must be collision-free over the actual symbol set
- 32-bit output preferred (compact symtab, fast lookup)
- Deterministic (same hash at build time and in stub libs)

```python
import hashlib

def symbol_hash(name: str) -> int:
    """FNV-1a 32-bit hash. Fast, good distribution."""
    h = 0x811c9dc5
    for byte in name.encode('utf-8'):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h

def verify_no_collisions(symbols):
    """Must pass for the chosen hash function."""
    seen = {}
    for sym in symbols:
        h = symbol_hash(sym)
        if h in seen:
            print(f"COLLISION: {sym} and {seen[h]} -> {h:#x}")
            return False
        seen[h] = sym
    return True
```

**Experiment**: Hash all LLVM+Clang function symbols. Verify zero collisions with FNV-1a 32-bit. If collisions exist, try 64-bit or a different hash. Alternatively, since all symbols are known at build time, generate a perfect hash using e.g. `gperf` or a minimal perfect hash algorithm.

### 2.3 Generate the Resolver

```c
// symtab.c - generated by llvm_dllify.py
// Compiled and linked into LLVM.dll

#include <stdint.h>

struct SymEntry {
    uint32_t hash;
    void *addr;
};

// Forward declarations for all functions (generated)
extern void LLVMContextCreate(void);  // actual signature doesn't matter for addr
extern void _ZN4llvm6Module11getFunctionE...(void);
// ... thousands more

static const struct SymEntry __llvm_symtab[] = {
    // Sorted by hash for binary search
    { 0x00012a3f, (void*)&LLVMContextCreate },
    { 0x0003bc81, (void*)&_ZN4llvm6Module11getFunctionE... },
    // ...
};

static const uint32_t __llvm_symtab_count = 
    sizeof(__llvm_symtab) / sizeof(__llvm_symtab[0]);

__declspec(dllexport) void* __llvm_resolve(uint32_t hash) {
    uint32_t lo = 0, hi = __llvm_symtab_count;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        if (__llvm_symtab[mid].hash < hash)
            lo = mid + 1;
        else
            hi = mid;
    }
    if (lo < __llvm_symtab_count && __llvm_symtab[lo].hash == hash)
        return __llvm_symtab[lo].addr;
    return (void*)0;
}
```

**Problem**: The `extern` declarations need correct signatures or at least correct mangled names so the linker resolves them. Since we're taking the address of each function, we just need the linker to find the symbol. Using `void*` casts from function pointers is technically UB in C, but works universally on Windows. Alternatively, generate as MASM:

```asm
; symtab.asm - generated
; Each entry is {hash_dword, pointer_to_symbol}

.data
PUBLIC ___llvm_symtab
___llvm_symtab:
    DD 00012a3fh
    DQ ?LLVMContextCreate@@...  ; linker fills in the address
    DD 0003bc81h
    DQ ?getFunction@Module@llvm@@...
    ; ... sorted by hash
```

### 2.4 Generate Stub Libs

For each original `.lib` (e.g., `LLVMCore.lib`), generate:

**Thunks (MASM x64):**

```asm
; LLVMCore_thunks.asm - generated by llvm_dllify.py

.data
; Function pointer slots (filled by init code)
PUBLIC __fp_?getFunction@Module@llvm@@QEBA...
__fp_?getFunction@Module@llvm@@QEBA... DQ 0

PUBLIC __fp_?getName@Value@llvm@@QEBA...
__fp_?getName@Value@llvm@@QEBA... DQ 0

; ... one per function symbol in LLVMCore

.code
; Thunks - same mangled name as the original symbol

; Note: the original mangled name becomes the label.
; The consumer's obj references this name, the thunk provides it.

?getFunction@Module@llvm@@QEBA... PROC
    jmp QWORD PTR [__fp_?getFunction@Module@llvm@@QEBA...]
?getFunction@Module@llvm@@QEBA... ENDP

?getName@Value@llvm@@QEBA... PROC
    jmp QWORD PTR [__fp_?getName@Value@llvm@@QEBA...]
?getName@Value@llvm@@QEBA... ENDP

; ... one per function
END
```

**Initializer:**

```c
// LLVMCore_init.c - generated by llvm_dllify.py

#include <stdint.h>

// Import the resolver from LLVM.dll
__declspec(dllimport) void* __llvm_resolve(uint32_t hash);

// Extern references to the function pointer slots
extern void* __fp_getFunction_Module_llvm;  // use mangled names
extern void* __fp_getName_Value_llvm;
// ...

static void __cdecl __init_LLVMCore(void) {
    __fp_getFunction_Module_llvm = __llvm_resolve(0x00012a3f);
    __fp_getName_Value_llvm      = __llvm_resolve(0x0003bc81);
    // ...
    // Optional: assert all non-NULL in debug builds
}

// Register with CRT init
#pragma section(".CRT$XCU", read)
__declspec(allocate(".CRT$XCU"))
static void (__cdecl *__p_init_LLVMCore)(void) = __init_LLVMCore;
```

**Assemble into a `.lib`:**

```bash
ml64 /c /nologo LLVMCore_thunks.asm
cl /c /nologo LLVMCore_init.c
lib /OUT:LLVMCore.lib LLVMCore_thunks.obj LLVMCore_init.obj LLVM.lib
```

The `LLVM.lib` at the end is the import library for LLVM.dll — it provides the `__llvm_resolve` import. Including it in the stub `.lib` means consumers don't need to add LLVM.lib to their link line separately; it comes along implicitly.

---

## Phase 3: Build Integration

### 3.1 Generate the .def File

```python
def generate_def(dll_name, c_api_symbols, data_symbols, resolver_name):
    lines = [f'LIBRARY {dll_name}', 'EXPORTS']
    
    # C API functions - real named exports
    for sym in sorted(c_api_symbols):
        lines.append(f'    {sym}')
    
    # Data symbols - real named exports with DATA keyword
    for sym in sorted(data_symbols):
        lines.append(f'    {sym}  DATA')
    
    # Resolver
    lines.append(f'    {resolver_name}')
    
    return '\n'.join(lines)
```

**Experiment**: Generate a .def file from actual symbols. Count the total exports. Verify it's under 65,535.

### 3.2 Link the DLLs

```bash
# LLVM.dll
link.exe /DLL /DEF:LLVM.def /OUT:LLVM.dll ^
    /WHOLEARCHIVE:LLVMCore.lib ^
    /WHOLEARCHIVE:LLVMSupport.lib ^
    /WHOLEARCHIVE:LLVMCodeGen.lib ^
    ... (all LLVM component static libs) ^
    symtab_llvm.obj

# clang-cpp.dll
link.exe /DLL /DEF:clang-cpp.def /OUT:clang-cpp.dll ^
    /WHOLEARCHIVE:clangAST.lib ^
    /WHOLEARCHIVE:clangSema.lib ^
    ... (all clang component static libs) ^
    symtab_clang.obj ^
    LLVM.lib              <-- import lib from LLVM.dll ^
    stubs\LLVM*.obj        <-- LLVM thunks (embedded in clang-cpp.dll)
```

**Important**: When linking clang-cpp.dll, the clang `.obj` files reference LLVM symbols. These get resolved through the LLVM stub thunks, which become part of clang-cpp.dll itself. The thunks' function pointers get filled in at clang-cpp.dll load time by the `.CRT$XCU` initializer calling `__llvm_resolve` from LLVM.dll.

### 3.3 Generate Forwarder DLLs

```bash
# LLVM-C.dll forwards to LLVM.dll
# Generate def:
#   LIBRARY LLVM-C
#   EXPORTS
#       LLVMContextCreate = LLVM.LLVMContextCreate
#       ...
link.exe /DLL /DEF:LLVM-C-fwd.def /OUT:LLVM-C.dll /NOENTRY

# libclang.dll is relinked against clang-cpp.dll + LLVM.dll stubs.
# LTO.dll is relinked against LLVM.dll stubs.
```

### 3.4 CMake Integration Points

The minimal-invasion approach to integrate into LLVM's build system:

**Option A: Two-stage external build (no LLVM patches)**

```bash
# Stage 1: static libs only
cmake -S llvm -B build-libs ^
    -DLLVM_ENABLE_PROJECTS="clang;lld" ^
    -DLLVM_BUILD_TOOLS=OFF ^
    -DLLVM_INCLUDE_TESTS=OFF ^
    -DCMAKE_INSTALL_PREFIX=sysroot
cmake --build build-libs
cmake --install build-libs

# Intercept
python llvm_dllify.py --sysroot sysroot/

# Stage 2: tools via standalone build
cmake -S llvm -B build-tools ^
    -DLLVM_DIR=sysroot/lib/cmake/llvm ^
    -DClang_DIR=sysroot/lib/cmake/clang
cmake --build build-tools
```

**Option B: In-tree CMake integration (requires LLVM patches)**

Files to modify:

```
llvm/CMakeLists.txt
  - Remove `if(NOT WIN32)` guard on LLVM_BUILD_LLVM_DYLIB
  - Add new option: LLVM_DLLIFY_TOOL (path to llvm_dllify.py)

llvm/cmake/modules/AddLLVM.cmake
  - In the LLVM_LINK_LLVM_DYLIB code path, add Windows support
  - The function llvm_config() already handles switching to shared
  - Need to add: after all LLVM libs are built, run dllify
  - Then tool targets link against stub libs

llvm/tools/llvm-shlib/CMakeLists.txt
  - Remove the `if(NOT WIN32)` guard
  - Add Windows-specific path using the dllify custom command

clang/tools/clang-shlib/CMakeLists.txt
  - Remove the `if(UNIX OR (MINGW AND LLVM_LINK_LLVM_DYLIB))` guard
  - Add Windows dllify path
```

The key CMake pattern:

```cmake
if(WIN32 AND MSVC AND LLVM_BUILD_LLVM_DYLIB)
    # Custom command that runs after all LLVM static libs are built
    # but before any tool targets link
    
    add_custom_command(
        OUTPUT ${CMAKE_BINARY_DIR}/bin/LLVM.dll
               ${CMAKE_BINARY_DIR}/lib/LLVM.lib
        COMMAND ${Python3_EXECUTABLE} ${LLVM_DLLIFY_TOOL}
            --partition LLVM
            --libs ${LLVM_AVAILABLE_LIBS}  
            --lib-dir ${CMAKE_BINARY_DIR}/lib
            --output-dir ${CMAKE_BINARY_DIR}
        DEPENDS ${LLVM_AVAILABLE_LIBS}
        COMMENT "Generating LLVM.dll and stub libraries"
    )
    
    add_custom_target(LLVM_dll ALL
        DEPENDS ${CMAKE_BINARY_DIR}/bin/LLVM.dll
    )
    
    # Make all tool targets depend on LLVM_dll
    # This ensures dllify runs before tool linking
endif()
```

---

## Phase 4: Testing and Validation

### 4.1 Minimal End-to-End Test

Start with a trivial case before tackling all of LLVM:

1. Create a small static library with 5 functions and 1 global variable
2. Run the dllify tool on it
3. Verify the DLL loads and the stub lib works
4. Verify the global variable is accessible

### 4.2 LLVM-C API Test

The C API is the easiest to validate because it has well-defined entry points:

```c
// test_llvm_c.c
#include <llvm-c/Core.h>
#include <stdio.h>

int main() {
    LLVMContextRef ctx = LLVMContextCreate();
    LLVMModuleRef mod = LLVMModuleCreateWithNameInContext("test", ctx);
    LLVMDumpModule(mod);
    LLVMDisposeModule(mod);
    LLVMContextDispose(ctx);
    printf("OK\n");
    return 0;
}
```

Compile and link against the stub libs, run against LLVM.dll.

### 4.3 C++ Consumer Test

```cpp
// test_llvm_cpp.cpp
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/IRBuilder.h"
#include <iostream>

int main() {
    llvm::LLVMContext ctx;
    auto mod = std::make_unique<llvm::Module>("test", ctx);
    llvm::IRBuilder<> builder(ctx);
    // ... create a simple function
    mod->print(llvm::outs(), nullptr);
    return 0;
}
```

This tests vtable resolution (RTTI), template instantiation linkage, and general C++ symbol resolution.

### 4.4 Build Clang Itself

The ultimate test: can clang.exe be built against the shared LLVM.dll + clang-cpp.dll and produce correct output?

```bash
# After dllify:
cmake --build build --target clang

# Test:
build\bin\clang.exe --version
echo "int main(){return 0;}" | build\bin\clang.exe -x c - -o test.exe
test.exe
```

### 4.5 Verify Size Reduction

The main user-visible benefit of shared libs is smaller total install size.

```bash
# Measure before (static):
dir /s build-static\bin\*.exe | findstr "File(s)"

# Measure after (shared):
dir /s build-shared\bin\*.exe | findstr "File(s)"
dir /s build-shared\bin\*.dll | findstr "File(s)"
```

Expected: individual tool `.exe` files shrink dramatically (from ~100MB each to ~1-5MB), while the DLLs total ~100-200MB.

---

## Phase 5: Edge Cases and Hardening

### 5.1 Symbol Classification Edge Cases

Things that need careful handling:

- **Inline functions with out-of-line copies**: MSVC may emit these in some TUs. They should be in the DLL but might not need stubs if they're also inlined at call sites.
- **COMDAT symbols**: Functions in COMDATs (template instantiations, inline functions) may appear in multiple `.lib` files. The dllify tool must deduplicate.
- **Weak symbols**: LLVM uses `selectany` / weak linkage for some globals. These need to be exported from exactly one DLL.
- **Thread-local storage**: Any `__declspec(thread)` or `thread_local` variables need special handling in the .def file.
- **Import/export of exception specifications**: MSVC's EH tables reference typeinfo symbols that must be accessible cross-DLL.

### 5.2 RTTI and dynamic_cast Across DLL Boundaries

MSVC's RTTI implementation requires that `type_info` objects are identical (same address) for `dynamic_cast` to work across DLL boundaries. Since all code lives in the same DLL(s), this should work — but verify with a test case that does `dynamic_cast` on an LLVM type across a DLL boundary.

### 5.3 Static Initializers and Registration

LLVM uses static initializers for pass registration, target registration, etc. These are currently triggered by `#include`-ing a header that references a global with a constructor. When the code moves into a DLL, the static initializers in the DLL run when the DLL is loaded — which is BEFORE the consumer's initializers run. Verify that pass registration and target initialization still work correctly.

### 5.4 Debug Builds

Debug builds with MSVC produce much larger `.obj` files due to `/Zi` debug info. Verify that:
- The 65K data export count stays under limit in debug builds
- The `.pdb` files for the DLLs are usable
- Debugging into DLL code works in Visual Studio

---

## Appendix A: Key LLVM Source Files to Study

```
# CMake build system
llvm/CMakeLists.txt
llvm/cmake/modules/AddLLVM.cmake
llvm/cmake/modules/LLVM-Config.cmake
llvm/cmake/modules/LLVMConfig.cmake.in
llvm/cmake/modules/TableGen.cmake
llvm/tools/llvm-shlib/CMakeLists.txt

# Clang build system
clang/CMakeLists.txt
clang/tools/clang-shlib/CMakeLists.txt
clang/tools/libclang/CMakeLists.txt
clang/include/clang-c/Platform.h          # CINDEX_LINKAGE macro

# LLD build system
lld/CMakeLists.txt

# C API headers (to identify C API symbols)
llvm/include/llvm-c/*.h
clang/include/clang-c/*.h

# Symbol visibility (Linux equivalent of what we're implementing)
llvm/tools/llvm-shlib/simple_version_script.map.in
```

## Appendix B: Tool Dependencies

The `llvm_dllify.py` tool requires:
- Python 3.8+
- MSVC toolchain in PATH (`cl.exe`, `link.exe`, `lib.exe`, `ml64.exe`, `dumpbin.exe`)
- Optionally `undname.exe` for reliable function/data classification

## Appendix C: Rough Implementation Order

1. **Week 1**: Symbol extraction + classification prototype. Run on real LLVM build. Gather census data.
2. **Week 2**: Hash function selection and collision testing. Resolver codegen. Test with a toy library.
3. **Week 3**: Stub lib generation (MASM thunks + CRT init). Test with toy library end-to-end.
4. **Week 4**: Full LLVM.dll generation. Test with C API consumer.
5. **Week 5**: C++ consumer tests. RTTI/vtable/exception handling validation.
6. **Week 6**: clang-cpp.dll (depends on LLVM.dll). Two-DLL dependency chain.
7. **Week 7**: Forwarder DLLs. Build clang.exe against shared libs.
8. **Week 8**: CMake integration. Polish. Debug build testing.

## Appendix D: Open Questions

1. **MASM symbol name length limits**: MSVC mangled names can be extremely long. Does MASM handle 4000-character labels? If not, we may need to use `ml64 /Gc` or generate COFF `.obj` files directly (using a small COFF writer, or `llvm-mc`).

2. **Incremental builds**: After the initial dllify, if one `.lib` changes, do we need to regenerate everything? Ideally the tool detects which symbols changed and updates only affected stubs.

3. **Cross-DLL global state**: LLVM's `ManagedStatic` and `cl::opt` (command-line option registration) use global state. Verify these work when the code is in a DLL vs. statically linked.

4. **LTO compatibility**: If consumers build with LTO (`/GL`), the `.obj` files contain MSIL bitcode, not native code. `dumpbin /SYMBOLS` may not work. The dllify tool may need to handle this case separately or require non-LTO builds.

5. **ARM64 Windows**: The thunk generation assumes x64 (`jmp QWORD PTR`). ARM64 Windows needs different thunk codegen (`adrp x16, ...; ldr x16, ...; br x16`). Parameterize the MASM template.

6. **Perfect hash vs. binary search**: For ~80K symbols, binary search is 17 iterations. A perfect hash is O(1) but more complex to generate. Profile the init time to decide if it matters (it probably doesn't — init runs once).