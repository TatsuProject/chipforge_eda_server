# All stages run sequentially with the legacy builder (build.sh); never use nproc.
# BASE is repeated here because FROM precedes COPY; build.sh checks it against pins.env.
ARG BASE=ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
ARG CERT_BASE=python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
FROM ${CERT_BASE} AS trust
FROM ${BASE} AS base
# Bootstrap HTTPS using only a CA bundle from a public digest-pinned image.
COPY --from=trust /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
LABEL io.chipforge.c1="v2-evaluator"
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY pins.env fetch.py /opt/cf/
# Apt verifies signed Release metadata and every package's SHA256. The dated archive fixes
# transitive packages too; never use trusted=yes or allow-unauthenticated.
RUN . /opt/cf/pins.env \
 && rm -f /etc/apt/sources.list.d/* \
 && printf 'deb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/%s noble main universe\ndeb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/%s noble-updates main universe\ndeb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/%s noble-security main universe\n' "$UBUNTU_SNAPSHOT" "$UBUNTU_SNAPSHOT" "$UBUNTU_SNAPSHOT" > /etc/apt/sources.list \
 && apt-get -o Acquire::Retries=5 -o APT::Update::Error-Mode=any update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake make git ca-certificates curl autoconf flex bison help2man perl \
      libfl-dev zlib1g-dev tcl-dev libreadline-dev libffi-dev pkg-config \
      libssl-dev libbz2-dev libsqlite3-dev liblzma-dev libncurses-dev uuid-dev \
      python3 strace="$STRACE_VERSION" systemd time="$TIME_VERSION" openssh-client xz-utils \
 && rm -rf /var/lib/apt/lists/*

FROM base AS python
RUN . /opt/cf/pins.env \
 && python3 /opt/cf/fetch.py "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" "$PYTHON_SHA256" /python.tar.xz \
 && mkdir /src && tar -xJf /python.tar.xz -C /src --strip-components=1 \
 && cd /src && ./configure --prefix=/usr/local --with-ensurepip=install --disable-test-modules \
 && make -j2 && make install DESTDIR=/out COMPILEALL_OPTS=-j2 \
 && ln -sf python3.13 /out/usr/local/bin/python3 \
 && ln -sf python3.13 /out/usr/local/bin/python

FROM base AS verilator
RUN . /opt/cf/pins.env \
 && retry() { n=0; until "$@"; do n=$((n+1)); [ "$n" -lt 5 ] || return 1; echo "retry $n: $*" >&2; sleep $((n*10)); done; } \
 && retry sh -c 'rm -rf /src && git clone https://github.com/verilator/verilator /src' \
 && cd /src && git checkout "$VERILATOR_COMMIT" \
 && test "$(git rev-parse HEAD)" = "$VERILATOR_COMMIT" \
 && autoconf && ./configure --prefix=/usr/local \
 && make -j2 && make install DESTDIR=/out \
 && mkdir -p /out/opt/cf/git \
 && git rev-parse HEAD > /out/opt/cf/git/verilator

FROM base AS yosys
RUN . /opt/cf/pins.env \
 && retry() { n=0; until "$@"; do n=$((n+1)); [ "$n" -lt 5 ] || return 1; echo "retry $n: $*" >&2; sleep $((n*10)); done; } \
 && retry sh -c 'rm -rf /src && git clone https://github.com/YosysHQ/yosys /src' \
 && cd /src && git checkout "$YOSYS_COMMIT" \
 && test "$(git rev-parse HEAD)" = "$YOSYS_COMMIT" \
 && retry git submodule update --init --recursive \
 && make -j2 CONFIG=gcc PREFIX=/usr/local \
 && make install CONFIG=gcc PREFIX=/usr/local DESTDIR=/out \
 && mkdir -p /out/opt/cf/git \
 && git rev-parse HEAD > /out/opt/cf/git/yosys \
 && git -C abc rev-parse HEAD > /out/opt/cf/git/yosys-abc \
 && git submodule status --recursive > /out/opt/cf/git/yosys-submodules

FROM base AS collateral
COPY nangate45.sha256 build_dram.py /opt/cf/
RUN . /opt/cf/pins.env \
 && retry() { n=0; until "$@"; do n=$((n+1)); [ "$n" -lt 5 ] || return 1; echo "retry $n: $*" >&2; sleep $((n*10)); done; } \
 && git init /orfs && cd /orfs \
 && git remote add origin https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts \
 && git sparse-checkout init --cone && git sparse-checkout set flow/platforms/nangate45 \
 && retry git fetch --depth 1 --filter=blob:none origin "$ORFS_COMMIT" && git checkout FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$ORFS_COMMIT" \
 && cd flow/platforms && sha256sum -c /opt/cf/nangate45.sha256 \
 && mkdir -p /out/opt/cf/pdk /out/opt/cf/git \
 && cp -a nangate45 /out/opt/cf/pdk/ \
 && printf '%s\n' "$ORFS_COMMIT" > /out/opt/cf/git/orfs
RUN . /opt/cf/pins.env \
 && retry() { n=0; until "$@"; do n=$((n+1)); [ "$n" -lt 5 ] || return 1; echo "retry $n: $*" >&2; sleep $((n*10)); done; } \
 && retry sh -c 'rm -rf /cacti && git clone https://github.com/HewlettPackard/cacti /cacti' && cd /cacti \
 && git checkout "$CACTI_COMMIT" && test "$(git rev-parse HEAD)" = "$CACTI_COMMIT" \
 && make -j2 && install -D cacti /out/opt/cf/cacti/cacti \
 && cp -a tech_params cache.cfg /out/opt/cf/cacti/ \
 && printf '%s\n' "$CACTI_COMMIT" > /out/opt/cf/git/cacti
RUN CF_DRAM_ROOT=/opt/cf/platform python3 /opt/cf/build_dram.py \
 && mkdir -p /out/opt/cf/platform \
 && cp -a /opt/cf/platform/DRAMsim3-* /opt/cf/platform/backend-pin.json /out/opt/cf/platform/ \
 && rm -f /out/opt/cf/platform/*.tar.gz

FROM base AS venvs
COPY --from=python /out/ /
COPY locks/ /opt/cf/locks/
RUN for c in orchestrator numerics workloads physical divisions reference-sw; do \
      python3.13 -m venv /opt/cf/venv/$c \
      && /opt/cf/venv/$c/bin/pip install --no-cache-dir --only-binary=:all: --no-deps --require-hashes -r /opt/cf/locks/$c.lock || exit 1; \
    done

FROM base
COPY --from=python /out/ /
RUN . /opt/cf/pins.env \
 && python3 /opt/cf/fetch.py "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" "$DOCKER_CLI_SHA256" /docker.tgz \
 && tar -xzf /docker.tgz -C /usr/local/bin --strip-components=1 docker/docker && rm /docker.tgz
COPY --from=verilator /out/ /
COPY --from=yosys /out/ /
COPY --from=collateral /out/ /
COPY --from=venvs /opt/cf/venv/ /opt/cf/venv/
COPY locks/ /opt/cf/locks/
COPY nangate45.sha256 VERSIONS.sh entrypoint.sh smoke.sh /opt/cf/
COPY loader/ /opt/cf/loader/
# Deliberate systemd-run --scope replacement: no systemd daemon runs in this image.
COPY cfscope /usr/local/bin/systemd-run
RUN /opt/cf/VERSIONS.sh > /opt/cf/VERSIONS.txt && cat /opt/cf/VERSIONS.txt
ENTRYPOINT ["/opt/cf/entrypoint.sh"]
