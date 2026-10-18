# ChipForge EDA Tools Server (npu-v2)

The containerized evaluator that validators run next to their validator, and that miners can run
to test a design before submitting. It serves **npu-v2 only**: Verilator simulation, Yosys
synthesis and OpenSTA timing on the open **Nangate45** library. npu-v1 has ended: there is no
sky130 or OpenLane flow and no PDK download in this server.

---

## What runs

| compose service | image | role |
|---|---|---|
| `eda-gateway` | built from `gateway/` | the only published port (8080): `GET /health` and the `/v2/*` routes |
| `v2-evaluator` | `chipforge_eda_server-v2-evaluator:2.0` | installs signed evaluators and runs evaluations (Verilator 5.038, Yosys 0.56, Nangate45, DRAMsim3) |
| `cf-compiler` | `cf-compiler:v2.1-public` | box image: every miner compiler is built and run in it |
| `cf-rtl` | `cf-rtl:v2.0` | box image: RTL tool, simulator and firmware boxes (g++/make/python3, xPack RISC-V GCC 14.2.0) |
| `cf-sta` | `cf-sta:v2.0` | box image: OpenSTA 2.4.0 for post-synthesis timing |

`cf-compiler`, `cf-rtl` and `cf-sta` only build their images: each runs `true` once and shows
`Exited (0)` in `docker compose ps -a`, by design. The evaluator starts them itself as no-network,
read-only sibling containers, so it needs the Docker socket. Every image is built locally from
pinned sources (`pull_policy: build`); see `v2-evaluator/README.md` and `v2-compiler/README.md`.

```
.
├── docker-compose.yml   # the five services above
├── .env.example         # every setting (copy to .env)
├── Makefile
├── gateway/             # port 8080: /health, /v2/* pass-through, 410 on the retired /evaluate
├── v2-evaluator/        # evaluator image + the cf-rtl / cf-sta box images (Dockerfile targets)
└── v2-compiler/         # cf-compiler box image
```

---

## Quick start

1. **Prerequisites:** Linux amd64 with Docker and the Compose plugin, cgroup v2. For a full
   evaluation: 16+ cores and 64 GB RAM recommended, about 15 GB of disk for the images and their
   build, and a data root with at least 100 GiB free on a local POSIX filesystem (not NTFS/FUSE).

2. **Clone and configure:**
   ```bash
   git clone https://github.com/TatsuProject/chipforge_eda_server
   cd chipforge_eda_server
   cp .env.example .env
   ```
   In `.env` set at least `CF_V2_EVALUATOR_PUBLIC_KEY` (the publisher's `ssh-ed25519 AAAA...`
   public key line), `CF_V2_ROOT` (data root) and `CF_V2_SECRETS` (see Configuration).

3. **Build and start** (one command, no profile):
   ```bash
   docker compose up -d --build --remove-orphans      # or: make start
   ```
   The first build compiles Python, Verilator, Yosys and OpenSTA from source and takes a while.
   On a small machine build one image at a time with the legacy builder:
   `COMPOSE_PARALLEL_LIMIT=1 DOCKER_BUILDKIT=0 docker compose up -d --build --remove-orphans`
   (keep the same prefix on later builds so the layer cache is reused). A failed build is usually
   a download timeout: run it again.

4. **Check it:**
   ```bash
   curl http://localhost:8080/health       # {"status": "ok"}: the gateway
   curl http://localhost:8080/v2/health    # the evaluator's own health and preflight
   ```

5. **Validators:** point the validator's EDA server URL at `http://localhost:8080` and start it
   after this server is up.

**Updating:** `git pull`, compare `.env` with `.env.example`, then
`docker compose up -d --build --remove-orphans`.

**Upgrading a server that ran npu-v1:** the same command. `--remove-orphans` removes the old
`verilator-api` and `openlane-api` containers; their images
(`chipforge_eda_server-verilator-api`, `chipforge_eda_server-openlane-api`, about 6.6 GB with the
sky130 PDK) are no longer used and can be deleted with `docker rmi` when nothing else needs them.

---

## Routes

| route | what it does |
|---|---|
| `GET /health` | gateway liveness; runs nothing |
| `POST /v2/evaluator`, `GET /v2/evaluator[/<sha256>]` | install an evaluator / read install state |
| `POST /v2/evaluate` | design + evaluator ZIP/signature (automatic install), or a cached evaluator SHA; 202 accepted, 200 already finished, 409 conflicting request ID |
| `GET /v2/status/<request_id>` | 202 while active, 200 final result, 404 unknown |
| `GET /v2/health` | evaluator health |
| `/evaluate` (npu-v1) | **410 Gone**, `error.code = V1_RETIRED`; the request is not read |

