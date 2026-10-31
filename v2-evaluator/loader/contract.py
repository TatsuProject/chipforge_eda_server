"""Public archive metadata interfaces; no evaluator implementation or asset inventory."""
from .security import InstallError, SHA, digest, inside


def manifest(raw, tree):
    if not isinstance(raw, dict):
        raise InstallError('INVALID_EVALUATOR_MANIFEST')
    if raw.get('schema') == 'cf-evaluator/1':
        if set(raw) - {'schema', 'service', 'preflight', 'policy', 'assets', 'post_install'}:
            raise InstallError('UNKNOWN_EVALUATOR_MANIFEST_FIELD')
        return dict(raw)
    if raw.get('schema') != 'cf-evaluator-zip/1':
        raise InstallError('INVALID_EVALUATOR_MANIFEST')
    allowed = {'schema', 'version', 'created', 'git_commit', 'source_dirty', 'entrypoint',
               'files', 'assets', 'post_install', 'python', 'policy'}
    if set(raw) - allowed:
        raise InstallError('UNKNOWN_EVALUATOR_MANIFEST_FIELD')
    entry, files = raw.get('entrypoint'), raw.get('files')
    if not isinstance(entry, dict) or set(entry) != {'service', 'preflight'} or not isinstance(files, dict):
        raise InstallError('INVALID_EVALUATOR_MANIFEST')
    actual = {p.relative_to(tree).as_posix() for p in tree.rglob('*') if p.is_file()} - {'MANIFEST.json'}
    if set(files) != actual:
        raise InstallError('EVALUATOR_FILE_INVENTORY_MISMATCH')
    for name, want in files.items():
        if not isinstance(want, str) or not SHA.fullmatch(want) or digest(inside(tree, name)) != want:
            raise InstallError('EVALUATOR_FILE_HASH_MISMATCH')
    steps = raw.get('post_install', [])
    if not isinstance(steps, list) or len(steps) > 64:
        raise InstallError('INVALID_POST_INSTALL')
    normalized = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {'id', 'argv', 'produces', 'timeout_s'}:
            raise InstallError('INVALID_POST_INSTALL')
        outputs = step.get('produces')
        if not isinstance(outputs, list):
            raise InstallError('INVALID_POST_INSTALL')
        clean = []
        for output in outputs:
            if not isinstance(output, dict) or set(output) - {'path', 'sha256', 'bytes'} or not {'path', 'sha256'} <= set(output):
                raise InstallError('INVALID_POST_INSTALL_OUTPUT')
            clean.append({'path': output['path'], 'sha256': output['sha256']})
        normalized.append(dict(argv=step.get('argv'), outputs=clean,
                               timeout_s=step.get('timeout_s', 7200)))
    return dict(schema=raw['schema'], service=entry['service'], preflight=entry['preflight'],
                policy=raw.get('policy', 'policy/policy.json'), assets=raw.get('assets', 'assets.json'),
                post_install=normalized)


def assets(raw):
    if not isinstance(raw, dict) or raw.get('schema') not in ('cf-assets/1', 'cf-evaluator-assets/1') or not isinstance(raw.get('assets'), list):
        raise InstallError('INVALID_ASSET_MANIFEST')
    normalized = []
    for asset in raw['assets']:
        if not isinstance(asset, dict):
            raise InstallError('INVALID_ASSET_MANIFEST')
        if raw['schema'] == 'cf-assets/1':
            if set(asset) - {'url', 'sha256', 'size', 'target'}:
                raise InstallError('INVALID_ASSET_MANIFEST')
            normalized.append(dict(asset, unpack='none'))
        else:
            if set(asset) - {'id', 'url', 'sha256', 'bytes', 'dest', 'unpack', 'licence'}:
                raise InstallError('INVALID_ASSET_MANIFEST')
            if asset.get('unpack', 'none') not in ('none', 'zip', 'tar.gz'):
                raise InstallError('INVALID_ASSET_UNPACK')
            normalized.append(dict(url=asset.get('url'), sha256=asset.get('sha256'), size=asset.get('bytes'),
                                   target=asset.get('dest'), unpack=asset.get('unpack', 'none')))
    return normalized
