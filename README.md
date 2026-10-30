# ChipForge EDA Server (Quest 2)

The EDA server evaluates Quest 2 (NPU v2.0) designs. **Validators** run it next to their validator neuron to
score submissions; **miners** run the same server to test a design before submitting it. It simulates (Verilator),
synthesises (Yosys) and times (OpenSTA) designs on the open Nangate45 library.

The judging code is not part of this repository. It is the **evaluator**: one zip file, released for each version
of the rules and signed by the subnet owner. This server installs an evaluator once, checks its signature, and
reuses it until a new one is released.

## Requirements

- Linux x86-64 with cgroup v2 (Ubuntu 22.04 or 24.04), Docker Engine with the Compose plugin.
- Validators: EC2 c7a.4xlarge (16 cores, 32 GiB RAM) or better, 300 GiB disk, on a machine used only for this.
- Miners: the same for a full evaluation; a smaller machine runs the quick checks.
- Data directories on a local ext4 or xfs filesystem (NTFS, FUSE, SMB and NFS are refused).
- Outbound HTTPS. No inbound access is needed.
- The evaluator refuses to install with less than about 22 GiB of RAM available. For testing on a smaller
  machine (a laptop), use [mock mode](#mock-mode-testing-only).

## Set up

```sh
git clone -b launch-v2-eda https://github.com/TatsuProject/chipforge_eda_server
cd chipforge_eda_server
cp .env.example .env
sudo mkdir -p /var/lib/chipforge-v2 /var/lib/chipforge-v2-secrets
```

Edit `.env`:

| Setting | Value |
|---|---|
| `CF_V2_ROOT` | `/var/lib/chipforge-v2` (data directory, 100 GiB free) |
| `CF_V2_SECRETS` | `/var/lib/chipforge-v2-secrets` |
| `CF_V2_EVALUATOR_ZIP` | absolute path of the evaluator zip, with its `.sshsig` next to it. Optional: leave empty if your validator neuron installs the evaluator |

Everything else is preset. `CF_V2_EVALUATOR_PUBLIC_KEY` holds the subnet owner's public key: the server installs
only evaluators signed with the matching private key.

## Start

```sh
make start
```

This pulls the four tool images from Docker Hub by fixed digest (about 2 GB the first time), starts the server
and, if `CF_V2_EVALUATOR_ZIP` is set, installs the evaluator and waits. It ends with:

```
Evaluator ready: <evaluator sha256>
```

The first install takes a few minutes plus a ~1 GB download. The installed evaluator is kept and reused after
restarts and reboots. Check the server at any time:

```sh
curl -sS http://localhost:8080/health       # {"status": "ok"}
curl -sS http://localhost:8080/v2/health    # "ready": true once an evaluator is installed
```

Never build the tool images yourself (`make build-from-source` is for source audit only): a rebuilt image is a
different image, and every evaluation would end `INCOMPLETE` (`VALIDATOR_ENVIRONMENT_MISMATCH`).

## Validators

Your validator neuron talks to `http://localhost:8080`. For each submission it sends the design zip, `mode=T2`,
the submission's `chain_commit` and the evaluator's sha256, then reads the result:

```sh
curl -sS -F design_zip=@design.zip -F mode=T2 -F request_id=<unique id> -F submitter=<miner hotkey> \
     -F evaluator_sha256=<sha256> --form-string "chain_commit=$(cat chain_commit.json)" \
     http://localhost:8080/v2/evaluate                    # HTTP 202: accepted
curl -sS http://localhost:8080/v2/status/<unique id>      # HTTP 202 while running, 200 with the result
```

A full evaluation takes about 3 hours on the minimum machine; the server runs one at a time and queues the rest.
The integration guide distributed with each release explains the neuron, the challenge server and `chain_commit`
in full.

## Miners: test a design

Use the evaluator from the miner folder, set it in `CF_V2_EVALUATOR_ZIP`, and run `make start`. A full evaluation
needs a `chain_commit` object; for a local test, make your own:

```sh
python3 - design.zip > my_chain_commit.json <<'EOF'
import hashlib, json, sys, time
print(json.dumps({"schema": "cf-chain-commit/1", "subtensor_genesis_hash": "0" * 64, "netuid": 108,
                  "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", "block_number": 1,
                  "block_hash": "0" * 64, "block_timestamp_ms": int(time.time() * 1000),
                  "package_sha256": hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()}))
EOF
curl -sS -F design_zip=@design.zip -F mode=T2 -F request_id=test-1 -F evaluator_sha256=<sha256> \
     --form-string "chain_commit=$(cat my_chain_commit.json)" http://localhost:8080/v2/evaluate
curl -sS http://localhost:8080/v2/status/test-1
```

Your local hidden tests differ from the validators': validators draw them from the block at which your
submission was received and a public random value published after it, which nobody can know in advance. A design
that passes locally passes on any draw only if it is correct in general, which is what the evaluation checks.

## Updating

```sh
git pull && make start
```

For a new evaluator, point `CF_V2_EVALUATOR_ZIP` at it and run `make start` (validator neurons install new
evaluators automatically). A server that ran Quest 1 is replaced in place by the same commands (same port 8080);
the old `chipforge_eda_server-verilator-api` and `chipforge_eda_server-openlane-api` images can then be removed.

## Security

- Port 8080 runs submitted code and has no API key. It is bound to `127.0.0.1` by default; never expose it to the
  internet. If the neuron runs on another machine, set `EDA_BIND_ADDRESS=0.0.0.0` and allow port 8080 only from
  that machine's IP.
- The evaluator container is privileged and controls Docker: use a dedicated machine.
- Designs are compiled, simulated and analysed in containers with no network access.
- Every download (evaluator assets, Gate B images, the drand random value) is checked against a pinned
  fingerprint or signature, so a proxy or relay cannot change any data.

## Reference

| Route | Purpose |
|---|---|
| `GET /health` | gateway is up |
| `GET /v2/health` | evaluator readiness, active evaluator, install failures |
| `POST /v2/evaluator` | install an evaluator (`evaluator_zip`, `evaluator_sig`) |
| `GET /v2/evaluator/<sha256>` | install progress (`phase`: ... `ready` or `failed`) |
| `POST /v2/evaluate` | start an evaluation |
| `GET /v2/status/<request_id>` | 202 while running, 200 with the result, 404 unknown |

| Command | Purpose |
|---|---|
| `make start` | pull images, start, install the evaluator from `.env` |
| `make evaluator` | install the evaluator from `.env` only |
| `make health` | check the gateway and the evaluator |
| `make logs` | follow the gateway and evaluator logs |
| `make ps` | list services (`cf-compiler`, `cf-rtl`, `cf-sta` showing `Exited (0)` is normal) |
| `make down` | stop the server |

Optional settings in `.env.example`: `CF_CPUS` and `CF_MEMORY` (limit a shared machine; never changes scores),
`CF_GATEB_CACHE` and `CF_GATEB_MIRROR` (Gate B image cache location), `CF_DRAND_RELAYS` (randomness relays),
`V2_ADMIN_TOKEN` (protects `POST /v2/evaluator`).

Outbound HTTPS is used for: Docker Hub (images), the drand relays `api.drand.sh`, `api2.drand.sh`,
`api3.drand.sh`, `drand.cloudflare.com` (one small request per evaluation), the Gate B image origin
`open-images-dataset.s3.amazonaws.com`, and, once per evaluator, the evaluator's files (`github.com` and
`objects.githubusercontent.com` for the model files, `s3.amazonaws.com`,
`hailo-model-zoo.s3.eu-west-2.amazonaws.com`, `huggingface.co`, `download.pytorch.org`, `download.openmmlab.com`,
`codeload.github.com`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make start` reports the install `failed` | `curl -sS http://localhost:8080/v2/health`: `install_failures` names the log under `/var/lib/chipforge-v2/logs/install/` (read it with `sudo`) |
| the evaluator restarts in a loop with "must be on a local POSIX filesystem" or "mode 0777" | the data directories are on NTFS/FUSE/SMB/NFS: move both to ext4 or xfs, update `.env`, `make start` |
| images cannot be pulled | no access to Docker Hub, or its anonymous rate limit: `docker login` with any free account |
| `INCOMPLETE` with `VALIDATOR_ENVIRONMENT_MISMATCH` | the tool images were rebuilt or changed: `make start` again (pulls the pinned ones) |
| HTTP 404 `EVALUATOR_NOT_CACHED` | the evaluator was never installed on this server: set `CF_V2_EVALUATOR_ZIP` and `make start` |
| an evaluation must be stopped | `docker compose exec v2-evaluator cf-cancel <request_id> --reason "..."`; never stop containers or edit the data directory by hand |

Technical details of the evaluator format: [`v2-evaluator/EVALUATOR_ZIP.md`](v2-evaluator/EVALUATOR_ZIP.md).
Unit tests: `make test` (no Docker needed).

## Mock mode (testing only)

Mock mode lets validators, the challenge server and the website be tested end to end in minutes instead of
hours per design, on any machine. The gateway answers the v2 API itself, in the real formats, without the
evaluator service or any tool image. **Never use it on a validator that evaluates real submissions.**

### Which mode

| mode | command | evaluations | evaluator installs | machine |
|---|---|---|---|---|
| real | `make start` | real (1.5-2 h per design) | real | 16 cores, 32 GiB RAM, 300 GiB disk |
| **mock** | `make mock-start` | simulated (`delay_seconds`) | simulated (`install_seconds`) | anything (one small container) |
| hybrid | `make hybrid-start` | simulated | real (signature, ~1 GB assets, post-install, preflight) | **at least ~22 GiB free RAM** |

The real evaluator's preflight refuses to install when less than about 22 GiB of RAM is *available* ("RAM
minimum" in the install log, `EVALUATOR_PREFLIGHT_FAILED`): a 16 GB laptop, or a 32 GB laptop with a browser
and an IDE open, cannot run `make start` or hybrid mode. Use mock mode there.

### Quick start (mock)

```sh
cd chipforge_eda_server        # branch launch-v2-eda
make down                      # stop the real server if it runs (same port 8080); never needed otherwise
make mock-start                # builds the gateway, creates shared/eda_mock.json from gateway/mock_result.example.json
curl -s localhost:8080/v2/health      # {"ready": true, ..., "mock": true}
make mock-logs                 # one [MOCK] line per install and per evaluation
make mock-stop
```

Nothing else is needed: no `.env` settings, no `CF_V2_EVALUATOR_ZIP`, no images, no data root.

### The validator next to it

In the SN108 validator's `.env` (only these matter for mock testing):

```sh
EDA_SERVER_URL=http://localhost:8080
EDA_API=v2
EDA_MODE=T2
EDA_POLL_SECONDS=10             # results are ready in seconds, poll often
EVALUATOR_RETRY_MINUTES=1       # retry a failed (simulated) install quickly
STATUS_REPORT_SECONDS=30
```

The validator installs the evaluator the challenge server names by itself: it downloads the bundle's
evaluator zip and `.sshsig`, sends them to `POST /v2/evaluator`, follows the phases and reports *Downloading
evaluator*, *Installing evaluator*, then *Idle* with the version ready (logs: `Status: ...`; website:
Validators tab). The website marks a validator whose EDA server is a mock with a MOCK badge.

### The challenge server side

The quest's test-cases file is an evaluator bundle (see EVALUATOR_UPDATES.md in chipforge_challenge_server):
the evaluator zip, its `.sshsig` and an optional `VERSION.txt`. The challenge server checks the signature
for real, so use either:

- the **released bundle** (signed by the subnet owner): nothing to configure; or
- **test-signed bundles**, to test updates (v1 -> v2 -> v3) with the real evaluator but without asking the
  publisher for new builds:
  ```sh
  cd chipforge_challenge_server
  python3 scripts/dev/make_test_evaluator.py <path>/chipforge_npu_v2_0_evaluator.zip --versions 3 --out dev/challenges
  # prints the test public key: put it in dev/env.lan as EVALUATOR_PUBLIC_KEY=ssh-ed25519 ..., then make lan
  make lan-seed ID=<id> ZIP=dev/challenges/<id>.zip TESTCASES=dev/challenges/quest2_testcases_v1.zip
  make lan-testcases FILE=dev/challenges/quest2_testcases_v2.zip     # an update mid-quest
  ```
  v1 is the release's bytes; vN only changes MANIFEST.json, so it gets a new sha256. The mock does not
  check signatures, so no key is needed on the EDA side.

The challenge server refuses results from a mock EDA server unless `ALLOW_MOCK_RESULTS=true` (set in its
`dev/env.lan` and `dev/env.dev`; never in production, where a validator misconfigured with a mock must not
count). Without it, every mock result is answered 409 "produced by a mock EDA server".

### shared/eda_mock.json

Re-read for every evaluation and every install (no restart). Defaults in brackets.

```json
{
  "delay_seconds": 60,
  "status": "PASSED",
  "S": [1.0, 6.0],
  "S_offset": 0,
  "failure_code": "NPU_PORT_MISMATCH",
  "projection": "deterministic",
  "install_seconds": 20,
  "install_fail": null
}
```

| field | effect |
|---|---|
| `delay_seconds` | seconds until an evaluation's result is ready; `/v2/status` answers 202 until then [10] |
| `status` | `PASSED`; `REJECTED` (the miner's fault, score 0); `INCOMPLETE` or `ERROR` (system fault: the challenge server hands the design to another validator) [PASSED] |
| `S` | the score: a number, or `[low, high]` for a value derived from the design's sha256, so every validator gets the same S for the same design [[1.0, 6.0]] |
| `S_offset` | added to S on this server only: a dishonest or misconfigured validator (its projection hash differs too) [0] |
| `failure_code` | the code of a `REJECTED` result [NPU_PORT_MISMATCH] |
| `projection` | `deterministic` (from the design, its chain_commit and the result: equal on honest servers) or `random` (a non-deterministic evaluator) [deterministic] |
| `install_seconds` | how long a simulated evaluator install takes, spread over its phases [20] |
| `install_fail` | a code, e.g. `"EVALUATOR_PREFLIGHT_FAILED"`: installs fail at preflight with it, as on a machine with too little RAM. Back to `null` and the validator's next retry succeeds [null] |

Environment of the mock container (docker-compose.mock.yml): `EDA_MOCK_FILE` (the file above, enables mock
mode), `EDA_MOCK_INSTALL` (`simulated`, or `real` for hybrid), `EDA_MOCK_EVALUATOR_SHA256` (the evaluator
that counts as installed from the start, default `000...0001`), `V2_ADMIN_TOKEN` (if set, installs need
`Authorization: Bearer <token>`; the validator sends `EDA_ADMIN_TOKEN`), `EDA_MOCK_MAX_UPLOAD_MB` (largest
design or evaluator upload, default 300; uploads need a Content-Length). The mock remembers the last 20
simulated installs and 5000 jobs.

### Per design: MOCK.json

A design zip may hold `MOCK.json` at its top level with the same result fields; they win for that design.
Useful designs for a test round:

| design | MOCK.json | expected |
|---|---|---|
| good | `{"S": 6.0}` | a record at 6.0 (two validators agree) |
| better | `{"S": 7.5}` | a record at 7.5 |
| low | `{"S": 3.0}` | evaluated, not a record |
| rejected | `{"status": "REJECTED", "failure_code": "NPU_PORT_MISMATCH"}` | not a record, reason shown |
| broken | `{"status": "INCOMPLETE"}` | system fault on every validator, then *Not scored* |
| slow | `{"delay_seconds": 2400}` | runs past the evaluation window: timeout |
| plain | none | S from the range, the same on every validator |

### What the mock checks (like the real server)

- `POST /v2/evaluate`: multipart `design_zip`, `mode`, `request_id`; T1/T2 need a `chain_commit`
  (`cf-chain-commit/1`, 8 fields) whose `package_sha256` is the zip's sha256; the evaluator named by
  `evaluator_sha256` must be installed and ready here (`EVALUATOR_NOT_CACHED` / `EVALUATOR_NOT_READY`
  otherwise); a reused request ID must name the same bytes, chain_commit and evaluator (409).
- `POST /v2/evaluator`: `evaluator_zip` + `evaluator_sig`; the zip must hold a readable `MANIFEST.json`, the
  signature must be an OpenSSH signature (its key is not checked), `evaluator_sha256` if sent must match.
  `GET /v2/evaluator/<sha>` then reports `verify`, `extract`, `fetch_assets`, `post_install`, `preflight`,
  `ready` (or `failed` with `error`), and `/v2/health` names the newest ready evaluator.
- Results are cf-response/1 with `"mock": true`, `evaluator_sha256`, `projection_sha256` and a
  `public_report` built from a sample report with the mocked S.

### Checking it by hand

```sh
curl -s localhost:8080/v2/health | jq                 # ready, active_evaluator, install_failures, mock
curl -s localhost:8080/v2/evaluator | jq              # every evaluator and its phase
curl -s localhost:8080/v2/evaluator/<sha256> | jq     # one install's phase / error
make mock-logs                                        # [MOCK] installing evaluator ... / accepted ... / PASSED S=...
```

Recipes:

- **A failed install**: set `"install_fail": "EVALUATOR_PREFLIGHT_FAILED"`, upload a new evaluator version
  (`make lan-testcases ...`): the validator shows *Install failed · EVALUATOR_PREFLIGHT_FAILED* and gets no
  work; set it back to `null`: it retries after `EVALUATOR_RETRY_MINUTES` and recovers.
- **A cheating validator**: `"S_offset": 2.5` on one laptop only: its results are outvoted.
- **An update while a design is in flight**: give a design `{"delay_seconds": 600}`, upload a new version
  while it evaluates: it finishes on the old version, newer designs wait (*Waiting for evaluator update*).
- **Restart**: `make mock-stop && make mock-start` forgets the simulated installs: the validator installs the
  current evaluator again by itself (a good test of recovery).

### Hybrid mode: real installs, simulated evaluations

Only on a machine with at least ~22 GiB of RAM free. To test the real install (signature against
`CF_V2_EVALUATOR_PUBLIC_KEY`, ~1 GB of assets, post-install, preflight) while evaluations take seconds:

```sh
make mock-stop                 # same port 8080
make hybrid-start              # pinned images + the real server (.env as for make start) + the mock for evaluations
make hybrid-logs
make hybrid-stop
```

- Leave `CF_V2_EVALUATOR_ZIP` empty: nothing is installed until a validator sends an evaluator.
- `CF_V2_PREFLIGHT_MIN_DISK_GIB=20` in `.env` on a laptop (the preflight wants 100 GiB free otherwise).
- With test-signed bundles, `CF_V2_EVALUATOR_PUBLIC_KEY` must be the test key. To keep a real setup untouched,
  give the test its own data root on the command line (overrides `.env`):
  ```sh
  CF_V2_ROOT=/var/lib/chipforge-v2-test CF_V2_SECRETS=/var/lib/chipforge-v2-test-secrets \
  CF_V2_EVALUATOR_PUBLIC_KEY="ssh-ed25519 AAAA... chipforge-evaluator-TEST" CF_V2_PREFLIGHT_MIN_DISK_GIB=20 \
  make hybrid-start
  # afterwards: make hybrid-stop; sudo rm -rf /var/lib/chipforge-v2-test /var/lib/chipforge-v2-test-secrets
  ```
- `/v2/health` is the real one (plus `"mock": true`); `/v2/evaluate` is refused until the requested evaluator
  is really installed. A failed preflight is explained in `$CF_V2_ROOT/logs/install/<sha>.preflight-T2.log`
  (root only: `sudo cat`).

### Troubleshooting (mock)

| symptom | cause / fix |
|---|---|
| `make mock-start`: port 8080 already allocated | the real server or hybrid runs: `make down` / `make hybrid-stop` |
| validator: *EDA server offline* | `curl localhost:8080/v2/health`; `EDA_SERVER_URL` in the validator's `.env` |
| validator stays *Waiting for evaluator* | the challenge server has no valid bundle (website: *Last upload refused: ...*) or the validator cannot download it (its log) |
| `EVALUATOR_NOT_CACHED` in a result | the validator asked for an evaluator this mock never installed (e.g. the mock restarted mid-evaluation): it re-installs on its next cycle |
| results never arrive | `delay_seconds` too large for the batch window, or the design's `MOCK.json` sets a long delay |
| results refused: "produced by a mock EDA server" | the challenge server needs `ALLOW_MOCK_RESULTS=true` (LAN / dev stacks only) |
| `413` on an upload | larger than `EDA_MOCK_MAX_UPLOAD_MB` (300) |
| scores differ between laptops | `S_offset` set on one of them, or a number vs a range in `S` |

## License and support

MIT License; see `LICENSE`. Support: [Discord](https://discord.com/channels/799672011265015819/1408463235082092564),
contact@tatsuecosystem.io.
