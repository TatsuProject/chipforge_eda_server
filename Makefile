.PHONY: build up down logs clean test

# Build all Docker images (smart - only builds changed services)
build: build-changed

# Force build all services (always rebuilds everything)
build-all:
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
build-yosys:
	docker compose build yosys-api

build-verilator:
	docker compose build verilator-api

build-icarus:
	docker compose build icarus-api

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

# Build only changed services (fast development)
build-changed:
	@echo "Building only services with recent changes..."
	@if [ -n "$$(find ./openlane-api -name '*.py' -newer .last_build_openlane 2>/dev/null)" ]; then \
		echo "Changes detected in openlane-api, rebuilding..."; \
		docker compose build openlane-api && touch .last_build_openlane; \
	fi
	@if [ -n "$$(find ./yosys-api -name '*.py' -newer .last_build_yosys 2>/dev/null)" ]; then \
		echo "Changes detected in yosys-api, rebuilding..."; \
		docker compose build yosys-api && touch .last_build_yosys; \
	fi
	@if [ -n "$$(find ./verilator-api -name '*.py' -newer .last_build_verilator 2>/dev/null)" ]; then \
		echo "Changes detected in verilator-api, rebuilding..."; \
		docker compose build verilator-api && touch .last_build_verilator; \
	fi
	@if [ -n "$$(find ./icarus-api -name '*.py' -newer .last_build_icarus 2>/dev/null)" ]; then \
		echo "Changes detected in icarus-api, rebuilding..."; \
		docker compose build icarus-api && touch .last_build_icarus; \
	fi
	@if [ -n "$$(find ./gateway -name '*.py' -newer .last_build_gateway 2>/dev/null)" ]; then \
		echo "Changes detected in gateway, rebuilding..."; \
		docker compose build eda-gateway && touch .last_build_gateway; \
	fi

# Initialize build tracking
init-build-tracking:
	@touch .last_build_openlane .last_build_yosys .last_build_verilator .last_build_icarus .last_build_gateway