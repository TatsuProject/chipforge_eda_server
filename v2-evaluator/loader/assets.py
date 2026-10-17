"""Guarded publication of verified files and public archive assets under the same-path root."""
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

from .security import InstallError, digest, extract, file_lock, inside, relative, remove_tree, sync_dir


def extract_tar(archive, target, max_bytes=16 << 30, max_files=200000):
    count, total, seen = 0, 0, set()
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar:
                count += 1
                if count > max_files:
                    raise InstallError('ASSET_TOO_MANY_FILES')
                name = member.name
                while name.startswith('./'):
                    name = name[2:]
                if not name and member.isdir():
                    continue
                p = relative(name.rstrip('/'))
                if member.issym() or member.islnk():
                    # Upstream source tarballs carry links (e.g. docs aliases). A link is never
                    # created and never followed; its name was still checked above.
                    continue
                if p in seen or not (member.isfile() or member.isdir()):
                    raise InstallError('UNSAFE_ASSET_ARCHIVE')
                seen.add(p)
                total += member.size
                if member.size < 0 or total > max_bytes:
                    raise InstallError('ASSET_TOO_LARGE')
                dest = target / p
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, dest.open('xb') as out:
                    size = 0
                    while data := src.read(1 << 20):
                        size += len(data)
                        if size > member.size:
                            raise InstallError('ASSET_TOO_LARGE')
                        out.write(data)
                    if size != member.size:
                        raise InstallError('UNSAFE_ASSET_ARCHIVE')
                    out.flush()
                    os.fsync(out.fileno())
                dest.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, FileExistsError, IsADirectoryError, EOFError):
        raise InstallError('UNSAFE_ASSET_ARCHIVE') from None


def publish_file(root, source, dest):
    # Caller holds assets-materialize.lock. Recheck parents after creating them.
    dest = inside(root, dest.relative_to(root).as_posix())
    dest.parent.mkdir(parents=True, exist_ok=True)
    want = digest(source)
    if dest.exists():
        if not dest.is_file() or digest(dest) != want:
            raise InstallError('ASSET_TARGET_CONFLICT')
        return
    fd, name = tempfile.mkstemp(dir=dest.parent)
    try:
        with os.fdopen(fd, 'wb') as out, source.open('rb') as src:
            shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o555 if source.stat().st_mode & 0o111 else 0o444)
        os.replace(name, dest)
        sync_dir(dest.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def materialize(root, cached, dest, unpack):
    with file_lock(root / 'locks/assets-materialize.lock'):
        if unpack == 'none':
            publish_file(root, cached, dest)
            return
        temp = Path(tempfile.mkdtemp(prefix='asset-', dir=root / 'tmp'))
        try:
            if unpack == 'zip':
                extract(cached, temp, max_bytes=16 << 30, max_files=200000, skip_links=True)
            elif unpack == 'tar.gz':
                extract_tar(cached, temp)
            else:
                raise InstallError('INVALID_ASSET_UNPACK')
            # Several archives may share a destination. Merge only identical existing files;
            # never overwrite different bytes or follow a pre-existing target symlink.
            for source in sorted(temp.rglob('*')):
                target = inside(root, (dest / source.relative_to(temp)).relative_to(root).as_posix())
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    publish_file(root, source, target)
        finally:
            remove_tree(temp)
