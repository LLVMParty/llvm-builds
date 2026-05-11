#!/usr/bin/env bash
#
# build-macos.sh — Build LLVM for macOS arm64.
#
# Usage:
#   ./build-macos.sh                        # defaults
#   ./build-macos.sh --version 21.1.6       # specific version
#   ./build-macos.sh --jobs 8               # override parallelism
#   ./build-macos.sh --assertions           # enable assertions
#   ./build-macos.sh --min-macos 14.0       # raise deployment target
#
# Output: dist/llvm-<version>-macos-arm64.zip
#
# Prerequisites: Xcode command line tools (xcode-select --install),
#                cmake, ninja (brew install cmake ninja)
#
set -euo pipefail

LLVM_VERSION="21.1.6"
PARALLEL_JOBS="$(sysctl -n hw.performancecores 2>/dev/null || sysctl -n hw.ncpu)"
OUTPUT_DIR="$(pwd)/dist"
CMAKE_EXTRA_ARGS=""
LLVM_ENABLE_ASSERTIONS="OFF"
MIN_MACOS="13.0"
BUILD_DIR="$(pwd)/.llvm-build"
INSTALL_DIR="$(pwd)/.llvm-install"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)    LLVM_VERSION="$2"; shift 2 ;;
        --jobs)       PARALLEL_JOBS="$2"; shift 2 ;;
        --output)     OUTPUT_DIR="$2";   shift 2 ;;
        --cmake-args) CMAKE_EXTRA_ARGS="$2"; shift 2 ;;
        --assertions) LLVM_ENABLE_ASSERTIONS="ON"; shift ;;
        --min-macos)  MIN_MACOS="$2"; shift 2 ;;
        *)            echo "Unknown option: $1"; exit 1 ;;
    esac
done

ARCH="$(uname -m)"

echo "=== LLVM ${LLVM_VERSION} ==="
echo "=== Architecture: ${ARCH} ==="
echo "=== Deployment target: macOS ${MIN_MACOS} ==="
echo "=== Assertions: ${LLVM_ENABLE_ASSERTIONS} ==="
echo "=== Parallel jobs: ${PARALLEL_JOBS} ==="
echo ""

# --- prerequisites --------------------------------------------------------
for cmd in cmake ninja; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Error: ${cmd} not found. Install with: brew install ${cmd}"
        exit 1
    fi
done

# --- download source ------------------------------------------------------
SOURCE_DIR="${BUILD_DIR}/llvm-project-${LLVM_VERSION}.src"
TARBALL="${BUILD_DIR}/llvm-project-${LLVM_VERSION}.src.tar.xz"
URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-${LLVM_VERSION}/llvm-project-${LLVM_VERSION}.src.tar.xz"

mkdir -p "${BUILD_DIR}"

if [[ -d "${SOURCE_DIR}/llvm" ]]; then
    echo "=== Source already extracted, skipping download ==="
else
    if [[ ! -f "${TARBALL}" ]]; then
        echo "=== Downloading LLVM ${LLVM_VERSION} ==="
        curl -L -o "${TARBALL}" "${URL}"
    fi
    echo "=== Extracting ==="
    tar xf "${TARBALL}" -C "${BUILD_DIR}"
fi

# --- configure & build ----------------------------------------------------
echo "=== Configuring ==="
cmake -G Ninja -S "${SOURCE_DIR}/llvm" -B "${BUILD_DIR}/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="${MIN_MACOS}" \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DLLVM_BUILD_LLVM_DYLIB=ON \
    -DLLVM_LINK_LLVM_DYLIB=ON \
    -DLLVM_PARALLEL_LINK_JOBS=1 \
    -DLLVM_ENABLE_RTTI=ON \
    -DLLVM_ENABLE_EH=ON \
    -DLLVM_ENABLE_ASSERTIONS="${LLVM_ENABLE_ASSERTIONS}" \
    -DLLVM_ENABLE_DUMP="${LLVM_ENABLE_ASSERTIONS}" \
    -DLLVM_TARGETS_TO_BUILD=all \
    -DLLVM_ENABLE_LIBEDIT=OFF \
    -DLLVM_INCLUDE_TESTS=OFF \
    -DLLVM_INCLUDE_BENCHMARKS=OFF \
    -DLLVM_INCLUDE_EXAMPLES=OFF \
    -DLLVM_INCLUDE_DOCS=OFF \
    -DLLVM_ENABLE_BINDINGS=OFF \
    -DLLVM_INSTALL_UTILS=OFF \
    ${CMAKE_EXTRA_ARGS}

echo "=== Building with ${PARALLEL_JOBS} compile jobs, 1 link job ==="
ninja -C "${BUILD_DIR}/build" -j"${PARALLEL_JOBS}"

echo "=== Installing ==="
cmake --install "${BUILD_DIR}/build" --prefix "${INSTALL_DIR}"

# --- package --------------------------------------------------------------
echo "=== Packaging ==="
mkdir -p "${OUTPUT_DIR}"

ASSERTIONS_SUFFIX=""
[[ "${LLVM_ENABLE_ASSERTIONS}" == "ON" ]] && ASSERTIONS_SUFFIX="-assertions"
ZIP_NAME="llvm-${LLVM_VERSION}-macos-arm64${ASSERTIONS_SUFFIX}.zip"
rm -f "${OUTPUT_DIR}/${ZIP_NAME}"
# -9 maximizes compression; -y preserves symlinks instead of duplicating targets.
(cd "${INSTALL_DIR}" && zip -qr9y "${OUTPUT_DIR}/${ZIP_NAME}" .)

echo ""
echo "  ✓ ${OUTPUT_DIR}/${ZIP_NAME}"
echo ""
ls -lh "${OUTPUT_DIR}/${ZIP_NAME}"

# --- cleanup hint ---------------------------------------------------------
echo ""
echo "Build artifacts are in ${BUILD_DIR} (~10+ GB)."
echo "Run 'rm -rf ${BUILD_DIR} ${INSTALL_DIR}' to reclaim space."