The gateway forwards each `/v2` request body as a stream with its `Content-Length` and returns the
evaluator's status code and body unchanged (only `Location` is rewritten to `/v2/status/<id>`).
When the evaluator is not configured or is down, every `/v2` route returns 503 with a JSON body
(`V2_NOT_CONFIGURED` or `V2_UNREACHABLE`). The first `/v2/evaluate` installs the evaluator
(download, regeneration, preflight); progress is in `GET /v2/evaluator`. See
[the evaluator archive contract](v2-evaluator/EVALUATOR_ZIP.md).

---

## Configuration

All settings are in [`.env.example`](.env.example); docker compose reads `.env` from this folder.
Apply a change with `docker compose up -d`.

- `EDA_BIND_ADDRESS` (default `127.0.0.1`): interface port 8080 is published on (see Security).
- `CF_V2_EVALUATOR_PUBLIC_KEY`: the publisher's OpenSSH Ed25519 public key. Unsigned evaluators
  are refused unless `CF_V2_ALLOW_UNSIGNED=1` (a testnet override, recorded in every response).
- `CF_V2_ROOT`: the data root, an absolute path on a local POSIX filesystem with at least 100 GiB
  free, mounted at the same path inside the evaluator (sibling boxes bind-mount from it). The
  evaluator's preflight checks the space; `CF_V2_PREFLIGHT_MIN_DISK_GIB` lowers it on a testnet host.
  The loader makes it owner-only (0700, owned by the evaluator's root user) at start and refuses to
  start if that does not take effect: it holds hidden inputs, escrow, uploads and the queue
  database. Sibling boxes do not need to traverse it (each input is bind-mounted by path), so
  nothing below it is opened up. Inspect it with `sudo`.
- `CF_V2_SECRETS`: a directory outside the data root; the salt is generated there on first install,
  or place an existing one first (see the archive contract). Made 0700 at start, the salt 0600.
- `CF_V2_CORES`: a count of CPUs the evaluator may use (a positive integer, e.g. `8`; empty = every
  CPU the container can see). It is not a CPU list: `16-31` is refused at start with a message
  naming the variable. To confine the evaluator to specific CPUs, add a compose override such as
  `docker-compose.override.yml`:

  ```yaml
  services:
    v2-evaluator:
      cpuset: "16-31"
  ```

  and set `CF_V2_CORES=16` (the number of CPUs in that set) or leave it empty.
- `V2_ADMIN_TOKEN`: if set, `POST /v2/evaluator` (optional pre-warm) requires
  `Authorization: Bearer <token>`. Automatic installs verify publisher signatures regardless.

The box images are passed to the evaluator by compose as `CF_COMPILER_IMAGE`, `CF_RTL_IMAGE` and
`CF_STA_IMAGE`; they match the tags the services above build and need no setting.

## Security

The code is open source; what needs protecting is a **running** server. `/v2/evaluate` runs
evaluator code, so anyone who can reach port 8080 can make the machine work for them.

- **There is no API key for evaluations.** Access control is network-level: **never expose port
  8080 to the internet.** Allow it only from the machine(s) that send evaluations.
- **By default the port is bound to `127.0.0.1`**, so only the same machine can reach it. If the
  validator runs elsewhere, set `EDA_BIND_ADDRESS=0.0.0.0` **and** allow port 8080 only from the
  validator's IP.
- The evaluator runs privileged with the Docker socket (it creates the sandbox boxes and their
  cgroups). Run this server on a machine dedicated to it.

## Monitoring & troubleshooting

- `make health` checks both the gateway and the evaluator.
- `make logs` follows the gateway and evaluator logs; `make ps` shows every service, including
  the three image-only ones (`Exited (0)` is normal for them).
- A failed evaluator install returns only a stable code; operators find the step output under
  `$CF_V2_ROOT/logs/install/` (see `v2-evaluator/README.md`).

## Tests

`make test` (or `pytest gateway/tests v2-evaluator/tests`) runs the unit tests; no Docker needed.

---

## License
MIT License; see `LICENSE`.

## Support
- [Discord](https://discord.com/channels/799672011265015819/1408463235082092564)
- Email: contact@tatsuecosystem.io
