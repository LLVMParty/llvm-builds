# syntax=docker/dockerfile:1
#
# Build LLVM for manylinux_2_28 with full C/C++ API.
# The base image is arch-specific — pass ARCH as a build arg.
#
#   docker buildx build --build-arg ARCH=x86_64 --build-arg LLVM_VERSION=21.1.6 ...
#   docker buildx build --build-arg ARCH=aarch64 --build-arg LLVM_VERSION=21.1.6 ...
#

ARG ARCH=x86_64
FROM quay.io/pypa/manylinux_2_28_${ARCH} AS builder

ARG LLVM_VERSION=21.1.6
ARG LLVM_ENABLE_ASSERTIONS=OFF
ARG NINJA_VERSION=1.13.2
ARG PARALLEL_JOBS=""
ARG CMAKE_EXTRA_ARGS=""

LABEL description="LLVM ${LLVM_VERSION} build environment (manylinux_2_28)"

# --- toolchain -----------------------------------------------------------
RUN yum install -y \
        gcc-toolset-12-gcc \
        gcc-toolset-12-gcc-c++ \
        gcc-toolset-12-binutils \
        cmake \
        make \
        python3 \
        zlib-devel \
        libzstd-devel \
        libxml2-devel \
        zip \
    && yum clean all

# Activate the newer toolchain for the rest of the build.
ENV PATH="/opt/rh/gcc-toolset-12/root/usr/bin:${PATH}" \
    CC=gcc \
    CXX=g++

# The distro ninja (1.8.2 in manylinux_2_28) cannot read LLVM 21's
# generated build.ninja because some depfile rules have multiple outputs.
# Build a newer ninja before configuring LLVM.
ADD https://github.com/ninja-build/ninja/archive/refs/tags/v${NINJA_VERSION}.tar.gz \
    /tmp/ninja.tar.gz

RUN mkdir -p /tmp/ninja \
    && tar xf /tmp/ninja.tar.gz -C /tmp/ninja --strip-components=1 \
    && cmake -G "Unix Makefiles" -S /tmp/ninja -B /tmp/ninja/build \
        -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/ninja/build --parallel "$(nproc)" \
    && install -m 0755 /tmp/ninja/build/ninja /usr/local/bin/ninja \
    && rm -rf /tmp/ninja /tmp/ninja.tar.gz \
    && ninja --version

WORKDIR /build

# --- source ---------------------------------------------------------------
ADD https://github.com/llvm/llvm-project/releases/download/llvmorg-${LLVM_VERSION}/llvm-project-${LLVM_VERSION}.src.tar.xz \
    /build/llvm-project.tar.xz

RUN tar xf llvm-project.tar.xz --strip-components=1 \
    && rm llvm-project.tar.xz

# --- build ----------------------------------------------------------------
#
# CMake flags explained:
#   LLVM_BUILD_LLVM_DYLIB      — produce a single libLLVM.so
#   LLVM_LINK_LLVM_DYLIB       — make all tools link against it
#   LLVM_PARALLEL_LINK_JOBS=1  — link one target at a time (each eats ~10 GB)
#   LLVM_ENABLE_RTTI/EH        — required for C++ API consumers
#   LLVM_ENABLE_ASSERTIONS     — off by default; enable for development builds
#   LLVM_ENABLE_DUMP           — tied to assertions (dump() methods)
#   LLVM_TARGETS_TO_BUILD      — all backends
#   LLVM_INCLUDE_*=OFF         — skip tests/benchmarks/examples/docs
#   LLVM_ENABLE_BINDINGS=OFF   — skip OCaml/Go bindings we don't need
#   LLVM_INSTALL_UTILS=OFF     — skip FileCheck, count, not-etc.
#   LLVM_ENABLE_LIBEDIT=OFF    — avoid optional dependency
#
ARG LLVM_ENABLE_ASSERTIONS
RUN JOBS="${PARALLEL_JOBS:-$(nproc)}"; \
    echo "=== Building with ${JOBS} compile jobs, 1 link job ===" \
    && cmake -G Ninja -S llvm -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/llvm \
        -DLLVM_BUILD_LLVM_DYLIB=ON \
        -DLLVM_LINK_LLVM_DYLIB=ON \
        -DLLVM_PARALLEL_LINK_JOBS=1 \
        -DLLVM_ENABLE_RTTI=ON \
        -DLLVM_ENABLE_EH=ON \
        -DLLVM_ENABLE_ASSERTIONS=${LLVM_ENABLE_ASSERTIONS} \
        -DLLVM_ENABLE_DUMP=${LLVM_ENABLE_ASSERTIONS} \
        -DLLVM_TARGETS_TO_BUILD=all \
        -DLLVM_ENABLE_LIBEDIT=OFF \
        -DLLVM_INCLUDE_TESTS=OFF \
        -DLLVM_INCLUDE_BENCHMARKS=OFF \
        -DLLVM_INCLUDE_EXAMPLES=OFF \
        -DLLVM_INCLUDE_DOCS=OFF \
        -DLLVM_ENABLE_BINDINGS=OFF \
        -DLLVM_INSTALL_UTILS=OFF \
        ${CMAKE_EXTRA_ARGS} \
    && ninja -C build -j"${JOBS}" \
    && cmake --install build --prefix /opt/llvm

# --- package --------------------------------------------------------------
# Strip debug info to shrink the output significantly.
RUN find /opt/llvm/lib -name '*.so*' -exec strip --strip-debug {} + 2>/dev/null; \
    find /opt/llvm/lib -name '*.a'   -exec strip --strip-debug {} + 2>/dev/null; \
    find /opt/llvm/bin -type f       -exec strip --strip-unneeded {} + 2>/dev/null; \
    true

ARG ARCH=x86_64
ARG LLVM_ENABLE_ASSERTIONS
RUN ASSERTIONS_SUFFIX=""; \
    [ "${LLVM_ENABLE_ASSERTIONS}" = "ON" ] && ASSERTIONS_SUFFIX="-assertions"; \
    mkdir -p /out \
    && cd /opt \
    && zip -qr /out/llvm-${LLVM_VERSION}-linux-${ARCH}${ASSERTIONS_SUFFIX}.zip llvm/

# --- output stage ---------------------------------------------------------
# "docker build --output" copies from this stage to the host.
FROM scratch AS export
COPY --from=builder /out/*.zip /
