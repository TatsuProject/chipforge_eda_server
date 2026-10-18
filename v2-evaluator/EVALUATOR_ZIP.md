# Evaluator archive contract (C2 + C6 / C7 envelope)

This public tools image contains the loader, never the evaluator. C7 must produce
an archive matching this versioned contract (`cf-evaluator-zip/1`). The tests build an entirely synthetic
HTTP service; they do not contain evaluation logic or challenge/reference data.

An archive is a ZIP of the evaluator repository root, with no enclosing directory:

```text
MANIFEST.json
assets.json
v2/deploy/service.py
v2/deploy/preflight.py
v2/...                       # supplied by the private publisher
harness/...                  # if required by that evaluator
policy/policy.json
```

`MANIFEST.json` uses the C7 `cf-evaluator-zip/1` envelope:

```json
{
  "schema": "cf-evaluator-zip/1",
  "version": "2.0.0",
  "created": "2026-01-01T00:00:00Z",
  "git_commit": "<publisher source commit>",
  "source_dirty": false,
  "entrypoint": {"service": "v2/deploy/service.py", "preflight": "v2/deploy/preflight.py"},
  "files": {"<every regular archive file except MANIFEST.json>": "<sha256>"},
  "policy": "policy/policy.json",
  "assets": "assets.json",
  "python": {},
  "post_install": []
}
```

The loader verifies the complete file inventory and every declared hash before
running code. Audit metadata (`version`, `created`, `git_commit`, `source_dirty`,
`python`) stays local; it never installs alternate dependencies from that metadata.
The image supplies the pinned interpreters and packages. The original manifest
is preserved for the evaluator's own provisioning code.

All paths are relative to the archive root. Service, preflight, policy and assets
files must exist. Policy is keyed by enabled modes (`DEV`, `T1`, `T2`) and is
passed unchanged to the service. The publisher supplies frozen baseline constants
in its policy/code: the loader performs **no baseline evaluation**. The current
native service's support for that policy remains the publisher's responsibility.
There is no mode or policy supplied by a miner beyond the selected enabled mode.

Archives are limited to 256 MiB compressed, 512 MiB extracted and 20,000 entries.
Extraction rejects absolute/traversal/backslash/drive paths, duplicate entries,
symlinks, devices, FIFOs, encrypted entries and reserved `.cf-*` root names.
Installation stages under `$CF_V2_ROOT/evaluators/`; post-install and preflight
run from the final `$CF_V2_ROOT/evaluators/<sha256>/` path. The ready tree is
read-only and a loader-owned `.cf-ready.json` records the verified install.
Ready markers bind the trust key, unsigned state, enabled modes and sandbox
override; changing those settings requires verification/preflight again.

## Authentication

Set `CF_V2_EVALUATOR_PUBLIC_KEY` to the publisher's **OpenSSH Ed25519 public key**
(`ssh-ed25519 AAAA...`). Alternatively the standalone loader accepts
`CF_V2_EVALUATOR_PUBLIC_KEY_FILE` naming a mounted public key file. The signing
private key stays with the publisher; never put it in compose or this image.

Sign the exact ZIP bytes with OpenSSH's detached Ed25519 signature format:

```sh
ssh-keygen -Y sign -f publisher-key -n chipforge-evaluator evaluator.zip
sha256sum evaluator.zip
```

Send the resulting `evaluator.zip.sig` as `evaluator_sig` (`signature` is accepted
as an alias). For C7 releases, use its OpenSSH `.sshsig` output. Verification uses `ssh-keygen -Y verify`, signer identity
`evaluator`, namespace `chipforge-evaluator`. No extra Python crypto dependency
is needed. ZIP SHA-256 is computed while accepting the archive and checked again
before any extraction or execution. If `evaluator_sha256` accompanies the ZIP,
it must match those bytes. There is no signature inside the ZIP (it cannot sign
its own containing bytes). The detached signature is stored with the archive.
A supplied invalid signature is refused even with the unsigned override enabled.

