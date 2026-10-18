.PHONY: start build up down logs ps health test

# Build every image and start the server (the gateway and the v2 evaluator)
start:
	docker compose up -d --build --remove-orphans
	@echo "Gateway: http://localhost:8080  (v2 routes under /v2/*)"

# Build all images. On a small machine: COMPOSE_PARALLEL_LIMIT=1 DOCKER_BUILDKIT=0 make build
build:
	docker compose build

up:
	docker compose up -d --remove-orphans

down:
	docker compose down

logs:
	docker compose logs -f eda-gateway v2-evaluator

# cf-compiler, cf-rtl and cf-sta show "Exited (0)": they only build images
ps:
	docker compose ps -a

health:
	curl -fsS http://localhost:8080/health && echo && curl -sS http://localhost:8080/v2/health && echo

# Unit tests (no Docker needed): pip install fastapi==0.104.1 aiohttp==3.14.5 httpx pytest pyyaml
test:
	python3 -m pytest -q gateway/tests v2-evaluator/tests
