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
                                    ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE)])
def test_public_tar_traversal_links_devices_refused(tmp_path, name, kind):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo(name); info.type = kind
        info.linkname = '../escape'
        tar.addfile(info)
    target = tmp_path / 'tree'; target.mkdir()
    with pytest.raises(InstallError): extract_tar(archive, target)
    assert not list(target.iterdir())


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
