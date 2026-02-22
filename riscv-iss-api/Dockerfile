FROM python:3.11-slim

# ---------------------------------------------------------------------------
# System dependencies (for Spike build + runtime)
# No Verilator needed — Coral binary arrives via evaluator bundle at runtime.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    make \
    autoconf \
    g++ \
    wget \
    device-tree-compiler \
    libboost-regex-dev \
    libboost-system-dev \
    libstdc++6 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Install RISC-V toolchain (pre-built from xPack)
# Only need gcc + objcopy to compile .S -> ELF -> mem
# ---------------------------------------------------------------------------
ARG RISCV_TOOLCHAIN_URL="https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v14.2.0-3/xpack-riscv-none-elf-gcc-14.2.0-3-linux-x64.tar.gz"
RUN mkdir -p /opt/riscv \
    && cd /tmp \
    && wget -q "${RISCV_TOOLCHAIN_URL}" -O riscv-toolchain.tar.gz \
    && tar xzf riscv-toolchain.tar.gz -C /opt/riscv --strip-components=1 \
    && rm riscv-toolchain.tar.gz \
    && ln -sf /opt/riscv/bin/riscv-none-elf-gcc /usr/local/bin/riscv64-unknown-elf-gcc \
    && ln -sf /opt/riscv/bin/riscv-none-elf-objcopy /usr/local/bin/riscv64-unknown-elf-objcopy \
    && ln -sf /opt/riscv/bin/riscv-none-elf-objdump /usr/local/bin/riscv64-unknown-elf-objdump

# ---------------------------------------------------------------------------
# Build Spike ISA simulator from source
# Golden reference — must be built inside Docker to be tamper-proof.
# ---------------------------------------------------------------------------
WORKDIR /tmp
RUN git clone https://github.com/riscv-software-src/riscv-isa-sim.git spike \
    && cd spike \
    && mkdir build && cd build \
    && ../configure --prefix=/opt/riscv \
    && make -j"$(nproc)" \
    && make install \
    && cd / && rm -rf /tmp/spike

ENV PATH="/opt/riscv/bin:${PATH}"

# ---------------------------------------------------------------------------
# Environment variables for the evaluator scripts (run.py reads these)
# ---------------------------------------------------------------------------
ENV RISCV_GCC="/usr/local/bin/riscv64-unknown-elf-gcc"
ENV RISCV_OBJCOPY="/usr/local/bin/riscv64-unknown-elf-objcopy"
ENV SPIKE_BIN="/opt/riscv/bin/spike"
ENV CORAL_BIN="/opt/coralnpu/bin/Vtb_top"

# ---------------------------------------------------------------------------
# Install Python dependencies + application code
# ---------------------------------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
