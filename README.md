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

## GUI Usage
- After running `make start`, open your browser and go to [http://localhost:8080/docs](http://localhost:8080/docs)
- In the GUI, for `/evaluate`, click "Browse..." and select both your design ZIP (`test_designs/adder.zip`) and evaluator ZIP (`test_designs/adder_evaluator.zip`)
- Click "Execute" to run the evaluation and see results below

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
- **Gateway Docs**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **Main Evaluation Endpoint**:  `POST /evaluate` with ZIP files
- **Verilator API**: Accessible at [http://localhost:8001](http://localhost:8001)
- **OpenLane API**: Accessible at [http://localhost:8003](http://localhost:8003)

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
- Use the GUI ([http://localhost:8080/docs](http://localhost:8080/docs)) for interactive testing.

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