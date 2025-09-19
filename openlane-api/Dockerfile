# Pull ChipForge openlane image as base
FROM logicx101/openlane:latest

# RUN apt-get update && apt-get install -y build-essential \
#     && rm -rf /var/lib/apt/lists/*


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

# Set working directory
WORKDIR /app

# Copy application code into container
COPY . /app

# Expose API port
EXPOSE 8003

# Default command: run FastAPI app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
