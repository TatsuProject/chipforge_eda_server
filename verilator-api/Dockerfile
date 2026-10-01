FROM python:3.11.9-slim

# Install system dependencies (for Verilator build + runtime)
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    perl \
    make \
    autoconf \
    g++ \
    flex \
    bison \
    ccache \
    libgoogle-perftools-dev \
    numactl \
    perl-doc \
    help2man \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------
# Install Verilator from source
# ----------------------------
WORKDIR /tmp
# Pinned to the exact revision EXPECTED.json was measured on. `stable` moves: the PC built 5.052 while
# this box ran 5.046, so two servers built a week apart were not running the same simulator.
ARG VERILATOR_REV=1264184fbbf44bbab9f41f9b83fbc5f04e291dc5
RUN git clone https://github.com/verilator/verilator \
    && cd verilator \
    && git checkout "$VERILATOR_REV" \
    && autoconf \
    && ./configure \
    && make -j"$(nproc)" \
    && make install \
    && cd / && rm -rf /tmp/verilator

# ----------------------------------------------------------------------------
# RISC-V toolchain (xPack riscv-none-elf-gcc) — Challenge-15 compiles the miner's
# kernels ON the server, inside run.py. Symlinked to riscv64-unknown-elf-* (what
# compile.sh expects). Same recipe as feature/riscv-iss-api. Portable (no glibc skew).
# Placed AFTER the Verilator build so that slow layer stays cached on rebuild.
# ----------------------------------------------------------------------------
ARG RISCV_TOOLCHAIN_URL="https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v14.2.0-3/xpack-riscv-none-elf-gcc-14.2.0-3-linux-x64.tar.gz"
RUN apt-get update && apt-get install -y wget && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/riscv && cd /tmp \
    && wget -q "${RISCV_TOOLCHAIN_URL}" -O riscv-toolchain.tar.gz \
    && tar xzf riscv-toolchain.tar.gz -C /opt/riscv --strip-components=1 \
    && rm riscv-toolchain.tar.gz \
    && ln -sf /opt/riscv/bin/riscv-none-elf-gcc     /usr/local/bin/riscv64-unknown-elf-gcc \
    && ln -sf /opt/riscv/bin/riscv-none-elf-objcopy /usr/local/bin/riscv64-unknown-elf-objcopy \
    && ln -sf /opt/riscv/bin/riscv-none-elf-size    /usr/local/bin/riscv64-unknown-elf-size
ENV PATH="/opt/riscv/bin:${PATH}"

# ----------------------------
# Install Python dependencies
# ----------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ----------------------------
# Copy application code
# ----------------------------
COPY . .

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
