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

To test validators, the challenge server and the website end to end without waiting hours per design, the
gateway can answer `/v2/health`, `/v2/evaluate` and `/v2/status` itself, in the real v2 formats, without the
evaluator or any tool image. **Never use it on a validator that evaluates real submissions.**

```sh
make down                      # stop the real server if it runs (same port 8080)
make mock-start                # creates shared/eda_mock.json from gateway/mock_result.example.json
make mock-logs                 # [MOCK] lines for every evaluation
make mock-stop
```

`shared/eda_mock.json` is re-read for every evaluation (no restart):

| field | effect |
|---|---|
| `delay_seconds` | how long until the result is ready (`/v2/status` answers 202 until then) |
| `status` | `PASSED`, `REJECTED` (fault `miner`), `INCOMPLETE` or `ERROR` (fault `system`) |
| `S` | the score: a number, or `[low, high]` for a value derived from the design's sha256 (every validator gets the same S for the same design) |
| `S_offset` | added to S on this server only: simulates a dishonest or misconfigured validator (its projection hash differs too) |
| `failure_code` | for `REJECTED` |
| `projection` | `deterministic` (default: from the design, its chain_commit and the result) or `random` |

A design zip may carry `MOCK.json` at its top level with the same fields; they win for that design (one
miner's design rejected, another slow, another with a fixed S). The mock checks what the real evaluator
checks: T1/T2 need a `chain_commit` whose `package_sha256` is the zip's sha256, the evaluator sha256 must be
`/v2/health`'s `active_evaluator`, and a reused request ID must name the same bytes. Results carry
`"mock": true`, `projection_sha256` and a `public_report` built from a sample report with the mocked S.

## License and support

MIT License; see `LICENSE`. Support: [Discord](https://discord.com/channels/799672011265015819/1408463235082092564),
contact@tatsuecosystem.io.