Unsigned ZIPs fail closed unless `CF_V2_ALLOW_UNSIGNED=1`. Every loader response
records the configured override in `X-CF-V2-Allow-Unsigned: 0|1`. Job responses
also record the actual install's `X-CF-Evaluator-Unsigned: 0|1`; these headers
survive the gateway. These are headers so terminal evaluator bodies remain
byte-for-byte unchanged. Install/health JSON also exposes the override.

## Public assets and regeneration

`assets.json` has the following shape:

```json
{
  "schema": "cf-evaluator-assets/1",
  "assets": [
    {
      "id": "public-example",
      "url": "https://example.org/public-data.bin",
      "sha256": "<64 lowercase hex digits>",
      "bytes": 1234,
      "dest": "data/public-data.bin",
      "unpack": "none",
      "licence": "publisher-declared licence"
    }
  ]
}
```

The declared `bytes` bounds the download (otherwise its ceiling is 16 GiB). HTTP(S) URLs only,
including after redirects; no URL credentials or fragments. HTTP is supported for
local fixtures; publishers should use HTTPS. Cached files are rehashed before
reuse at `$CF_V2_ROOT/assets/<sha256>`. A locked `.part` file supports Range resume;
a server returning 200 instead of 206 restarts the file. Final hashes and optional
sizes are always verified before atomic publication. Failed/truncated transfers
retain resumable prefixes; hash mismatches discard them.

For `unpack: none`, targets are regular copies under `$CF_V2_ROOT`, so sibling host-daemon bind
mounts work at the identical path. No symlink targets or escapes are allowed.
The loader reserves `assets`, `archives`, `evaluators`, `loader`, `tools`, `state`,
`escrow`, `locks`, `tmp`, `lanes` and dot-prefixed root names. A target already
holding different bytes fails closed; evaluators sharing a root should use
versioned target names. `unpack: zip` and `unpack: tar.gz` extract into the
`dest` directory with traversal/device/size guards (16 GiB expanded,
200,000 entries per archive). Only regular files and directories are materialized:
symbolic and hard link members (common in upstream source tarballs) are skipped,
never created or followed; a link whose own name is unsafe still fails the install,
and devices/FIFOs are refused. The evaluator ZIP itself refuses every link. Several archives may share a directory; existing
files must have identical bytes. No existing file is overwritten with different
content, and no pre-existing symlink is followed. Assets are fetched only while installing, never when
forwarding a design to the service.

Optional `post_install` steps regenerate local data using the image's pinned
interpreters. Every declared output must be hash-checked:

```json
{
  "id": "regenerate-example",
  "argv": ["{python}", "{evaluator}/regenerate.py", "--out", "{root}/data/generated.bin"],
  "timeout_s": 7200,
  "produces": [{"path": "data/generated.bin", "sha256": "<64 lowercase hex digits>"}]
}
```

Commands are argv arrays, executed without a shell in the evaluator directory.
Allowed substitutions are `{python}`, `{evaluator}`, `{root}`, `{assets}`, `{state}`,
`{posix}`. Literal braces must be doubled. `timeout_s` defaults to 7200, bounded
at 86400. The same target rules apply to declared outputs. Empty `produces` lists are permitted for no-op steps. Step/preflight stdout
and stderr go to an operator-only log, `$CF_V2_ROOT/logs/install/<sha>.<step>.log`
(file 0600, directory 0700; `<step>` is `post_install-<n>` or `preflight-<mode>`), never to
the HTTP caller, so provisioning diagnostics cannot leak through the API.
The evaluator is authenticated, privileged code; these are integrity checks,
not an isolation boundary for an untrusted evaluator publisher.

## Service, preflight and secrets

The loader invokes preflight once for **each enabled mode**, using the pinned
orchestrator interpreter, with:

```text
python -B <preflight> --mode <mode> --scratch <state> --posix <escrow> --json
```

Exit 0 is required. An explicit `CF_V2_ALLOW_RUNC=1` appends `--allow-runc` to both
preflight and service, and is recorded in health. The evaluator must implement
that flag/recording contract. Preflight has a 300 second ceiling.

