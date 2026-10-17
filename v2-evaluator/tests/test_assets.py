import hashlib
import io
from pathlib import Path
import tarfile
import zipfile

import pytest

from loader.assets import extract_tar, materialize
from loader.security import InstallError


@pytest.mark.parametrize('kind', ['zip', 'tar.gz'])
def test_public_archives_materialized_and_reused_without_overwrite(tmp_path, kind):
    root = tmp_path / 'root'
    (root / 'tmp').mkdir(parents=True)
    archive = tmp_path / 'public-archive'
    data = b'synthetic public archive member'
    if kind == 'zip':
        with zipfile.ZipFile(archive, 'w') as z: z.writestr('folder/member', data)
    else:
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo('./folder/member'); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    dest = root / 'data/unpacked'
    materialize(root, archive, dest, kind)
    assert (dest / 'folder/member').read_bytes() == data
    materialize(root, archive, dest, kind)
    (dest / 'folder/member').chmod(0o644)
    (dest / 'folder/member').write_bytes(b'changed')
    with pytest.raises(InstallError, match='ASSET_TARGET_CONFLICT'):
        materialize(root, archive, dest, kind)


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                                    ('../link', tarfile.SYMTYPE), ('/hard', tarfile.LNKTYPE),
                                    ('device', tarfile.CHRTYPE), ('fifo', tarfile.FIFOTYPE)])
def test_public_tar_traversal_devices_refused(tmp_path, name, kind):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo(name); info.type = kind
        info.linkname = '../escape'
        tar.addfile(info)
    target = tmp_path / 'tree'; target.mkdir()
    with pytest.raises(InstallError): extract_tar(archive, target)
    assert not list(target.iterdir())


def _link_tar(archive):
    data = b'regular member'
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('pkg/real.md'); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        for name, kind, target in (('pkg/docs/alias.md', tarfile.SYMTYPE, '../real.md'),
                                   ('pkg/docs/escape', tarfile.SYMTYPE, '../../../../etc/passwd'),
                                   ('pkg/hard.md', tarfile.LNKTYPE, 'pkg/real.md')):
            info = tarfile.TarInfo(name); info.type = kind; info.linkname = target
            tar.addfile(info)
    return data


def test_public_tar_links_skipped_never_created(tmp_path):
    # Upstream source tarballs (e.g. a GitHub tag archive) carry symlinks; they are left out.
    archive = tmp_path / 'upstream.tar.gz'
    data = _link_tar(archive)
    target = tmp_path / 'tree'; target.mkdir()
    extract_tar(archive, target)
    assert (target / 'pkg/real.md').read_bytes() == data
    for name in ('pkg/docs/alias.md', 'pkg/docs/escape', 'pkg/hard.md'):
        assert not (target / name).exists() and not (target / name).is_symlink()


def test_public_zip_symlink_skipped_never_created(tmp_path):
    root = tmp_path / 'root'; (root / 'tmp').mkdir(parents=True)
    archive = tmp_path / 'upstream.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('pkg/real.md', b'regular member')
        link = zipfile.ZipInfo('pkg/alias.md'); link.external_attr = (0o120777 << 16)
        z.writestr(link, '../../../etc/passwd')
    materialize(root, archive, root / 'data/unpacked', 'zip')
    assert (root / 'data/unpacked/pkg/real.md').read_bytes() == b'regular member'
    assert not (root / 'data/unpacked/pkg/alias.md').exists()
    assert not (root / 'data/unpacked/pkg/alias.md').is_symlink()


def test_public_tar_size_limit(tmp_path):
    archive = tmp_path / 'big.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('big'); info.size = 1024
        tar.addfile(info, io.BytesIO(b'x'*1024))
    target = tmp_path / 'tree'; target.mkdir()
    with pytest.raises(InstallError, match='ASSET_TOO_LARGE'):
        extract_tar(archive, target, max_bytes=100)


def test_public_archive_target_symlink_refused(tmp_path):
    root = tmp_path / 'root'; (root/'tmp').mkdir(parents=True)
    archive = tmp_path/'a.zip'
    with zipfile.ZipFile(archive,'w') as z: z.writestr('file', b'data')
    (root/'data').symlink_to(tmp_path)
    with pytest.raises(InstallError, match='UNSAFE_PATH'):
        materialize(root, archive, root/'data/escape', 'zip')
    assert not (tmp_path/'escape').exists()
