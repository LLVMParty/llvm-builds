#!/usr/bin/env bash
#
# build-macos.sh — Build LLVM distributions for macOS arm64 and x86_64.
#
# Usage:
#   ./build-macos.sh                        # both arches, default LLVM version
#   ./build-macos.sh --version 21.1.6       # specific version
#   ./build-macos.sh --arch x86_64          # single arch only
#   ./build-macos.sh --arch all             # both arches
#   ./build-macos.sh --jobs 8               # override parallelism
#   ./build-macos.sh --assertions           # enable assertions
#   ./build-macos.sh --min-macos 14.0       # raise deployment target
#
# Output: dist/llvm-<version>-macos-<arch>.zip
#
# Prerequisites: Xcode command line tools (xcode-select --install),
#                cmake, ninja (brew install cmake ninja)
#
# NOTE on cross-arch builds:
#   Building x86_64 on Apple Silicon requires Rosetta 2, because build-time
#   LLVM tools are produced for the target architecture and executed during the
#   build. Building arm64 on an Intel Mac is not supported by this script.
#
set -euo pipefail

normalize_arch() {
    case "$1" in
        arm64|aarch64) echo "arm64" ;;
        x86_64|amd64)  echo "x86_64" ;;
        *)             echo "Error: unsupported architecture '$1' (expected arm64, x86_64, or all)" >&2; exit 1 ;;
    esac
}

LLVM_VERSION="21.1.6"
ARCHES=("arm64" "x86_64")
PARALLEL_JOBS="$(sysctl -n hw.performancecores 2>/dev/null || sysctl -n hw.ncpu)"
OUTPUT_DIR="$(pwd)/dist"
CMAKE_EXTRA_ARGS=""
LLVM_ENABLE_ASSERTIONS="OFF"
MIN_MACOS="13.0"
BUILD_DIR="$(pwd)/.llvm-build"
INSTALL_ROOT="$(pwd)/.llvm-install"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)    LLVM_VERSION="$2"; shift 2 ;;
        --arch)
            if [[ "$2" == "all" ]]; then
                ARCHES=("arm64" "x86_64")
            else
                ARCHES=("$(normalize_arch "$2")")
            fi
            shift 2
            ;;
        --jobs)       PARALLEL_JOBS="$2"; shift 2 ;;
        --output)     OUTPUT_DIR="$2";   shift 2 ;;
        --cmake-args) CMAKE_EXTRA_ARGS="$2"; shift 2 ;;
        --assertions) LLVM_ENABLE_ASSERTIONS="ON"; shift ;;
        --min-macos)  MIN_MACOS="$2"; shift 2 ;;
        *)            echo "Unknown option: $1"; exit 1 ;;
    esac
done

HOST_ARCH="$(normalize_arch "$(uname -m)")"

ASSERTIONS_SUFFIX=""
[[ "${LLVM_ENABLE_ASSERTIONS}" == "ON" ]] && ASSERTIONS_SUFFIX="-assertions"

echo "=== LLVM ${LLVM_VERSION} ==="
echo "=== Host architecture: ${HOST_ARCH} ==="
echo "=== Target architectures: ${ARCHES[*]} ==="
echo "=== Deployment target: macOS ${MIN_MACOS} ==="
echo "=== Assertions: ${LLVM_ENABLE_ASSERTIONS} ==="
echo "=== Parallel jobs: ${PARALLEL_JOBS} ==="
echo "=== Output: ${OUTPUT_DIR} ==="
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

mkdir -p "${BUILD_DIR}" "${OUTPUT_DIR}" "${INSTALL_ROOT}"

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