The preflight also checks free space under the data root against the evaluator's own
minimum (100 GiB for the v2.0 evaluator); a smaller disk fails the install with
`EVALUATOR_PREFLIGHT_FAILED`. A non-empty `CF_V2_PREFLIGHT_MIN_DISK_GIB=N` appends
`--min-disk-gib N` to preflight, is bound into the ready marker (changing it re-runs
preflight) and is reported as `preflight_min_disk_gib` in health. Lower it only on a
testnet/development host that runs one evaluation at a time. Preflight output may
name private paths: it is kept only in the operator log above, and the install state shows
only the failure code. On `POST_INSTALL_FAILED` or `EVALUATOR_PREFLIGHT_FAILED`, read that log on the host.

The service runs on a private loopback port, with the native service CLI:

```text
python -B <service> --host 127.0.0.1 --port <port>
  --state $CF_V2_ROOT/state/<sha> --posix $CF_V2_ROOT/escrow/<sha>
  --policy <policy> --workers <workers> --cores <cores> --lanes $CF_V2_ROOT/lanes
```

It must implement `GET /health`, `POST /evaluate` (Content-Length multipart
`design_zip`, `request_id`, `mode`, `submitter`) and `GET /status/<id>`. HTTP 202
reports `state` (`admitting`/`queued`/`running`); HTTP 200 is the full terminal
`cf-response/1` body. IDs must be idempotent across service restarts. The loader
restarts a dead service, retains durable requests, and resubmits only if the
service returns 404. Response bodies up to 16 MiB are forwarded unchanged.
One active service/job runs at a time; switching versions drains the old service.
On normal SIGTERM the loader stops accepting and drains the service. After an
abrupt service-parent death, orphan broker/worker processes in its own process
group receive SIGTERM, then SIGKILL after five seconds; the service's durable
leases/checkpoints govern recovery. Queued loader jobs persist across restart.

`CF_V2_ROOT` must be an absolute same-path host/container bind. The loader copies
bind-mounted image collateral (Verilator/Yosys share, native Verilator runtime
and libraries, Nangate45, and the DRAMsim3 platform tree that simulators built in RTL
boxes link and load) into `tools/`, and points `CF_DRAM_ROOT` at `tools/platform`.
Native-only CACTI stays in `/opt/cf`. All temporary/build/cache bind sources are under the data root.
The evaluator must honor `CF_V2_ROOT`, `CF_ORCH_SSD`, `CF_ORCH_POSIX` and the
shared path configuration established by the loader before any imports.

Compose mounts a separate host secrets directory **only** at `/run/cf-secrets`.
`CF_EVALUATOR_SALT_FILE=/run/cf-secrets/evaluator-salt.bin` is generated once with
64 random bytes, fsynced and atomically published with mode **0600**. Standalone
default: a sibling `<root-name>-secrets/evaluator-salt.bin` outside `CF_V2_ROOT`.
To keep an existing salt (moving a validator, or comparing with earlier runs), place
it as `$CF_V2_SECRETS/evaluator-salt.bin` before the first install: a regular file of
at least 32 bytes, one link, mode 0600. An existing salt is never regenerated.
At start the loader refuses a data root on NTFS/FUSE/SMB/NFS-like filesystems (local ext4/xfs
only), chowns `CF_V2_ROOT` to its own root user and makes it and the secrets directory 0700 (the
salt 0600), and refuses to start if a mode does not take effect. Everything below the root keeps
box-readable modes and is unreachable to other local users through it; operators read results
through the gateway (`/v2/status/<id>`) and use `sudo` for logs and job directories.
Secrets must never appear in a miner box mount, asset, response or log. The
loader does not log request paths, evaluator output, salts or exception details.
A trusted evaluator must uphold the same rule in its own responses/logs.

## HTTP lifecycle

The gateway exposes `/v2/*`; the loader accepts these paths and equivalent
unprefixed paths for the existing proxy. Uploads require Content-Length; the
gateway forwards multipart bytes as a stream. The loader spools ZIP parts with
bounded memory (at most two concurrent uploads).

```sh
curl -F design_zip=@design.zip -F evaluator_zip=@evaluator.zip \
  -F evaluator_sig=@evaluator.zip.sig -F request_id=job-1 \
  http://localhost:8080/v2/evaluate
# Later requests may replace evaluator_zip/evaluator_sig with evaluator_sha256=<sha>.
curl http://localhost:8080/v2/status/job-1
```

