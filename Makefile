.PHONY: images start evaluator build-from-source up down logs ps health test mock-start mock-stop mock-logs \
	hybrid-start hybrid-stop hybrid-logs

# Optional stronger check: pull and verify the 4 images against the SIGNED evaluator zip itself (check its
# signature first: see the setup guide):  make images ZIP=/path/to/evaluator.zip
images:
	@test -n "$(ZIP)" || { echo "usage: make images ZIP=/path/to/chipforge_npu_v2_0_evaluator.zip"; exit 2; }
	rm -rf .images-zip && mkdir .images-zip
	unzip -q "$(ZIP)" 'v2/deploy/*' 'v2/pipeline/image_content.py' 'v2/pipeline/orchestrator/checker/validator_pins.json' -d .images-zip
	cd .images-zip && bash v2/deploy/load_images.sh pull docker

# Pull the 4 pinned images by digest (images.lock), build the gateway (the only locally built image) and start
# the server, then install the evaluator named in .env (CF_V2_EVALUATOR_ZIP) and wait until it is ready. Never
# builds the 4 pinned images. This is the one command validators run (after git pull).
start:
	bash ./pull_images.sh
	docker compose build eda-gateway
	docker compose up -d --no-build --remove-orphans
	@echo "Gateway: http://localhost:8080  (v2 routes under /v2/*)"
	bash ./install_evaluator.sh

# Install the evaluator named by CF_V2_EVALUATOR_ZIP in .env and wait until it is ready (also part of make start).
evaluator:
	bash ./install_evaluator.sh

# Source audit only: rebuilds the 4 images from this repo. The evaluator REFUSES locally built images
# (new image identity), so never use these for validating.
build-from-source:
	docker compose build

up:
	docker compose up -d --no-build --remove-orphans

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

# TESTING ONLY: v2 mock mode, the gateway alone answering /v2/* from shared/eda_mock.json (no evaluator, no
# tool images, results in seconds). Stop the real server first (make down); never use this to validate.
mock-start:
	@test -f shared/eda_mock.json || { mkdir -p shared && cp gateway/mock_result.example.json shared/eda_mock.json && echo "created shared/eda_mock.json from the example"; }
	docker compose -f docker-compose.mock.yml up -d --build
	@echo "Mock EDA server: http://localhost:8080 (edit shared/eda_mock.json any time; it is re-read per evaluation)"

mock-stop:
	docker compose -f docker-compose.mock.yml down

mock-logs:
	docker compose -f docker-compose.mock.yml logs -f

# TESTING ONLY: hybrid mode, the real server with simulated evaluations. Evaluator installs are real (the
# validator installs the evaluator from the challenge server by itself); only /v2/evaluate is simulated.
HYBRID = docker compose -f docker-compose.yml -f docker-compose.hybrid.yml

hybrid-start:
	@test -f shared/eda_mock.json || { mkdir -p shared && cp gateway/mock_result.example.json shared/eda_mock.json && echo "created shared/eda_mock.json from the example"; }
	bash ./pull_images.sh
	$(HYBRID) build eda-gateway
	$(HYBRID) up -d --no-build --remove-orphans
	@echo "Hybrid EDA server: http://localhost:8080 (real installs, simulated evaluations; no evaluator installed until a validator sends one)"

hybrid-stop:
	$(HYBRID) down

hybrid-logs:
	$(HYBRID) logs -f eda-gateway v2-evaluator
