# ChipForge EDA Tools Server

A production-ready, containerized solution for evaluating hardware designs described in Verilog/SystemVerilog using Verilator and OpenLane. Built for ChipForge-SN84, it enables automated simulation and validation workflows. This guide helps validators and miners quickly set up, test, and operate the server from both the terminal and the GUI.

---

## Features
- Integrated EDA Tools: Verilator (simulation) and OpenLane (area, performance, and soon power evaluation)
- Evaluation Metrics: Functionality, area, performance, and (coming soon) power
- File Management: Direct upload, automatic parsing, ZIP archival

---

## Project Structure
```bash
chipforge_eda_server/
├── .env.example
├── Makefile
├── README.md
├── docker-compose.yml
├── example_usage.py
├── gateway/
│   ├── Dockerfile
│   ├── main.py
│   ├── requirements.txt
│   └── evaluator/
│       ├── evaluator.py
│       ├── evaluator.txt
│       └── evaluator.zip
├── test_designs/
│   ├── adder.zip
│   └── adder_evaluator.zip
├── verilator-api/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── openlane-api/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
```

---

## Quick Start for Setting up the EDA Server

1. **Prerequisites**: Docker, Docker Compose, Python 3.8+, 8GB+ RAM, 20GB+ disk.

2. **Clone & Setup**:
   ```fish
   git clone https://github.com/TatsuProject/chipforge_eda_server
   cd chipforge_eda_server
   cp .env.example .env
   # Edit .env if needed
   ```

3. **Build Docker Images**:
   ```fish
   make build
   # If this fails (sometimes due to internet speed), just run 'make build' again until it succeeds
   ```

4. **Run Test from Terminal**:
   ```fish
   make test
   # This will build, start all services, and run the validator script (example_usage.py)
   ```

---

## Step-by-Step Usage (Manual Control)
- Build Docker images:
  ```fish
  make build
  ```
- Start all services (if already built):
  ```fish
  make up
  ```
- Build and start all services together:
  ```fish
  make start
  ```
- Run the validator script (if services are already running):
  ```fish
  python3 example_usage.py
  ```
- Check health:
  ```fish
  make health
  ```
- View logs:
  ```fish
  make logs
  ```

---

## Command-line Usage
```sh
curl -X POST http://localhost:8080/evaluate \
     -F "design_zip=@design.zip" -F "evaluator_zip=@evaluator.zip" -F "submission_id=my_run"
# add  -H "X-API-Key: $EDA_API_KEY"  if the server sets EDA_API_KEY (see Security below)
```

---

## Makefile Commands
- `make build` — Build all Docker images
- `make up` — Start all services
- `make start` — Build and start all services together
- `make test` — Build, start, and run the validator script (all-in-one)
- `make health` — Check API health
- `make logs` — View logs
- `make down` — Stop all services
- `make clean` — Remove containers and prune system
- `make restart-gateway` — Restart only the gateway service
- `build-gateway` — Build only the gateway service
- `build-verilator` — Build only the verilator-api service

---

## API Usage
- **Main Evaluation Endpoint**: `POST /evaluate` on port 8080, with the design and evaluator ZIPs.
- The gateway is the only published port. verilator-api (8001) and openlane-api (8003) are reachable
  only on the internal Docker network. The interactive `/docs` page is disabled.

---

## Security

The code is open source; what needs protecting is a **running** server. `/evaluate` accepts an
evaluator ZIP from the caller and executes the `run.py` inside it, so anyone who can reach port 8080
can run code on that machine. Every operator (miner or validator) runs their own server and protects
their own.

- **Recommended:** do not expose port 8080 to the internet. Allow it only from the machine that
  sends evaluations (localhost, or an AWS security group limited to the validator).
- **If 8080 must be reachable from outside:** set `EDA_API_KEY` to a long random value of your own
  choosing (e.g. `openssl rand -hex 32`) in `.env`. Every request must then send it in the
  `X-API-Key` header. There is no shared or published key.
- If `EDA_API_KEY` is unset, the server works as before and logs a warning at startup. That is fine
  for a miner testing locally.

## Time limits

| setting | default | what it means |
|---|---|---|
| `EVAL_TIMEOUT_S` | 2700 (45 min) | Run-time limit per evaluation, counted after it leaves the queue. Exceeding it is `EVALUATION_TIMEOUT`, fault `miner`, not retryable. |
| `EDA_REQUEST_TIMEOUT_S` | 14400 (4 h) | Gateway ceiling on queue + run. Only a backstop for a long queue; exceeding it is fault `system`, retryable. |

Every failure is returned in one shape: `error{code, category, fault, retryable, stage, message}`.

---

## Python Client Example

```python
# example_usage.py (run with: make test)
import os
import requests
BASE_URL = os.getenv("EDA_BASE_URL", "http://localhost:8080")
design_zip = "test_designs/adder.zip"
evaluator_zip = "test_designs/adder_evaluator.zip"
with open(design_zip, "rb") as d, open(evaluator_zip, "rb") as e:
    files = {
        "design_zip": (os.path.basename(design_zip), d, "application/zip"),
        "evaluator_zip": (os.path.basename(evaluator_zip), e, "application/zip")
    }
    resp = requests.post(f"{BASE_URL}/evaluate", files=files)
    print(resp.json() if resp.ok else resp.text)
```

---

## Testing & Validation
- Run `make test` to verify simulation from the terminal.

---

## Monitoring & Troubleshooting
- Health: `/health`, `/metrics`, `/status`
- Logging: JSON, ELK stack, Sentry
- Use `make logs`, and check `.env` for issues.
- See docs for common errors and solutions.

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

**Ready to revolutionize hardware design evaluation!**
*Built with ❤️ for the hardware design community*