`mode` defaults to `T2` and must be enabled in `CF_V2_ALLOWED_MODES`. The first
response is 202 with Location/status_url. Polling phases are
`installing_evaluator` → `queued` → `running` → terminal 200; short phases may be
missed between polls. Terminal bodies are unchanged, with `X-CF-Phase: done`.
The gateway rewrites the service-style `/status/<id>` Location to `/v2/status/<id>`.
Optional pre-warm: `POST /v2/evaluator` (also `/admin/install` on the loader),
then `GET /v2/evaluator/<sha>`; `GET /v2/evaluator` lists install state.
Pre-warming also starts the service when idle. Health is 503 until a verified
ready install and live service exist. `V2_ADMIN_TOKEN` protects pre-warm at the
gateway if configured. Automatic install through `/v2/evaluate` still requires
a valid publisher signature (or the recorded explicit unsigned override).

Installs are locked per SHA across threads/processes. Jobs waiting on a failed
attempt receive HTTP 200 `cf-response/1` with `status: ERROR`, `score: null`, and
retryable `problems[].fault: system`. No provisioning failure is a miner score.
A new request retries a failed install; an identical request ID may retry that
system error too. Successful terminal requests retain idempotent bytes. Changing
archive, design, mode or submitter under an existing ID returns 409.

## Minimal fixture compatibility

The provisional `cf-evaluator/1` manifest remains supported: flat `service` and
`preflight` paths (defaults `v2/deploy/service.py`, `v2/deploy/preflight.py`),
`policy` (default `evaluator/policy.json`), `assets` (default `assets.json`), and
`post_install` steps with `argv`, `outputs` (nonempty list of `path`/`sha256`) and
optional `timeout_s`. Its `cf-assets/1` document uses `url`, `sha256`, optional
`size`, and `target`, always materialized as a regular file. This compatibility
format is exercised with synthetic fixtures; C7 uses the envelope above.

## cfscope and tests

The image intentionally installs `cfscope` as `/usr/local/bin/systemd-run`.
Supported: `--user`, `--scope`, `-q`/`--quiet`, `--unit NAME`/`--unit=NAME`,
`-p KEY=VALUE`/`-pKEY=VALUE`/`--property=KEY=VALUE`, and `--`.
Properties: `MemoryMax`, `MemorySwapMax`, `OOMPolicy=continue`, `CPUQuota=<n>%`,
`AllowedCPUs=<list/ranges>`. Other options/properties exit 2 loudly. Memory units
use powers of 1024. Swap defaults to zero. There is no systemd bus/journal unit.

The entrypoint moves its container cgroup's processes into `service.scope`,
sets its 5 GiB cap (`CF_V2_SERVICE_MEMORY_MAX`), and enables memory/cpu/cpuset
controllers in the now-empty delegated parent. It never changes host ancestor
controllers or processes. Each command gets a sibling `cf-*.scope`, joins it,
then execs directly: PID, signal and exit semantics are preserved. A locked
reaper removes only empty scopes. `CF_SCOPE_BACKEND=cfscope` is available to the
evaluator for receipts; HTTP always records `X-CF-Scope-Backend: cfscope`.

```sh
python -m pytest v2-evaluator/tests gateway/tests/test_v2_proxy.py -q
```

Kernel tests skip outside a delegated container. Run them inside a running image
using the orchestrator venv's pytest and the test directory (read-only bind/copy).
They check the real cap, `memory.events` OOM counters, zero swap, CPU settings,
exec PID/exit/signals and safe empty-scope cleanup. Synthetic fixture generation:

```sh
python v2-evaluator/tests/fixtures.py /tmp/fake-evaluator.zip
```

The synthetic fixture is transport-only and cannot validate actual EDA/tool
parity, real policy compatibility or a full T2 evaluation. C7 and fresh-install
acceptance remain separate work.

An isolated signed container smoke uses prebuilt images, a unique compose project
and an ephemeral loopback gateway port. It starts only the evaluator and gateway,
mounts this checkout's gateway code read-only, and removes its own containers,
temporary signing key and data afterward:

```sh
python v2-evaluator/tests/container_smoke.py --work /var/tmp/c6-smoke
```
