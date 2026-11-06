"""Old evaluator versions are deleted once nothing needs them (state, escrow and the installed tree; the
archive stays so a version asked for again is reinstalled)."""
import json

from fixtures import RAW, build_zip
from test_runtime import running, terminal, wait_until


def evaluate(request, archive, rid):
    assert request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id=rid))[0] == 202
    status, headers, body = wait_until(lambda: terminal(request, rid), timeout=30)
    assert body == RAW, body
    return headers


def trees(root, sha):
    return [d for d in ('state', 'escrow', 'evaluators') if (root / d / sha).exists()]


def test_old_version_goes_once_a_newer_one_runs(tmp_path):
    a, b = tmp_path / 'a.zip', tmp_path / 'b.zip'
    old, new = build_zip(a, delay=0.1), build_zip(b, delay=0.1, extra={'v.txt': 'b'})
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        evaluate(request, a, 'one')
        (runtime.root / 'state' / old / 'runs').mkdir(exist_ok=True)
        (runtime.root / 'state' / old / 'runs' / 'big.bin').write_bytes(b'x' * 4096)
        evaluate(request, b, 'two')
        wait_until(lambda: runtime.service.sha == new)
        done = runtime.prune(wait=True) or runtime.last_cleanup
        assert done and done['evaluators'] == [old]
        assert trees(runtime.root, old) == [] and trees(runtime.root, new) == ['state', 'escrow', 'evaluators']
        assert (runtime.root / 'archives' / (old + '.zip')).is_file()          # kept for a reinstall
        health = json.loads(request('GET', '/health')[2])
        assert health['last_cleanup']['evaluators'] == [old] and health['disk']['free_gib'] > 0
        assert health['disk']['evaluator_versions'] == 1
        # asked for again (a late re-run on the old version): reinstalled from its archive, and it runs
        assert request('POST', '/evaluate', dict(design_zip=b'd', evaluator_sha256=old, request_id='late'))[0] == 202
        assert wait_until(lambda: terminal(request, 'late'), timeout=30)[2] == RAW
        assert runtime.installer.ready(old)


def test_a_version_with_pending_work_is_kept(tmp_path):
    a, b = tmp_path / 'a.zip', tmp_path / 'b.zip'
    old, new = build_zip(a, delay=3), build_zip(b, delay=0.1, extra={'v.txt': 'b'})
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=a, request_id='long'))
        wait_until(lambda: runtime.job('long')['forwarded'])
        assert request('POST', '/evaluate', dict(design_zip=b'e', evaluator_zip=b, request_id='next'))[0] == 202
        wait_until(lambda: runtime.installer.ready(new), timeout=30)
        runtime.prune(wait=True)
        assert trees(runtime.root, old) == ['state', 'escrow', 'evaluators']   # still running a job
        assert wait_until(lambda: terminal(request, 'long'), timeout=30)[2] == RAW
        assert wait_until(lambda: terminal(request, 'next'), timeout=30)[2] == RAW
        wait_until(lambda: runtime.service.sha == new)
        runtime.prune(wait=True)
        assert trees(runtime.root, old) == []


def test_leftovers_are_cleared_before_a_new_install_checks_the_disk(tmp_path):
    a = tmp_path / 'a.zip'
    sha = build_zip(a, delay=0.1)
    stale = 'f' * 64
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        for d in ('state', 'escrow'):
            (runtime.root / d / stale / 'runs').mkdir(parents=True)
        evaluate(request, a, 'first')
        assert trees(runtime.root, stale) == []
        assert trees(runtime.root, sha) == ['state', 'escrow', 'evaluators']


def test_cleanup_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv('CF_V2_KEEP_OLD_EVALUATORS', '1')
    a, b = tmp_path / 'a.zip', tmp_path / 'b.zip'
    old, new = build_zip(a, delay=0.1), build_zip(b, delay=0.1, extra={'v.txt': 'b'})
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        evaluate(request, a, 'one')
        evaluate(request, b, 'two')
        wait_until(lambda: runtime.service.sha == new)
        assert runtime.prune(wait=True) is None and trees(runtime.root, old) == ['state', 'escrow', 'evaluators']


def test_cleanup_never_follows_symlinks_out(tmp_path):
    from loader.runtime import _remove
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep.txt').write_text('keep')
    tree = tmp_path / 'tree'
    (tree / 'a').mkdir(parents=True)
    (tree / 'a' / 'link').symlink_to(outside)
    (tree / 'a').chmod(0o555)
    _remove(tree)
    assert not tree.exists() and (outside / 'keep.txt').read_text() == 'keep'
