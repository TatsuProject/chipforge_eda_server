.PHONY: build up down logs clean test test-iss start health \
       build-verilator build-riscv-iss build-openlane build-gateway \
       restart-gateway restart-openlane restart-riscv-iss

# Build all services
build:
	docker compose build

# Start all services
up:
	docker compose up -d

# Stop all services
down:
	docker compose down

# View logs
logs:
	docker compose logs -f

# Clean up everything
clean:
	docker compose down -v
	docker system prune -f

# Test gateway with adder evaluator
test:
	python3 example_usage.py

# Test riscv-iss-api service (Spike + toolchain smoke test)
test-iss:
	docker compose up -d riscv-iss-api
	@sleep 3
	./tests/test_riscv_iss.sh

# Individual service builds
build-verilator:
	docker compose build verilator-api

build-riscv-iss:
	docker compose build riscv-iss-api

build-openlane:
	docker compose build openlane-api

build-gateway:
	docker compose build eda-gateway

# Fast development: restart individual services after changes
restart-gateway: build-gateway
	docker compose up -d eda-gateway
	@echo "Gateway restarted at: http://localhost:8080"

restart-riscv-iss: build-riscv-iss
	docker compose up -d riscv-iss-api
	@echo "RISC-V ISS API restarted at: http://localhost:8002"

restart-openlane: build-openlane
	docker compose up -d openlane-api
	@echo "OpenLane API restarted at: http://localhost:8003"

# Health check
health:
	@curl -s http://localhost:8080/health | python3 -m json.tool

# Quick start (build + run)
start: build up
	@echo "EDA Server running:"
	@echo "  Gateway:        http://localhost:8080"
	@echo "  Verilator API:  http://localhost:8001"
	@echo "  RISC-V ISS API: http://localhost:8002"
	@echo "  OpenLane API:   http://localhost:8003"
	@echo "  Swagger UI:     http://localhost:8080/docs"
