# v2 evaluator tools image (C1)

Ubuntu 24.04 LTS, Linux amd64 only. This public context contains tools, Python
package locks and public Nangate45 collateral. The small evaluator zip carries
code, policy, manifests and private inputs. Public model and dataset assets are
listed there by URL + SHA256 and fetched by the later install loader; they are
not baked into this image. Preprocessed tensors are regenerated at install.

`entrypoint.sh` is a **placeholder**: verify/print VERSIONS.txt and sleep. It does
not serve HTTP, load a zip or establish cgroup nesting. C2 supplies the cgroup
scope implementation; C6 supplies installation/supervision. Port 18080 is reserved.

On a shared laptop, build the images sequentially with the site's `cfjob` wrapper:

```sh
cfjob ./v2-compiler/build.sh
cfjob ./v2-evaluator/build.sh
docker run --rm --network none chipforge_eda_server-v2-evaluator:2.0 /opt/cf/smoke.sh
```

The build scripts check root-disk free space and use the legacy builder with
`--no-cache`; all compilation uses at most two jobs. For compose builds use
`COMPOSE_PARALLEL_LIMIT=1 DOCKER_BUILDKIT=0 docker compose --profile v2 build`.
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
socket for the later evaluator. `CF_V2_ROOT` is mounted at exactly the same path
on host and container for sibling Docker bind mounts. Secrets have a separate
container destination `/run/cf-secrets`; they do not belong under the data root.
No running service needs to be restarted to build or smoke-test these images.

Installed tool paths for the later loader/path integration:

| Tool | Image path |
| --- | --- |
| Component venv | `/opt/cf/venv/<component>` |
| Nangate45 | `/opt/cf/pdk/nangate45` |
| CACTI and tables | `/opt/cf/cacti/cacti`, `/opt/cf/cacti/tech_params` |
| DRAMsim3 | `/opt/cf/platform/DRAMsim3-29817593b3389f1337235d63cac515024ab8fd6e` |
| Backend pin | `/opt/cf/platform/backend-pin.json` |
| Verilator runtime headers | `/usr/local/share/verilator` |

C4/C6 must seed any collateral used in sibling-container bind mounts under
`CF_V2_ROOT`; a path existing only inside this image cannot be mounted by the
host Docker daemon. This C1 placeholder does not perform that installation.
