FROM python:3.11-slim

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
RUN git clone https://github.com/verilator/verilator \
    && cd verilator \
    && git checkout stable \
    && autoconf \
    && ./configure \
    && make -j"$(nproc)" \
    && make install \
    && cd / && rm -rf /tmp/verilator

# ----------------------------
# Install Python dependencies
# ----------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ----------------------------
# Copy application code
# ----------------------------
COPY . /app

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
