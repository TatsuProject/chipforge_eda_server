"""The v2-only compose file and the cf-rtl / cf-sta box images (source checks; no Docker needed)."""
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]          # v2-evaluator/
REPO = HERE.parent
pytestmark = pytest.mark.skipif(not (REPO / 'docker-compose.yml').is_file(),
                                reason='source checkout only (not inside the image)')


def pins():
    out = {}
    for line in (HERE / 'pins.env').read_text().splitlines():
        if line and not line.startswith('#'):
            k, _, v = line.partition('=')
            out[k] = v
    return out


def compose():
    yaml = pytest.importorskip('yaml')
    return yaml.safe_load((REPO / 'docker-compose.yml').read_text())


def test_firmware_toolchain_hashes_equal_compiler_box():
    assert (HERE / 'riscv-gcc.sha256').read_bytes() == (REPO / 'v2-compiler/target-gcc.sha256').read_bytes()


def test_toolchain_pin_equals_compiler_box_pin():
    text = (REPO / 'v2-compiler/Dockerfile').read_text()
    p = pins()
    assert f"ARG XPACK_VERSION={p['RISCV_GCC_VERSION']}" in text
    assert f"ARG XPACK_SHA256={p['RISCV_GCC_SHA256']}" in text


def test_box_stages_pinned_and_in_build_context():
    p = pins()
    assert re.fullmatch(r'[0-9a-f]{40}', p['OPENSTA_COMMIT']) and p['OPENSTA_VERSION']
    docker = (HERE / 'Dockerfile').read_text()
    for stage in ('FROM base AS rtl', 'FROM base AS opensta', 'FROM base AS sta'):
        assert stage in docker
    assert '!riscv-gcc.sha256' in (HERE / '.dockerignore').read_text().splitlines()
    for word in ('volare', 'sky130', 'openlane'):
        assert word not in docker.lower()


def test_compose_is_v2_only():
    services = compose()['services']
    assert set(services) == {'eda-gateway', 'cf-compiler', 'cf-rtl', 'cf-sta', 'v2-evaluator'}
    assert not any('profiles' in s for s in services.values())
    text = (REPO / 'docker-compose.yml').read_text().lower()
    for word in ('verilator-api', 'openlane-api', 'volare', 'sky130_', ':8001', ':8003', 'pdk_root'):
        assert word not in text
    assert not (REPO / 'verilator-api').exists() and not (REPO / 'openlane-api').exists()


def test_evaluator_gets_the_box_images_compose_builds():
    services = compose()['services']
    env = dict(e.split('=', 1) for e in services['v2-evaluator']['environment'])
    assert env['CF_RTL_IMAGE'] == services['cf-rtl']['image']
    assert env['CF_STA_IMAGE'] == services['cf-sta']['image']
    assert env['CF_COMPILER_IMAGE'] == services['cf-compiler']['image']
    assert 'CF_OPENLANE_IMAGE' not in env
    assert services['cf-rtl']['build']['target'] == 'rtl' and services['cf-sta']['build']['target'] == 'sta'
    for name in ('cf-compiler', 'cf-rtl', 'cf-sta', 'v2-evaluator'):
        # the published pinned images, pulled + verified by `make images`; never built or pulled by compose
        assert services[name]['pull_policy'] == 'never'
    for name in ('cf-compiler', 'cf-rtl', 'cf-sta'):
        assert services['v2-evaluator']['depends_on'][name] == {'condition': 'service_completed_successfully'}


def test_images_lock_pins_every_compose_image_by_digest():
    """`make start` pulls images.lock by immutable digest: one line per pinned compose image, digest refs only."""
    import re
    services = compose()['services']
    want = {services[n]['image'] for n in ('cf-compiler', 'cf-rtl', 'cf-sta', 'v2-evaluator')}
    rows = [line.split() for line in (REPO / 'images.lock').read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    assert {r[0] for r in rows} == want and len(rows) == len(want)
    assert all(len(r) == 2 and re.fullmatch(r'docker\.io/logicx101/chipforge-v2-[a-z]+@sha256:[0-9a-f]{64}', r[1]) for r in rows)
