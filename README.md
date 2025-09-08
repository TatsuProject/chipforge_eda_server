# ChipForge EDA Tools Server

A production-ready, containerized solution for evaluating hardware designs described in Verilog/SystemVerilog using Verilator. Built for ChipForge-SN84, it enables automated simulation and validation workflows. This guide helps validators and miners quickly set up, test, and operate the server from both the terminal and the GUI.

---

## Features
- Integrated EDA Tool: Verilator (simulation), accessible via a unified API gateway.
- Evaluation Metrics: Functionality and simulation performance scored and reported.
- Evaluation Matrix (Coming Soon): Extended analysis for area, power, and advanced performance metrics.
- Security: API key authentication and input validation.
- File Management: Direct upload, automatic parsing, ZIP archival.

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
│   └── adder.zip
├── verilator-api/
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
   # Edit .env and set EDA_API_KEY (required for authentication)
   ```
   > ⚠️ **Note:** The `.env` file is required for security. Without a valid `EDA_API_KEY`, the gateway will reject requests.

---

## Terminal Usage

- **One-step test:**
  ```fish
  make test
  # This will build the Docker images, start all services, and run the validator script (example_usage.py)
  ```
  This is the recommended way for quick validation from the terminal.

- **Step-by-step control:**
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
- You will see a GUI similar to the first image above.
- Click the green "Authorize" button (top right). Enter your API key (same as in `.env`) in the popup and click "Authorize".
- For `/evaluate`, click "Browse..." and select your design ZIP (e.g., `test_designs/adder.zip` as shown in the third image).
- Click "Execute" to run the evaluation and see results below.

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
- **Authentication**:  `x-api-key: <your-eda-api-key>` (set in `.env`)
- **Gateway Docs**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **Main Evaluation Endpoint**:  `POST /evaluate` with ZIP file
- **Verilator API**: Accessible at [http://localhost:8001](http://localhost:8001)

---

## Python Client Example

```python
# example_usage.py (run with: make test)
import os
import requests
API_KEY = os.getenv("EDA_API_KEY", "test-key")
BASE_URL = os.getenv("EDA_BASE_URL", "http://localhost:8080")
zip_path = "test_designs/adder.zip"
with open(zip_path, "rb") as f:
    files = {"file": (os.path.basename(zip_path), f, "application/zip")}
    headers = {"x-api-key": API_KEY}
    resp = requests.post(f"{BASE_URL}/validate", files=files, headers=headers)
    print(resp.json() if resp.ok else resp.text)
```

---

## Testing & Validation
- Run `make test` to verify simulation from the terminal.
- Use the GUI ([http://localhost:8080/docs](http://localhost:8080/docs)) for interactive testing.

---

## Monitoring & Security
- Health: `/health`, `/metrics`, `/status`
- Logging: JSON, ELK stack, Sentry
- Security: API keys, RBAC, TLS, input validation

---

## Troubleshooting
- Use `make logs`, and check `.env` for issues.
- See docs for common errors and solutions.

---

## 🙏 Acknowledgments
- Verilator and all contributors.

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