for ARCH in "${ARCHES[@]}"; do
    if [[ "${HOST_ARCH}" == "arm64" && "${ARCH}" == "x86_64" ]]; then
        if ! arch -x86_64 /usr/bin/true &>/dev/null; then
            echo "Error: building macOS x86_64 on Apple Silicon requires Rosetta 2."
            echo "Install it with: softwareupdate --install-rosetta --agree-to-license"
            exit 1
        fi
    elif [[ "${HOST_ARCH}" == "x86_64" && "${ARCH}" == "arm64" ]]; then
        echo "Error: building macOS arm64 on an Intel Mac is not supported by this script."
        echo "Run this on Apple Silicon, or use --arch x86_64 on this host."
        exit 1
    fi

    TARGET_TRIPLE="${ARCH}-apple-darwin"
    BUILD_ARCH_DIR="${BUILD_DIR}/build-${ARCH}"
    INSTALL_DIR="${INSTALL_ROOT}/${ARCH}"

    echo "──────────────────────────────────────────────"
    echo "  Building for ${ARCH}"
    echo "──────────────────────────────────────────────"

    # --- configure & build ------------------------------------------------
    echo "=== Configuring ==="
    cmake -G Ninja -S "${SOURCE_DIR}/llvm" -B "${BUILD_ARCH_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}" \
        -DCMAKE_OSX_DEPLOYMENT_TARGET="${MIN_MACOS}" \
        -DLLVM_DEFAULT_TARGET_TRIPLE="${TARGET_TRIPLE}" \
        -DLLVM_HOST_TRIPLE="${TARGET_TRIPLE}" \
        -DCMAKE_OSX_ARCHITECTURES="${ARCH}" \
        -DLLVM_BUILD_LLVM_DYLIB=ON \
        -DLLVM_LINK_LLVM_DYLIB=ON \
        -DLLVM_PARALLEL_LINK_JOBS=1 \
        -DLLVM_ENABLE_RTTI=ON \
        -DLLVM_ENABLE_EH=ON \
        -DLLVM_ENABLE_ASSERTIONS="${LLVM_ENABLE_ASSERTIONS}" \
        -DLLVM_ENABLE_DUMP="${LLVM_ENABLE_ASSERTIONS}" \
        -DLLVM_ENABLE_PROJECTS="clang;lld" \
        -DLLVM_TARGETS_TO_BUILD=all \
        -DLLVM_ENABLE_LIBEDIT=OFF \
        -DLLVM_INCLUDE_TESTS=OFF \
        -DLLVM_INCLUDE_BENCHMARKS=OFF \
        -DLLVM_INCLUDE_EXAMPLES=OFF \
        -DLLVM_INCLUDE_DOCS=OFF \
        -DLLVM_ENABLE_BINDINGS=OFF \
        -DLLVM_INSTALL_UTILS=ON \
        -DLLVM_ENABLE_ZSTD=OFF \
        ${CMAKE_EXTRA_ARGS}

    echo "=== Building with ${PARALLEL_JOBS} compile jobs, 1 link job ==="
    ninja -C "${BUILD_ARCH_DIR}" -j"${PARALLEL_JOBS}"

    echo "=== Installing ==="
    rm -rf "${INSTALL_DIR}"
    cmake --install "${BUILD_ARCH_DIR}" --prefix "${INSTALL_DIR}"

    # --- package ----------------------------------------------------------
    echo "=== Packaging ==="
    ZIP_NAME="llvm-${LLVM_VERSION}-macos-${ARCH}${ASSERTIONS_SUFFIX}.zip"
    rm -f "${OUTPUT_DIR}/${ZIP_NAME}"
    # -9 maximizes compression; -y preserves symlinks instead of duplicating targets.
    (cd "${INSTALL_DIR}" && zip -qr9y "${OUTPUT_DIR}/${ZIP_NAME}" .)

    echo ""
    echo "  ✓ ${OUTPUT_DIR}/${ZIP_NAME}"
    echo ""
    ls -lh "${OUTPUT_DIR}/${ZIP_NAME}"
    echo ""
done

echo "=== Done ==="
ls -lh "${OUTPUT_DIR}"/llvm-"${LLVM_VERSION}"-macos-*"${ASSERTIONS_SUFFIX}".zip

# --- cleanup hint ---------------------------------------------------------
echo ""
echo "Build artifacts are in ${BUILD_DIR} (~10+ GB per arch)."
echo "Install trees are in ${INSTALL_ROOT}."
echo "Run 'rm -rf ${BUILD_DIR} ${INSTALL_ROOT}' to reclaim space."
