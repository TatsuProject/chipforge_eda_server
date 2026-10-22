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
   build, and a data root with at least 100 GiB free on a local POSIX filesystem (ext4 or xfs; not NTFS,
   FUSE, SMB or NFS: the loader refuses those).

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
- **Resources** (`CF_CPUS`, `CF_MEMORY`, `CF_EVALUATOR_CONFIG`). Empty = auto: the evaluator uses
  every CPU the container may use (affinity and any cgroup quota) and the RAM less max(4 GiB, 12.5%)
  for the OS. Override with `CF_CPUS` (`16`, `75%`) and `CF_MEMORY` (`160GiB`, `75%`), or put them in
  the evaluator config file, `$CF_V2_ROOT/evaluator.json` by default (`CF_EVALUATOR_CONFIG` names
  another in-container path):

  ```json
  {"resources": {"cpus": 32, "memory": "160GiB"},
   "waits": {"step_bound_factor": 8, "step_idle_s": 3600, "stage_idle_s": 7200,
             "command_idle_s": 7200, "permit_wait_s": 86400}}
  ```

  Environment beats the file. `waits` only decides when a run that shows no progress is ended as an
  unscored system fault (the evaluator's TESTNET.md lists every knob); the defaults suit a dedicated
  validator. The loader refuses to start, naming the file, when the file is not this shape (the old
  flat `{"cpus": 36}` form included); the evaluator validates the values and logs one `resources:`
  line with the plan it derived. Sizing changes wall time, never cycles or scores.
- `CF_V2_CORES` (deprecated): a count of CPUs, used only when nothing above sets cpus (a positive
  integer, e.g. `8`). It is not a CPU list: `16-31` is refused at start with a message
  naming the variable. To confine the evaluator to specific CPUs, add a compose override such as
  `docker-compose.override.yml`:

  ```yaml
  services:
    v2-evaluator:
      cpuset: "16-31"
  ```

  and leave `CF_CPUS` empty (auto then sees exactly that set).
- **Gate B pool** (`CF_GATEB_CACHE`, `CF_GATEB_MIRROR`). Gate B draws its private images per
  evaluation from a signed public pool and fetches only the drawn ones. Verified bytes are cached
  in `CF_GATEB_CACHE` (default `$CF_V2_ROOT/gateb-pool-cache`; ~250 KB per image, a few GB in
  total; set a path to put it on another disk). `CF_GATEB_MIRROR` (default
  `/var/lib/chipforge-v2-gateb-mirror`, created empty) is an optional read-only pre-seeded copy
  (`<sha[:2]>/<sha>`). Both are mounted at the identical path. Nothing in them is secret.
- **drand relays** (`CF_DRAND_RELAYS`, `CF_DRAND_TIMEOUT_S`, `CF_DRAND_DEADLINE_S`): see Network
  egress. Empty = the signed beacon policy's defaults.
- `V2_ADMIN_TOKEN`: if set, `POST /v2/evaluator` (optional pre-warm) requires
  `Authorization: Bearer <token>`. Automatic installs verify publisher signatures regardless.

The box images are passed to the evaluator by compose as `CF_COMPILER_IMAGE`, `CF_RTL_IMAGE` and
`CF_STA_IMAGE`; they match the tags the services above build and need no setting.

## Network egress

Only the `v2-evaluator` container (and the image builds) use the network. Every box it starts to
compile, simulate or analyse a miner's design runs with `--network none`; keep it that way: the
evaluation randomness is public once its drand round is out.

| what | when | outbound HTTPS (443) to |
|---|---|---|
| drand beacon round (one ~200-byte GET per T1/T2 evaluation, BLS-verified, cached) | every T1/T2 evaluation and its preflight | `api.drand.sh`, `api2.drand.sh`, `api3.drand.sh`, `drand.cloudflare.com` (or your `CF_DRAND_RELAYS`) |
| Gate B pool images (the drawn ones only; sha256-verified) | T2 evaluations whose images are not in the mirror or cache | `open-images-dataset.s3.amazonaws.com` (the origin URLs in the signed pool index) |
| evaluator public assets (457 files, ~315 MB: COCO images, model archives; sha256-verified) | once per new evaluator install, then cached in `$CF_V2_ROOT/assets` | the URLs in the zip's `assets.json`; v2.0: `s3.amazonaws.com` (COCO `images.cocodataset.org` bucket), `hailo-model-zoo.s3.eu-west-2.amazonaws.com`, `huggingface.co`, `download.pytorch.org`, `download.openmmlab.com`, `codeload.github.com` |
| image builds (`docker compose build`) | when building | `snapshot.ubuntu.com`, `www.python.org`, `github.com`, `download.docker.com`, `static.rust-lang.org`, PyPI (`pypi.org`, `files.pythonhosted.org`); all digest- or hash-pinned |

The evaluator's own Python dependencies are **not** fetched: they ship inside the signed zip as
hash-pinned wheels and install offline (`v2-evaluator/EVALUATOR_ZIP.md`). No inbound access is
needed beyond the gateway port (see Security). Relays and origins are untrusted transport: every
byte is checked against a pinned key or hash, so a proxy that cannot alter content is fine.

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
  `$CF_V2_ROOT/logs/install/` (see `v2-evaluator/README.md`). Reading it needs `sudo`.
- **The data root is root-owned and private.** At start the loader chowns `CF_V2_ROOT` to the
  evaluator's root user and makes it `0700`, and makes `CF_V2_SECRETS` `0700` and the salt `0600`.
  Read results through the gateway API (`GET /v2/status/<request_id>` returns the full final
  response), not from files. Install logs and job directories on the host need `sudo`.
- **The evaluator keeps restarting with "... must be on a local POSIX filesystem" or "... is mode
  0777; it must admit no group/other access"** (`make logs`): `CF_V2_ROOT` or `CF_V2_SECRETS` is on
  NTFS, FUSE, SMB/CIFS, NFS, a VirtualBox/9p share or a similar filesystem. Those ignore or emulate
  owner-only modes, and their file locks (used for the job queue and the simulation box permits)
  are not reliable, so the loader refuses them, even when mounted to accept `chmod`. Move both to a
  local ext4 or xfs filesystem, update `.env`, and restart.

- **Cancel an evaluation** (operator only; there is deliberately no HTTP endpoint):

  ```sh
  docker compose exec v2-evaluator cf-cancel <request_id> --reason "host maintenance"
  ```

  The request becomes a terminal `ERROR` (`EVALUATION_CANCELLED`, system fault, never scored); a
  running stage and its boxes are stopped within about a minute; the caller sees it on its next
  status poll and the loader moves on. Do **not** stop containers, edit `http.sqlite` or move job
  directories by hand: a request the loader still believes in-flight blocks every later one.
  Resubmit under a new `request_id`.

## Tests

`make test` (or `pytest gateway/tests v2-evaluator/tests`) runs the unit tests; no Docker needed.

---

## License
MIT License; see `LICENSE`.

## Support
- [Discord](https://discord.com/channels/799672011265015819/1408463235082092564)
- Email: contact@tatsuecosystem.io
