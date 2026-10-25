.PHONY: images start build-from-source up down logs ps health test

# Pull and verify the 4 published pinned images, from the SIGNED evaluator zip (check its signature
# first: see the setup guide). Once per evaluator release:  make images ZIP=/path/to/evaluator.zip
images:
	@test -n "$(ZIP)" || { echo "usage: make images ZIP=/path/to/chipforge_npu_v2_0_evaluator.zip"; exit 2; }
	rm -rf .images-zip && mkdir .images-zip
	unzip -q "$(ZIP)" 'v2/deploy/*' 'v2/pipeline/image_content.py' 'v2/pipeline/orchestrator/checker/validator_pins.json' -d .images-zip
	cd .images-zip && bash v2/deploy/load_images.sh pull docker

# Build the gateway (the only locally built image) and start the server. Never builds the 4 pinned images.
start:
	docker compose build eda-gateway
	docker compose up -d --no-build --remove-orphans
	@echo "Gateway: http://localhost:8080  (v2 routes under /v2/*)"

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
