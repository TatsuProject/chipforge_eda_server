# Locally built cf-compiler v2.1

This context ports the pinned public tool recipe used by cf-compiler:v2.1. It
contains no evaluator code. Build with `cfjob ./v2-compiler/build.sh` (or a serial
compose build). The default tag is `cf-compiler:v2.1-public`, preserving any existing
local `cf-compiler:v2.1` image.

Inputs: the pinned Python/Debian base, dated Debian snapshot, exact LLVM/GCC
versions, SHA256-verified Rust/xPack archives and hash-locked Python wheels.
The two IREE C API headers are public upstream sources; `iree-dev/SOURCE.json`
records their upstream commit and hashes. Their contents are unchanged.

The final build compares VERSIONS.txt to `expected-versions.txt` from v2.1 and
verifies four target-toolchain binaries against `target-gcc.sha256`. It records
`/opt/cf/CONTENT.sha256` over those two manifests. This is the content pin; image
IDs are diagnostic only and are not expected to reproduce. The expected version
report preserves the original raw output, including trailing spaces on freeze
lines. Version or target binary changes require reviewing and updating the
committed content pin.

The expected versions include import and frontend availability probes. The
original recipe's network-disabled box environment and tool paths are preserved.

To check an already built image, mount the committed content pin read-only or
compare its bytes to `/opt/cf/CONTENT.sha256`, then run both
`sha256sum -c CONTENT.sha256` and `sha256sum -c target-gcc.sha256` from `/opt/cf`.
Rerun `/opt/cf/versions.sh` and compare it to VERSIONS.txt when checking tools.

The pinned header source is
https://github.com/iree-org/iree/tree/e4a3b0405d7d23554da26403658d0e8c3c5ecf25/compiler/bindings/c/iree/compiler
(`api_support.h` and `embedding_api.h`); both hashes were checked against those
public upstream bytes during the port.
