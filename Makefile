.PHONY: build up down logs clean test


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

# Test the services
test:
	python example_usage.py

# Fast development: restart only openlane-api after changes
restart-openlane: build-openlane
	docker compose up -d openlane-api
	@echo "OpenLane API restarted at: http://localhost:8003"
	@echo "Documentation: http://localhost:8003/docs"

# Fast development: restart only gateway after changes
restart-gateway: build-gateway
	docker compose up -d eda-gateway
	@echo "Gateway restarted at: http://localhost:8080"
	@echo "Documentation: http://localhost:8080/docs"

# Individual service commands

build-verilator:
	docker compose build verilator-api

build-openlane:
	docker compose build openlane-api

build-gateway:
	docker compose build eda-gateway

# Health check
health:
	curl http://localhost:8080/health

# Quick start (build + run)
start: build up
	@echo "EDA Tools API Gateway starting..."
	@echo "Services will be available at:"
	@echo "  Gateway: http://localhost:8080"
	@echo "  Yosys: http://localhost:8000" 
	@echo "  Verilator: http://localhost:8001"
	@echo "  Icarus: http://localhost:8002"
	@echo "  Documentation: http://localhost:8080/docs"

