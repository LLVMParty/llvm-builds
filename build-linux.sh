#!/usr/bin/env bash
#
# build.sh — Build LLVM distributions for linux x86_64 and aarch64.
#
# Usage:
#   ./build.sh                        # both arches, default LLVM version
#   ./build.sh --version 21.1.0       # specific version
#   ./build.sh --arch x86_64          # single arch only
#   ./build.sh --jobs 8               # override parallelism
#   ./build.sh --assertions           # enable LLVM assertions (for development)
#
# Output: dist/llvm-<version>-linux-<arch>.zip
#
# NOTE on cross-arch builds:
#   Building LLVM under QEMU emulation is extremely slow (expect 10-20x
#   slower than native). Prefer building natively on each arch:
#     - x86_64: any modern x86_64 host
#     - aarch64: an ARM server (Graviton, Ampere, Apple Silicon via Lima/UTM)
#   If you only have x86_64 hardware and must cross-build aarch64, see the
#   CROSS_COMPILE note below.
#
set -euo pipefail

LLVM_VERSION="21.1.6"
ARCHES=("x86_64" "aarch64")
PARALLEL_JOBS=""
OUTPUT_DIR="$(pwd)/dist"
CMAKE_EXTRA_ARGS=""
LLVM_ENABLE_ASSERTIONS="OFF"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)    LLVM_VERSION="$2"; shift 2 ;;
        --arch)       ARCHES=("$2");     shift 2 ;;
        --jobs)       PARALLEL_JOBS="$2"; shift 2 ;;
        --output)     OUTPUT_DIR="$2";   shift 2 ;;
        --cmake-args) CMAKE_EXTRA_ARGS="$2"; shift 2 ;;
        --assertions) LLVM_ENABLE_ASSERTIONS="ON"; shift ;;
        *)            echo "Unknown option: $1"; exit 1 ;;
    esac
done

mkdir -p "$OUTPUT_DIR"

echo "=== LLVM ${LLVM_VERSION} ==="
echo "=== Architectures: ${ARCHES[*]} ==="
echo "=== Assertions: ${LLVM_ENABLE_ASSERTIONS} ==="
echo "=== Output: ${OUTPUT_DIR} ==="
echo ""

for ARCH in "${ARCHES[@]}"; do
    echo "──────────────────────────────────────────────"
    echo "  Building for ${ARCH}"
    echo "──────────────────────────────────────────────"

    HOST_ARCH="$(uname -m)"
    # Normalise Apple Silicon's "arm64" to Docker's "aarch64"
    [[ "$HOST_ARCH" == "arm64" ]] && HOST_ARCH="aarch64"
    if [[ "$ARCH" != "$HOST_ARCH" ]]; then
        echo ""
        echo "  ⚠  Cross-arch build detected (host=$(uname -m), target=${ARCH})."
        if [[ "$(uname -s)" == "Darwin" ]]; then
            echo "     Docker Desktop will use Rosetta 2 (make sure it's enabled in settings)."
            echo "     Expect ~2-3x slower than native."
        else
            echo "     This will use QEMU emulation and be VERY slow for LLVM."
            echo "     Consider building natively on ${ARCH} hardware instead."
        fi
        echo ""
        # On Linux, ensure binfmt/QEMU is registered for the target arch.
        # On macOS, Docker Desktop handles emulation via Rosetta/QEMU internally.
        if [[ "$(uname -s)" == "Linux" ]]; then
            docker run --rm --privileged tonistiigi/binfmt --install "${ARCH}" 2>/dev/null || true
        fi
    fi

    DOCKER_BUILDKIT=1 docker build \
        --build-arg ARCH="${ARCH}" \
        --build-arg LLVM_VERSION="${LLVM_VERSION}" \
        --build-arg LLVM_ENABLE_ASSERTIONS="${LLVM_ENABLE_ASSERTIONS}" \
        --build-arg PARALLEL_JOBS="${PARALLEL_JOBS}" \
        --build-arg CMAKE_EXTRA_ARGS="${CMAKE_EXTRA_ARGS}" \
        --platform "linux/${ARCH}" \
        --target export \
        --output "type=local,dest=${OUTPUT_DIR}" \
        -f Dockerfile \
        .

    echo ""
    echo "  ✓ ${OUTPUT_DIR}/llvm-${LLVM_VERSION}-linux-${ARCH}.zip"
    echo ""
done

echo "=== Done ==="
ls -lh "${OUTPUT_DIR}"/llvm-*.zip