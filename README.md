# ChipForge EDA Tools Server

A production-ready, containerized solution for evaluating hardware designs described in Verilog/SystemVerilog using Verilator and OpenLane. Built for ChipForge, it enables automated simulation and validation workflows. This guide helps validators and miners quickly set up, test, and operate the server from the terminal.

---

## Features
- **Simulation** with Verilator: functionality score from the challenge's testbench
- **Synthesis** with OpenLane (sky130): area and performance
- One endpoint, `POST /evaluate`, that runs both and returns the scores and the pass/fail gates
- Sizes itself to the machine: parallel simulation and synthesis lanes are derived from the CPU cores
  and memory (override in `.env`)

Validators run this server next to their validator; miners can run it to test designs before submitting.

---

## Project Structure
```
chipforge_eda_server/
├── .env.example          # every setting, with defaults (copy to .env)
├── docker-compose.yml    # the three services
├── Makefile
├── gateway/              # POST /evaluate on port 8080: unpacks, calls the two services, scores
├── verilator-api/        # simulation service (internal port 8001)
├── openlane-api/         # synthesis service (internal port 8003)
├── capacity.py           # how lanes are sized from the machine
├── example_usage.py      # sends test/*.zip to the gateway (make test)
├── test/                 # adder.zip + adder_evaluator.zip: a design and its evaluator bundle
├── shared/, results/     # mounted into the containers (gitignored)
```

---

## Quick Start

1. **Prerequisites**: Linux with Docker and the Docker Compose plugin. 16 GB+ RAM recommended (each
   synthesis run reserves about 6 GB), 25 GB+ free disk (the images are about 9 GB), and Python 3 with
   `requests` for the test script.

2. **Clone and configure**:
   ```bash
   git clone https://github.com/TatsuProject/chipforge_eda_server
   cd chipforge_eda_server
   cp .env.example .env      # optional: the defaults work; see "Configuration"
   ```

3. **Build and start**:
   ```bash
   make start                # build the images and start the services
   # A failed build is usually a download timeout: run it again.
   ```

4. **Check it works**:
   ```bash
   make health               # {"status": "ok"}
   pip install requests
   make test                 # evaluates test/adder.zip against test/adder_evaluator.zip
   ```

5. **Validators:** set `EDA_SERVER_URL=http://localhost:8080` in the validator's `.env` (the default)
   and start the validator after this server is up.

**Updating:** `git pull`, compare your `.env` with `.env.example` for new settings, then `make start`.

---

## Configuration

All settings are in [`.env.example`](.env.example) and are optional; docker compose reads `.env` from
this folder. Apply a change with `docker compose up -d`.

| setting | default | what it does |
|---|---|---|
| `EDA_BIND_ADDRESS` | `127.0.0.1` | interface port 8080 is published on (see Security) |
| `EVAL_TIMEOUT_S`, `EDA_REQUEST_TIMEOUT_S` | 2700, 14400 | see "Time limits" |
| `OPENLANE_LANES`, `VERILATOR_EVAL_LANES`, ... | automatic | parallel synthesis/simulation; the startup logs print the plan |
| `C15_MAX_SUBMISSION_MB`, `C15_MAX_UNCOMPRESSED_MB` | 300, 4096 | upload and unpacked-size limits |
| `EDA_MOCK_FILE` | empty | testing only, see "Mock mode" |

---

## Makefile Commands
- `make start` — build and start all services
- `make build` / `make up` / `make down` — build, start, stop
- `make logs` — follow the logs of all services
- `make health` — check the gateway
- `make test` — run `example_usage.py` against the running services
- `make clean` — stop, remove volumes and prune Docker
- `make restart-gateway` / `make restart-openlane` — rebuild and restart one service

---

## API Usage
- **Main Evaluation Endpoint**: `POST /evaluate` on port 8080, with the design and evaluator ZIPs.
  ```bash
  curl -X POST http://localhost:8080/evaluate \
       -F "design_zip=@design.zip" -F "evaluator_zip=@evaluator.zip" -F "submission_id=my_run"
  ```
- `GET /health` answers without running anything.
- The gateway is the only published port. verilator-api (8001) and openlane-api (8003) are reachable
  only on the internal Docker network. The interactive `/docs` page is disabled.

## Security

