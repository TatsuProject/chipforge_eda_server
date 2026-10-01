# Pull ChipForge openlane image as base
# Pinned by digest (2026-09-26): the tools inside decide every area and fmax, and an unpinned tag
# moved between July and September (the same core read 72.1 MHz then, 79.05 MHz now). Everyone
# who evaluates must run this exact image.
FROM logicx101/openlane@sha256:b5433bed02733611b385e0dc217512d2b459c83027c7b82178ffb2979b0e7d05

# Install Python dependencies for FastAPI service
RUN pip install --no-cache-dir \
    fastapi==0.68.0 \
    uvicorn==0.15.0 \
    pydantic==1.8.2 \
    python-multipart==0.0.5 \
    requests \
    volare \
    aiofiles

# Install PDK (Sky130) at build time and create alias "sky130A"
RUN volare enable --pdk sky130 --pdk-root /openlane/pdks && \
    ln -sf /openlane/pdks/volare/sky130/versions/*/sky130A /openlane/pdks/sky130A

# Set ENV for all processes (FastAPI + subprocess + flow.tcl)
ENV PDK_ROOT=/openlane/pdks \
    PDK=sky130A \
    STD_CELL_LIBRARY=sky130_fd_sc_hd \
    PDKPATH=/openlane/pdks/sky130A

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
