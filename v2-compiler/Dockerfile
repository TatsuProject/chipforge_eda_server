# cf-compiler: the one image every npu-v2 miner compiler is built and run in (CONTRACT.md 3.5).
#
#   build:   build.sh            (standalone context; network needed only at build)
#   use:     CF_COMPILER_IMAGE=cf-compiler:v2.1-public   (the box never pulls, never has network)
#
# Every input is pinned by digest or hash: the base image by manifest digest, Debian packages by
# exact version from a dated snapshot.debian.org archive, Python wheels by sha256 (requirements*.lock,
# --require-hashes), the Rust and xPack toolchains by the sha256 their publishers sign. Tool versions and target GCC hashes form the content pin; image IDs need not reproduce.
ARG BASE=python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

# ----------------------------------------------------------------------------- fetch (not shipped)
FROM ${BASE} AS fetch
ARG RUST_VERSION=1.99.0
ARG RUST_DATE=2026-10-01
ARG RUST_SHA256=de0581ca9d732295a6474cfbd02461db27d69acd5050a8206523a8d6fa1599db
ARG XPACK_VERSION=14.2.0-3
ARG XPACK_SHA256=f574415b63f12b09bdd3475223ab492a465d23810646c90c13a4c3b676c83503
COPY fetch.py /fetch.py
# Rust: the standalone installer, no rustup (no proxies, no self-update, nothing that writes HOME).
RUN python3 /fetch.py https://static.rust-lang.org/dist/${RUST_DATE}/rust-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.gz \
        ${RUST_SHA256} /rust.tgz \
 && mkdir /rust-src && tar -xzf /rust.tgz -C /rust-src --strip-components=1 && rm /rust.tgz \
 && /rust-src/install.sh --prefix=/opt/rust --disable-ldconfig \
        --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu \
 && rm -rf /rust-src /opt/rust/share/doc /opt/rust/share/man /opt/rust/lib/rustlib/*.log
# xPack riscv-none-elf-gcc: byte-identical to the evaluator's target toolchain (the eda-server's
# /opt/riscv; verified by build.sh). Only the default and rv32imc/ilp32 multilibs are kept -- the
# evaluator's flags (-march=rv32imc_zicsr -mabi=ilp32) select rv32imc/ilp32 -- and gdb is dropped.
RUN python3 /fetch.py https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v${XPACK_VERSION}/xpack-riscv-none-elf-gcc-${XPACK_VERSION}-linux-x64.tar.gz \
        ${XPACK_SHA256} /xpack.tgz \
 && mkdir -p /opt/riscv && tar -xzf /xpack.tgz -C /opt/riscv --strip-components=1 && rm /xpack.tgz \
 && for d in /opt/riscv/riscv-none-elf/lib /opt/riscv/lib/gcc/riscv-none-elf/*; do \
        for m in "$d"/rv32* "$d"/rv64*; do [ "$(basename "$m")" = rv32imc ] || rm -rf "$m"; done; \
        [ -d "$d/rv32imc" ] && for a in "$d"/rv32imc/*; do [ "$(basename "$a")" = ilp32 ] || rm -rf "$a"; done; \
    done \
 && rm -rf /opt/riscv/bin/riscv-none-elf-gdb* /opt/riscv/share/doc /opt/riscv/share/info /opt/riscv/share/man \
           /opt/riscv/share/gcc-*/python /opt/riscv/riscv-none-elf/share \
 && /opt/riscv/bin/riscv-none-elf-gcc -march=rv32imc_zicsr -mabi=ilp32 -print-multi-directory | grep -qx rv32imc/ilp32

