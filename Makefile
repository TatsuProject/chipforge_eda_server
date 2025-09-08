.PHONY: build up down logs clean test

# --- Build Targets ---
build: 
	docker compose build

build-gateway:
	docker compose build eda-gateway

build-verilator:
	docker compose build verilator-api

# --- Run/Stop Targets ---
up:        # Start all services
	docker compose up -d
down:      # Stop all services
	docker compose down
logs:      # View logs
	docker compose logs -f
clean:     # Clean up everything
	docker compose down -v
	docker system prune -f

# --- Validator & Health Targets ---
test: start    # Run validator from terminal
	sleep 1  # Wait for services to be up
	python3 example_usage.py
health:    # Health check for gateway
	curl http://localhost:8080/health

# --- Development Targets ---
restart-gateway: build-gateway
	docker compose up -d eda-gateway
	@echo "Gateway restarted at: http://localhost:8080"
	@echo "Documentation: http://localhost:8080/docs"

# --- Quick Start ---
start: build up
	@echo "EDA Tools API Gateway starting..."
	@echo "Services will be available at:"
	@echo "  Gateway: http://localhost:8080"
	@echo "  Verilator: http://localhost:8001"
	@echo "  Documentation: http://localhost:8080/docs"