The code is open source; what needs protecting is a **running** server. `/evaluate` accepts an
evaluator ZIP from the caller and executes the `run.py` inside it, so anyone who can reach port 8080
can run code on that machine. Every operator (miner or validator) runs their own server and protects
their own.

- **There is no API key.** Access control is network-level: **never expose port 8080 to the
  internet.** Allow it only from the machine(s) that send evaluations: `localhost`, or an AWS
  security group / firewall rule limited to your validator's IP.
- **By default the port is bound to `127.0.0.1`** (`EDA_BIND_ADDRESS` in `.env`), so only the same
  machine can reach it. If the validator runs elsewhere, set `EDA_BIND_ADDRESS=0.0.0.0` **and** allow
  port 8080 only from the validator's IP.

## Time limits

| setting | default | what it means |
|---|---|---|
| `EVAL_TIMEOUT_S` | 2700 (45 min) | Run-time limit per evaluation, counted after it leaves the queue. Exceeding it is `EVALUATION_TIMEOUT`, fault `miner`, not retryable. |
| `EDA_REQUEST_TIMEOUT_S` | 14400 (4 h) | Gateway ceiling on queue + run. Only a backstop for a long queue; exceeding it is fault `system`, retryable. |

Every failure is returned in one shape: `error{code, category, fault, retryable, stage, message}`.

---

## Python Client Example

```python
import requests
with open("test/adder.zip", "rb") as d, open("test/adder_evaluator.zip", "rb") as e:
    resp = requests.post("http://localhost:8080/evaluate",
                         files={"design_zip": d, "evaluator_zip": e},
                         data={"submission_id": "my_run"})
print(resp.json())
```

`example_usage.py` does the same for the ZIPs in `test/` (`EDA_BASE_URL` and `EDA_TEST_DIR` override
the URL and folder).

---

## Testing & Validation
- `make test` evaluates the example design in `test/` end to end.

### Mock mode (testing only)

To test the validator/challenge-server flow without waiting for real EDA runs, the gateway can
return a fixed result instead of running Verilator and OpenLane.

1. Create `shared/eda_mock.json` (the `shared/` folder is mounted into the gateway and gitignored);
   start from `gateway/mock_result.example.json`:
   ```json
   {
     "delay_seconds": 10,
     "overall": 50.0,
     "func_score": 100.0,
     "area_score": 40.0,
     "perf_score": 60.0,
     "power_score": 0.0,
     "functional_gate": true,
     "overall_gate": true
   }
   ```
2. Restart the gateway with mock mode on:
   ```bash
   EDA_MOCK_FILE=/shared/eda_mock.json docker compose up -d --build eda-gateway
   ```
   (or set `EDA_MOCK_FILE=/shared/eda_mock.json` in `.env` and run `docker compose up -d`)
3. Edit the file at any time: it is re-read on every request, no restart needed.

| field | effect |
|---|---|
| `delay_seconds` | how long `/evaluate` waits before answering (default 10) |
| `overall`, `func_score`, `area_score`, `perf_score`, `power_score` | the scores returned; a number, or `[low, high]` for a random value per request |
| `functional_gate`, `overall_gate` | gate flags; `overall_gate: false` returns `REJECTED` (fault `miner`) |
| `result` | `"ERROR"` returns a retryable system error instead of a score |

Mocked responses carry `"mock": true` and the gateway logs `[MOCK] … no EDA tools ran` for each one.
To turn it off, empty `EDA_MOCK_FILE` (in `.env` or the shell) and run `docker compose up -d --build eda-gateway`.
**Never set `EDA_MOCK_FILE` on a production EDA server.**

---

## Monitoring & Troubleshooting
- `make health` (or `curl http://localhost:8080/health`) must return `{"status": "ok"}`.
- `make logs` shows all three services; at startup each prints its capacity plan (lanes, cores, memory).
- `docker compose ps` shows whether the services are up and healthy.
- Every failed evaluation carries `error.code`, `error.stage` and `error.fault` (`miner` or `system`)
  in the response; the validator logs them.
- A validator that cannot connect: check `EDA_SERVER_URL` in its `.env`, and that it runs on the same
  machine (or that `EDA_BIND_ADDRESS` and the firewall allow it).

---

## 🙏 Acknowledgments
- Verilator, OpenLane, and all contributors.

---

## License
MIT License—see `LICENSE`.

---

## Support
- [Discord](https://discord.com/channels/799672011265015819/1408463235082092564)
- Email: contact@tatsuecosystem.io

---
