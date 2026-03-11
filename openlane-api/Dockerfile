# Pull ChipForge openlane image as base
# TODO: pin to a specific tag (e.g. logicx101/openlane:2024.xx) once you identify
#       the tag in use, to prevent cache-busting from upstream updates.
FROM logicx101/openlane:latest

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