# ----------------------------------------------------------------------------- the image
FROM ${BASE}
ARG DEBIAN_SNAPSHOT=20261003T000000Z
ARG LLVM=22
ARG LLVM_DEB=1:22.1.8-1~deb13u4
# Debian from a dated snapshot, so the same Dockerfile resolves the same .debs next year.
RUN rm -f /etc/apt/sources.list.d/* \
 && printf '%s\n' \
    "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} trixie main" \
    "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} trixie-security main" \
    > /etc/apt/sources.list \
 && apt-get -o Acquire::Retries=5 update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      gcc-14=14.2.0-19 g++-14=14.2.0-19 gcc g++ make cmake ninja-build pkg-config \
      clang-${LLVM}=${LLVM_DEB} lld-${LLVM}=${LLVM_DEB} llvm-${LLVM}=${LLVM_DEB} llvm-${LLVM}-dev=${LLVM_DEB} \
      mlir-${LLVM}-tools=${LLVM_DEB} libmlir-${LLVM}-dev=${LLVM_DEB} libclang-${LLVM}-dev=${LLVM_DEB} \
      zlib1g-dev libzstd-dev libffi-dev libxml2-dev libz3-dev libncurses-dev \
      xz-utils zstd file patch \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/info \
 && for t in clang clang++ lld ld.lld llvm-ar llvm-objdump llvm-config FileCheck opt llc \
             mlir-opt mlir-translate mlir-tblgen mlir-runner mlir-lsp-server mlir-pdll mlir-reduce tblgen-lsp-server; do \
        [ -e /usr/lib/llvm-${LLVM}/bin/$t ] && ln -sf /usr/lib/llvm-${LLVM}/bin/$t /usr/local/bin/$t; done; true

COPY --from=fetch /opt/rust /opt/rust
COPY --from=fetch /opt/riscv /opt/riscv

# Python: one environment for the evaluator's model stack + IREE + TVM (+ build helpers), and a
# separate venv for tosa-tools, whose numpy/flatbuffers pins conflict with the evaluator's.
COPY requirements.lock requirements-tosa.lock /opt/cf/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes --only-binary=:all: \
        -r /opt/cf/requirements.lock \
 && python3 -m venv /opt/tosa \
 && /opt/tosa/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/cf/requirements-tosa.lock \
 && for t in tosa_reference_model tosa_verify tosa-opt tosa_json2fbbin tosa_json2numpy tosa_schemavalidation; do \
        ln -sf /opt/tosa/bin/$t /usr/local/bin/$t; done \
 && rm -rf /root/.cache

# Small, pinned additions for Vela and the stable IREE compiler C API. The library is already
# provided by the wheel; do not duplicate LLVM or build IREE in the image. Plus the ONNX frontends'
# one missing dependency (innovation audit #8): the scored input is model.onnx, and TVM Relax
# `from_onnx` and IREE `iree-import-onnx` both import `onnx`. onnx 1.18.0 / onnxruntime 1.22.1 are the
# versions the evaluator's own workload stack uses (pipeline/workloads/t1/requirements.lock), so a
# compiler that checks itself against ORT sees the evaluator's numerics. coloredlogs/humanfriendly
# are onnxruntime's only dependencies not already in requirements.lock (resolved with
# `pip install --dry-run --report` against this image's frozen set).
COPY requirements-extra.lock /opt/cf/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes --only-binary=:all: \
        -r /opt/cf/requirements-extra.lock
COPY iree-dev/ /opt/iree/include/iree/compiler/
RUN mkdir -p /opt/iree/lib /opt/cf/target/include /opt/cf/target/src \
 && ln -s /usr/local/lib/python3.12/site-packages/iree/compiler/_mlir_libs/libIREECompiler.so /opt/iree/lib/libIREECompiler.so

COPY versions.sh /opt/cf/versions.sh

ENV PATH=/opt/rust/bin:/opt/riscv/bin:/usr/lib/llvm-22/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    CF_TARGET_INCLUDE=/opt/cf/target/include \
    CF_TARGET_CC=/opt/riscv/bin/riscv-none-elf-gcc \
    MLIR_DIR=/usr/lib/llvm-22/lib/cmake/mlir \
    LLVM_DIR=/usr/lib/llvm-22/lib/cmake/llvm \
    CARGO_NET_OFFLINE=true \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# The box runs read-only as uid 65534. Fail the image build if anything a box relies on is missing
# or anything here would need the network or a writable rootfs at evaluation time.
RUN /opt/cf/versions.sh > /opt/cf/VERSIONS.txt && cat /opt/cf/VERSIONS.txt

COPY expected-versions.txt target-gcc.sha256 /opt/cf/
RUN diff -u /opt/cf/expected-versions.txt /opt/cf/VERSIONS.txt \
 && sha256sum -c /opt/cf/target-gcc.sha256 \
 && cd /opt/cf && sha256sum VERSIONS.txt target-gcc.sha256 > CONTENT.sha256
