# v2 evaluator container (C1 + C2 + C6)

Ubuntu 24.04 LTS, Linux amd64 only. This public context contains tools, Python
package locks and public Nangate45 collateral. The small evaluator zip carries
code, policy, manifests and private inputs. Public model and dataset assets are
listed there by URL + SHA256 and fetched automatically by the install loader; they are
not baked into this image. Preprocessed tensors are regenerated at install.

`entrypoint.sh` verifies VERSIONS.txt, establishes delegated cgroup-v2 nesting,
and starts the loader on port 18080 (compose-internal only). The loader accepts
`design_zip` plus `evaluator_zip`/`evaluator_sig` on the first request, or an
already cached `evaluator_sha256`. It installs asynchronously, then runs the
evaluator's loopback service. See [EVALUATOR_ZIP.md](EVALUATOR_ZIP.md) for the
archive/signature contract, lifecycle, configuration and tests.

Set `CF_V2_EVALUATOR_PUBLIC_KEY` to the publisher's OpenSSH Ed25519 public key.
Unsigned evaluators are refused unless `CF_V2_ALLOW_UNSIGNED=1` is explicitly set;
the override is recorded in every response's headers. Start the server from the repository
root with `docker compose up -d --build`.

On a shared laptop, build the images sequentially with the site's `cfjob` wrapper:

```sh
cfjob ./v2-compiler/build.sh
cfjob ./v2-evaluator/build.sh
docker run --rm --network none chipforge_eda_server-v2-evaluator:2.0 /opt/cf/smoke.sh
```

The build scripts check root-disk free space and use the legacy builder with
`--no-cache`; all compilation uses at most two jobs. For compose builds use
`COMPOSE_PARALLEL_LIMIT=1 DOCKER_BUILDKIT=0 docker compose build`.
Do not run a compose build concurrently with another image build.

`pins.env` is the reviewed pin list; the Dockerfile repeats only the FROM digests
(the build script checks agreement). Ubuntu packages come from the dated signed
snapshot; apt verifies metadata signatures and package SHA256s. HTTPS trust is
bootstrapped with a CA bundle from the compiler's public digest-pinned Python base;
the final image remains Ubuntu, which installs its own ca-certificates package. Git sources are
checked out by full commit, including Yosys's pinned submodules. Downloaded
archives and Python wheels require SHA256 verification. The Nangate45 manifest
covers every platform file, relative to the ORFS `flow/platforms` directory.

Python 3.13.3 is compiled from its verified upstream release archive because the
LTS base's default Python differs. Locks reproduce the component `pip freeze`
versions for CPython 3.13 on Linux amd64, without dependency re-resolution. The CPU
PyTorch wheels use the public CPU index; hash enforcement applies to that index
as well. `regenerate-locks.py` regenerates locks from explicitly provided component
venvs; review version changes and wheel URLs before committing.

Tool parity checks version and git SHA, as required by the lead decision. Yosys's
reported build compiler may differ from the laptop. DRAMsim3 checks both upstream
archive and patched controller hashes; the observer overlay is the same small
public-library integration patch used by the native backend. The shared library
can differ in bytes across build toolchains.

Compose requires privileged operation, host PID/cgroup namespaces and the Docker
socket for the evaluator. `CF_V2_ROOT` is mounted at exactly the same path
on host and container for sibling Docker bind mounts. Secrets have a separate
container destination `/run/cf-secrets`; they do not belong under the data root.
No running service needs to be restarted to build or smoke-test these images.

Installed tool paths:

| Tool | Image path |
| --- | --- |
| Component venv | `/opt/cf/venv/<component>` |
| Nangate45 | `/opt/cf/pdk/nangate45` |
| CACTI and tables | `/opt/cf/cacti/cacti`, `/opt/cf/cacti/tech_params` |
| DRAMsim3 | `/opt/cf/platform/DRAMsim3-29817593b3389f1337235d63cac515024ab8fd6e` |
| Backend pin | `/opt/cf/platform/backend-pin.json` |
| Verilator runtime headers | `/usr/local/share/verilator` |

The loader seeds collateral used in sibling-container bind mounts under
`CF_V2_ROOT`; a path existing only inside this image cannot be mounted by the
host Docker daemon (DRAMsim3 is copied to `tools/platform` for that reason).
Native-only tools remain at the image paths above.

## Box images (targets `rtl` and `sta`)

The same Dockerfile builds the two images the evaluator starts as no-network, read-only sibling
boxes. Compose builds them as services `cf-rtl` and `cf-sta` and passes their tags to the
evaluator as `CF_RTL_IMAGE` and `CF_STA_IMAGE`. Both are `FROM base`, so they share its layers.

| Image | Contents | Used for |
| --- | --- | --- |
| `cf-rtl:v2.0` | Ubuntu g++/make/python3 from the pinned snapshot; xPack `riscv-none-elf-gcc` `RISCV_GCC_VERSION` at `/opt/riscv` (reduced to the rv32imc/ilp32 multilib, binaries checked against `riscv-gcc.sha256`) | RTL tool box (Verilator/Yosys frontends), Verilator model compile, simulator box, host firmware build |
| `cf-sta:v2.0` | OpenSTA `OPENSTA_COMMIT` at `/build/bin/sta` (no CUDD, no tclreadline); python3 | post-synthesis STA, hierarchical STA, clock-domain STA |

Neither image carries Verilator or Yosys: the evaluator stages its own pinned `verilator_bin`,
Yosys and their runtime libraries into each box, so the tool version is always this image's.
Neither carries a PDK: the Nangate45 views are bind-mounted from `CF_V2_ROOT/tools/nangate45`.
`riscv-gcc.sha256` must equal `../v2-compiler/target-gcc.sha256` (the miner compiler box and the
firmware build use byte-identical target toolchains); `tests/test_tool_images.py` checks it.
OpenROAD is not built: v2.0 runs no placement or routing (the EN4 power path that would use it is
recorded as UNRESOLVED and is display-only). A host that wants it supplies an image and sets
`CF_OPENROAD_IMAGE`.

Each box image's `/opt/cf/VERSIONS.txt` records its tool versions; the build fails if the
OpenSTA version or embedded git SHA, or a toolchain hash, does not match the pins.

## Install failures

A failed install returns only a stable code (for example `POST_INSTALL_FAILED`); step output can
contain private evaluator content and is never sent to callers. For operators, the last 200 lines
of each failing `post_install` or preflight step are kept in
`$CF_V2_ROOT/logs/install/<sha>.<step>.log` (mode 0600). The container log prints the log's path
(`install failed: ... log=...`) and `GET /health` lists it under `install_failures`; neither
contains the output itself.
