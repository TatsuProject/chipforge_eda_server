# ChipForge EDA Server

Automated evaluation server for ChipForge SN84 challenges. Miners submit designs, validators score them. Three backend services handle different evaluation types — the gateway routes to the right ones based on each challenge's configuration.

## Architecture

```
                         POST /evaluate
                     design.zip + evaluator.zip
                              |
                     [ eda-gateway :8080 ]
                              |
                   reads config.json from
                   evaluator.zip to decide
                   which backends to call
                              |
            +-----------------+-----------------+
            |                 |                 |
   [ verilator-api ]   [ riscv-iss-api ]   [ openlane-api ]
       :8001               :8002               :8003
            |                 |                 |
     RTL simulation    Spike ISA sim +     Synthesis with
     with Verilator    RISC-V toolchain    OpenLane (area,
     (functionality,   (bug bounty         fmax, power)
      IPC)              evaluation)
```

**What each service provides:**

| Service | Built-in Tools | What it evaluates |
|---------|---------------|-------------------|
| `verilator-api` | Verilator | RTL simulation — functionality score, IPC |
| `riscv-iss-api` | Spike ISA simulator, RISC-V GCC | Assembly-level bug bounty — compiles .S, runs Spike + Coral, compares traces |
| `openlane-api` | OpenLane, Sky130 PDK | Physical design — area, fmax, power |

## How It Works

Every challenge ships an **evaluator.zip** that tells the gateway what to do. The miner ships a **design.zip** with their submission. The gateway reads the evaluator's config, fans out to the right backends, collects results, and returns a weighted score.

**evaluator.zip layout:**
```
evaluator.zip
├── gateway/
│   └── config.json        # weights, targets, services list
├── verilator/
│   └── run.py + tests     # only if challenge uses verilator
├── riscv-iss/
│   └── run.py + scripts   # only if challenge uses riscv-iss
└── openlane/
    └── run.py             # only if challenge uses openlane
```

**config.json** (formerly weights.json — both names are supported):
```json
{
  "weights": {
    "functionality": 0.5,
    "area": 0.25,
    "performance": 0.25,
    "power": 0.0
  },
  "targets": {
    "func_threshold": 0.8,
    "area_target_um2": 200000.0,
    "perf_target_ips": 100000000.0
  },
  "services": ["verilator", "openlane"]
}
```

The `services` list controls which backends are called. If omitted, the gateway infers it from the weights (backward-compatible with older challenges).

**design.zip** contents depend on the challenge type:
- RTL challenges: Verilog/SystemVerilog files + `rtl.f`
- Bug bounty challenges: a single `.S` assembly file

## Setup

**Requirements:** Docker, Docker Compose, 8GB+ RAM, 20GB+ disk.

```bash
git clone https://github.com/TatsuProject/chipforge_eda_server
cd chipforge_eda_server
```

Build and start:
```bash
make build    # build all Docker images (first time takes ~20 min)
make up       # start all services in background
```

Verify:
```bash
make health   # should return {"status": "healthy"}
```

## Usage

### From the Terminal

The server exposes a single endpoint: `POST /evaluate` on port 8080.

```bash
curl -X POST http://localhost:8080/evaluate \
  -F "design_zip=@design.zip" \
  -F "evaluator_zip=@evaluator.zip"
```

Or use the included test script:
```bash
make test     # runs example_usage.py against test/adder.zip
```

### From the Browser

Open http://localhost:8080/docs after starting the server. The Swagger UI lets you upload files and execute requests interactively.

### Python Client

```python
import requests

with open("design.zip", "rb") as d, open("evaluator.zip", "rb") as e:
    resp = requests.post(
        "http://localhost:8080/evaluate",
        files={
            "design_zip": ("design.zip", d, "application/zip"),
            "evaluator_zip": ("evaluator.zip", e, "application/zip"),
        },
    )
    print(resp.json())
```

### Response Format

```json
{
  "success": true,
  "submission_id": "a1b2c3...",
  "verilator_results": { "success": true, "results": { "functionality_score": 0.95 } },
  "openlane_results": { "success": true, "results": { "area_um2": 150000 } },
  "riscv_iss_results": { "success": true, "results": { "bug_found": true } },
  "weights": { ... },
  "targets": { ... },
  "final_score": {
    "func_score": 95.0,
    "area_score": 80.0,
    "overall": 87.5,
    "functional_gate": true,
    "overall_gate": true
  }
}
```

Only the services that were called appear in the response. Unused services show `{"skipped": true}`.

## Challenge Types

### RTL Challenges (e.g., adder, rv32i)

Miners submit Verilog designs. The evaluator runs them through Verilator for functional correctness, and optionally through OpenLane for area/performance.

```json
{ "services": ["verilator", "openlane"] }
```

### Bug Bounty Challenges (e.g., Challenge 0011 — CoralNPU)

Miners submit `.S` assembly files that expose bugs in a target CPU. The evaluator compiles the assembly, runs it on both a reference simulator (Spike) and the target (Coral RTL), and compares execution traces.

```json
{ "services": ["riscv-iss"] }
```

The Coral binary (`Vtb_top`) can be baked into the Docker image or shipped inside the evaluator.zip under `riscv-iss/bin/Vtb_top`.

## Smoke Tests

```bash
# Start the riscv-iss-api service
docker-compose up -d riscv-iss-api

# Run tests (checks health, Spike, GCC, and end-to-end evaluation)
./tests/test_riscv_iss.sh
```

## Makefile Reference

| Command | Description |
|---------|-------------|
| `make build` | Build all Docker images |
| `make up` | Start all services |
| `make down` | Stop all services |
| `make start` | Build + start |
| `make test` | Run `example_usage.py` against the gateway |
| `make health` | Check gateway health |
| `make logs` | Tail service logs |
| `make clean` | Remove containers and prune |
| `make restart-gateway` | Rebuild and restart gateway only |
| `make restart-openlane` | Rebuild and restart openlane-api only |

## Service Ports

| Service | Port | Docs |
|---------|------|------|
| Gateway | http://localhost:8080 | http://localhost:8080/docs |
| Verilator API | http://localhost:8001 | http://localhost:8001/docs |
| RISC-V ISS API | http://localhost:8002 | http://localhost:8002/docs |
| OpenLane API | http://localhost:8003 | http://localhost:8003/docs |

## Support

- Discord: https://discord.com/channels/799672011265015819/1408463235082092564
- Email: contact@tatsuecosystem.io

## License

MIT — see `LICENSE`.
