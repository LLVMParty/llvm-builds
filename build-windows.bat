@echo off
setlocal enabledelayedexpansion

REM ==========================================================================
REM build-windows.bat — Build LLVM for Windows x86_64.
REM
REM Usage:
REM   build-windows.bat
REM   build-windows.bat --version 21.1.6
REM   build-windows.bat --jobs 8
REM   build-windows.bat --assertions
REM   build-windows.bat --llvm-dll     (experimental LLVM_BUILD_LLVM_DYLIB equivalent)
REM   set LLVM_BUILD_LLVM_DYLIB=ON && build-windows.bat
REM
REM Output: dist\llvm-<version>-windows-x86_64.zip
REM
REM Prerequisites:
REM   - Visual Studio 2022 with C++ workload
REM   - CMake (https://cmake.org or via VS installer)
REM   - Ninja (https://ninja-build.org or via VS installer)
REM   - 7-Zip or PowerShell 5+ (for zipping)
REM
REM Run from a plain cmd.exe — the script finds and calls vcvarsall.bat itself.
REM ==========================================================================

set LLVM_VERSION=21.1.6
set PARALLEL_JOBS=%NUMBER_OF_PROCESSORS%
set OUTPUT_DIR=%cd%\dist
set CMAKE_EXTRA_ARGS=
set LLVM_ENABLE_ASSERTIONS=OFF
if not defined LLVM_BUILD_LLVM_DYLIB set LLVM_BUILD_LLVM_DYLIB=OFF
set ARCH=x86_64

:parse_args
if "%~1"=="" goto done_args
if "%~1"=="--version"    ( set LLVM_VERSION=%~2& shift & shift & goto parse_args )
if "%~1"=="--jobs"       ( set PARALLEL_JOBS=%~2& shift & shift & goto parse_args )
if "%~1"=="--output"     ( set OUTPUT_DIR=%~2& shift & shift & goto parse_args )
if "%~1"=="--cmake-args" ( set CMAKE_EXTRA_ARGS=%~2& shift & shift & goto parse_args )
if "%~1"=="--assertions" ( set LLVM_ENABLE_ASSERTIONS=ON& shift & goto parse_args )
if "%~1"=="--llvm-dll"   ( set LLVM_BUILD_LLVM_DYLIB=ON& shift & goto parse_args )
echo Unknown option: %~1
exit /b 1
:done_args

echo === LLVM %LLVM_VERSION% ===
echo === Architecture: %ARCH% ===
echo === Assertions: %LLVM_ENABLE_ASSERTIONS% ===
echo === LLVM.dll mode: %LLVM_BUILD_LLVM_DYLIB% ===
echo === Parallel jobs: %PARALLEL_JOBS% ===
echo.

REM --- Find Visual Studio -------------------------------------------------
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo Error: vswhere.exe not found. Install Visual Studio 2022 with C++ workload.
    exit /b 1
)

for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -property installationPath`) do set "VS_PATH=%%i"
if not defined VS_PATH (
    echo Error: Visual Studio installation not found.
    exit /b 1
)

set "VCVARSALL=%VS_PATH%\VC\Auxiliary\Build\vcvarsall.bat"
if not exist "%VCVARSALL%" (
    echo Error: vcvarsall.bat not found at %VCVARSALL%
    exit /b 1
)

echo === Setting up MSVC environment ===
call "%VCVARSALL%" x64
if errorlevel 1 (
    echo Error: vcvarsall.bat failed.
    exit /b 1
)

REM --- Check prerequisites ------------------------------------------------
where cmake >nul 2>&1 || (
    echo Error: cmake not found. Install via Visual Studio or https://cmake.org
    exit /b 1
)
where ninja >nul 2>&1 || (
    echo Error: ninja not found. Install via Visual Studio or https://ninja-build.org
    exit /b 1
)
where python >nul 2>&1 || (
    echo Error: python not found. Python 3.8+ is required for LLVM.dll mode and LLVM's build tools.
    exit /b 1
)

REM --- Download source ----------------------------------------------------
set BUILD_DIR=%cd%\.llvm-build
set INSTALL_DIR=%cd%\.llvm-install
set SOURCE_DIR=%BUILD_DIR%\llvm-project-%LLVM_VERSION%.src
set TARBALL=%BUILD_DIR%\llvm-project-%LLVM_VERSION%.src.tar.xz
set URL=https://github.com/llvm/llvm-project/releases/download/llvmorg-%LLVM_VERSION%/llvm-project-%LLVM_VERSION%.src.tar.xz

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"

if exist "%SOURCE_DIR%\llvm" (
    echo === Source already extracted, skipping download ===
) else (
    if not exist "%TARBALL%" (
        echo === Downloading LLVM %LLVM_VERSION% ===
        curl -L -o "%TARBALL%" "%URL%"
        if errorlevel 1 (
            echo Error: Download failed.
            exit /b 1
        )
    )
    echo === Extracting ===
    cmake -E tar xf "%TARBALL%" --format=7zip
    if errorlevel 1 (
        REM Fallback: cmake tar should handle .tar.xz
        tar xf "%TARBALL%" -C "%BUILD_DIR%"
    )
    if not exist "%SOURCE_DIR%\llvm" (
        REM Handle case where extraction happened in current dir
        if exist "llvm-project-%LLVM_VERSION%.src\llvm" (
            move "llvm-project-%LLVM_VERSION%.src" "%SOURCE_DIR%"
        ) else (
            echo Error: Extraction failed — cannot find %SOURCE_DIR%\llvm
            exit /b 1
        )
    )
)

REM --- Configure ----------------------------------------------------------
REM LLVM_BUILD_LLVM_DYLIB is implemented externally by tools\llvm_dllify.py.
REM In that mode, configure LLVM as a normal static MSVC build, build the
REM a normal static MSVC build first, then replace the installed component
REM libraries listed by LLVM's own libllvm-c.args with stub libraries that load
REM LLVM.dll for downstream consumers.
set LLVM_BUILD_LLVM_C_DYLIB=ON

echo === Configuring ===
cmake -G Ninja -S "%SOURCE_DIR%\llvm" -B "%BUILD_DIR%\build" ^
    -DCMAKE_C_COMPILER=clang-cl ^
    -DCMAKE_CXX_COMPILER=clang-cl ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DCMAKE_INSTALL_PREFIX="%INSTALL_DIR%" ^
    -DLLVM_BUILD_LLVM_C_DYLIB=%LLVM_BUILD_LLVM_C_DYLIB% ^
    -DLLVM_PARALLEL_LINK_JOBS=1 ^
    -DLLVM_ENABLE_RTTI=ON ^
    -DLLVM_ENABLE_EH=ON ^
    -DLLVM_ENABLE_ASSERTIONS=%LLVM_ENABLE_ASSERTIONS% ^
    -DLLVM_ENABLE_DUMP=%LLVM_ENABLE_ASSERTIONS% ^
    -DLLVM_TARGETS_TO_BUILD=all ^
    -DLLVM_ENABLE_PROJECTS=clang;lld ^
    -DLLVM_ENABLE_LIBEDIT=OFF ^
    -DLLVM_ENABLE_DIA_SDK=OFF ^
    -DLLVM_ENABLE_LIBXML2=OFF ^
    -DLLVM_INCLUDE_TESTS=OFF ^
    -DLLVM_INCLUDE_BENCHMARKS=OFF ^
    -DLLVM_INCLUDE_EXAMPLES=OFF ^
    -DLLVM_INCLUDE_DOCS=OFF ^
    -DLLVM_ENABLE_BINDINGS=OFF ^
    -DLLVM_INSTALL_UTILS=ON ^
    -DLLVM_USE_SYMLINKS=ON ^
    %CMAKE_EXTRA_ARGS%

if errorlevel 1 (
    echo Error: CMake configure failed.
    exit /b 1
)

REM --- Build --------------------------------------------------------------
echo === Building with %PARALLEL_JOBS% compile jobs, 1 link job ===
ninja -C "%BUILD_DIR%\build" -j%PARALLEL_JOBS%
if errorlevel 1 (
    echo Error: Build failed.
    exit /b 1
)

if "%LLVM_BUILD_LLVM_DYLIB%"=="ON" (
    if not exist "%cd%\tools\llvm_dllify.py" (
        echo Error: tools\llvm_dllify.py not found.
        exit /b 1
    )

    echo === Generating LLVM.dll and component stub libraries ===
    python "%cd%\tools\llvm_dllify.py" build ^
        --libsfile "%BUILD_DIR%\build\libllvm-c.args" ^
        --output-dir "%BUILD_DIR%\build" ^
        --bin-dir "%BUILD_DIR%\build\bin" ^
        --out-lib-dir "%BUILD_DIR%\build\lib" ^
        --work-dir "%BUILD_DIR%\build\dllify-work" ^
        --llvm-c-forwarder
    if errorlevel 1 (
        echo Error: llvm_dllify failed.
        exit /b 1
    )
)

REM --- Install ------------------------------------------------------------
echo === Installing ===
cmake --install "%BUILD_DIR%\build" --prefix "%INSTALL_DIR%"
if errorlevel 1 (
    echo Error: Install failed.
    exit /b 1
)

if "%LLVM_BUILD_LLVM_DYLIB%"=="ON" (
    echo === Installing LLVM.dll artifacts ===
    if not exist "%INSTALL_DIR%\bin" mkdir "%INSTALL_DIR%\bin"
    if not exist "%INSTALL_DIR%\lib" mkdir "%INSTALL_DIR%\lib"
    if not exist "%INSTALL_DIR%\share" mkdir "%INSTALL_DIR%\share"
    copy /Y "%BUILD_DIR%\build\bin\LLVM.dll" "%INSTALL_DIR%\bin\LLVM.dll" >nul
    copy /Y "%BUILD_DIR%\build\bin\LLVM-C.dll" "%INSTALL_DIR%\bin\LLVM-C.dll" >nul
    copy /Y "%BUILD_DIR%\build\lib\LLVM.lib" "%INSTALL_DIR%\lib\LLVM.lib" >nul
    copy /Y "%BUILD_DIR%\build\lib\LLVM-C.lib" "%INSTALL_DIR%\lib\LLVM-C.lib" >nul
    if exist "%BUILD_DIR%\build\dllify-work\manifest.json" copy /Y "%BUILD_DIR%\build\dllify-work\manifest.json" "%INSTALL_DIR%\share\llvm-dllify-manifest.json" >nul
)

REM --- Package ------------------------------------------------------------
echo === Packaging ===
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

set ASSERTIONS_SUFFIX=
if "%LLVM_ENABLE_ASSERTIONS%"=="ON" set ASSERTIONS_SUFFIX=-assertions
set LLVM_DLL_SUFFIX=
if "%LLVM_BUILD_LLVM_DYLIB%"=="ON" set LLVM_DLL_SUFFIX=-dll

set ZIP_NAME=llvm-%LLVM_VERSION%-windows-%ARCH%%ASSERTIONS_SUFFIX%%LLVM_DLL_SUFFIX%.zip
if exist "%OUTPUT_DIR%\%ZIP_NAME%" del /f /q "%OUTPUT_DIR%\%ZIP_NAME%"

set ZIP_EXE=
for %%Z in (7z.exe 7zz.exe 7za.exe) do (
    if not defined ZIP_EXE (
        for /f "delims=" %%P in ('where %%Z 2^>nul') do (
            if not defined ZIP_EXE set "ZIP_EXE=%%P"
        )
    )
)

pushd "%INSTALL_DIR%"
if defined ZIP_EXE (
    "%ZIP_EXE%" a -tzip -mx=9 "%OUTPUT_DIR%\%ZIP_NAME%" .\*
) else (
    powershell -NoProfile -Command "Compress-Archive -Path '.\*' -DestinationPath '%OUTPUT_DIR%\%ZIP_NAME%' -CompressionLevel Optimal -Force"
)
set PACKAGE_RESULT=!ERRORLEVEL!
popd

if not "%PACKAGE_RESULT%"=="0" (
    echo Error: Packaging failed.
    exit /b 1
)

echo.
echo   === Done ===
echo   %OUTPUT_DIR%\%ZIP_NAME%
echo.
echo Build artifacts are in %BUILD_DIR% (10+ GB).
echo Run 'rmdir /s /q %BUILD_DIR% %INSTALL_DIR%' to reclaim space.