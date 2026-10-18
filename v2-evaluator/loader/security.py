"""Verification, guarded extraction and content-addressed public asset downloads."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile

SHA = re.compile(r'[0-9a-f]{64}\Z')


class InstallError(Exception):
    """Only the stable code may leave the loader; exception text can contain private paths."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while data := f.read(1 << 20):
            h.update(data)
    return h.hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, obj):
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    sync_dir(path.parent)


class PrivateModeError(RuntimeError):
    """A private location still admits group/other access after the loader tried to close it."""


def owner_only(path, mode, *, label=None):
    """Close `path` to group and other (a directory 0700, a file 0600) and verify it took effect.

    Only the TOP of a private tree is closed: what lies below is then unreachable whatever its own
    modes, so the files the uid-65534 boxes must read can stay 0644/0755. The boxes never traverse
    these host parents: Docker bind-mounts each input by path as root, and inside the box only the
    mount point's own mode applies. `label` replaces the path in errors (the secrets location is
    never printed)."""
    path = Path(path)
    current = stat.S_IMODE(path.stat().st_mode)
    if current != mode:
        try:
            os.chmod(path, mode)
        except OSError:
            pass     # A read-only mount that is already private is fine; anything else fails below.
        current = stat.S_IMODE(path.stat().st_mode)
    if current & 0o077:
        raise PrivateModeError(f'{label or path} is mode {current:04o}; it must admit no group/other access '
                               f'(chmod {mode:o} failed or was ignored: use a local POSIX filesystem owned by the '
                               'evaluator user)')
    return current


# Filesystems the data root must not live on: they ignore or emulate POSIX modes, or their flock
# (the simulate box permit, the queue and install locks) is not reliable across processes.
NON_POSIX_FS = frozenset({'fuseblk', 'ntfs', 'ntfs3', 'vfat', 'msdos', 'exfat', 'cifs', 'smb3', 'smbfs',
                          'nfs', 'nfs4', '9p', 'vboxsf', 'virtiofs', 'sshfs'})


def filesystem_type(path, mountinfo='/proc/self/mountinfo'):
    """-> the filesystem type of the mount holding `path` (longest mount-point prefix), or None."""
    target, best, kind = str(Path(path).resolve()), -1, None
    try:
        lines = Path(mountinfo).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        left, sep, right = line.partition(' - ')
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        point = fields[4].replace('\\040', ' ')
        if (target == point or target.startswith(point.rstrip('/') + '/')) and len(point) > best:
            best, kind = len(point), right.split()[0]
    return kind


def local_posix(root, mountinfo='/proc/self/mountinfo'):
    """Start-up: refuse a data root on NTFS/FUSE/SMB/NFS-like filesystems, even ones mounted to accept chmod."""
    kind = filesystem_type(root, mountinfo)
    if kind and (kind in NON_POSIX_FS or kind.startswith('fuse')):
        raise PrivateModeError(f'{root} is on a {kind} filesystem; the data root must be on a local POSIX '
                               'filesystem (ext4 or xfs): owner-only modes and file locks are not reliable there')
    return kind


def private_locations(root, salt):
    """Start-up: the data root and the secrets directory are owner-only (0700), an existing salt 0600.

    The data root holds hidden evaluation inputs, private tensors, escrow, miner uploads and the
    queue databases; the evaluator's umask leaves the files below it box-readable (0644/0755), so
    this one mode is what keeps them from other local users. 0711 is not needed: no process other
    than the evaluator (root here) and the Docker daemon resolves paths through it.

    Running as root, the loader also takes ownership of a root the operator created: an evaluator's
    preflight probes the bind sources from a box as uid 0 with every capability dropped, which is
    "other" to an operator-owned 0700 directory. The operator inspects it with sudo."""
    root, salt = Path(root), Path(salt)
    local_posix(root)
    euid = os.geteuid()
    if euid == 0 and root.stat().st_uid != euid:
        try:
            os.chown(root, euid, os.getegid())
        except OSError as exc:
            raise PrivateModeError(f'{root} could not be made owned by the evaluator user: {exc.strerror}') from None
    owner_only(root, 0o700)
    salt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner_only(salt.parent, 0o700, label='the evaluator secrets directory')
    if salt.is_file() and not salt.is_symlink():
        owner_only(salt, 0o600, label='the evaluator salt')


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise InstallError('UNSAFE_PATH')
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ('', '.', '..') for x in value.split('/')) or ':' in value:
        raise InstallError('UNSAFE_PATH')
    return Path(*p.parts)


def inside(root, value):
    p = root / relative(value)
    # Also refuse pre-existing symlink parents (asset targets can already exist).
    if not p.resolve().is_relative_to(root.resolve()) or any(x.is_symlink() for x in (p, *p.parents) if x != root.parent):
        raise InstallError('UNSAFE_PATH')
    return p


def verify(archive, sha, signature, public_key, allow_unsigned):
    if not SHA.fullmatch(sha) or digest(archive) != sha:
        raise InstallError('EVALUATOR_HASH_MISMATCH')
    if not signature:
        if not allow_unsigned:
            raise InstallError('EVALUATOR_SIGNATURE_REQUIRED')
        return True
    if not public_key or not public_key.startswith('ssh-ed25519 ') or '\n' in public_key.strip():
        raise InstallError('EVALUATOR_PUBLIC_KEY_REQUIRED')
    # OpenSSH's detached Ed25519 signature format; the namespace prevents reuse in another protocol.
    with tempfile.TemporaryDirectory() as d:
        allowed = Path(d) / 'allowed_signers'
        sig = Path(d) / 'signature'
        allowed.write_text('evaluator ' + public_key.strip() + '\n')
        sig.write_bytes(signature)
        with archive.open('rb') as f:
            p = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(allowed), '-I', 'evaluator',
                                '-n', 'chipforge-evaluator', '-s', str(sig)], stdin=f,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if p.returncode:
            raise InstallError('EVALUATOR_SIGNATURE_INVALID')
    return False


def extract(archive, target, max_bytes=512 << 20, max_files=20000, skip_links=False):
    """No links, devices, encryption, duplicate names, absolute paths or zip bombs.

    skip_links (public asset archives only): symlink entries are left out instead of refused;
    a link is never created or followed. The evaluator archive itself always refuses links."""
    try:
        with zipfile.ZipFile(archive) as z:
            entries, total, names, links = z.infolist(), 0, set(), set()
            if len(entries) > max_files:
                raise InstallError('EVALUATOR_TOO_MANY_FILES')
            for entry in entries:
                if entry.orig_filename != entry.filename:
                    raise InstallError('UNSAFE_ARCHIVE')
                raw = entry.filename.rstrip('/') if entry.is_dir() else entry.filename
                p = relative(raw)
                if str(p) in names or p.parts[0].startswith('.cf-'):
                    raise InstallError('UNSAFE_ARCHIVE')
                names.add(str(p))
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if skip_links and kind == stat.S_IFLNK and not entry.flag_bits & 1:
                    links.add(entry.filename)
                    continue
                if kind not in (0, stat.S_IFDIR, stat.S_IFREG) or entry.flag_bits & 1:
                    raise InstallError('UNSAFE_ARCHIVE')
                total += entry.file_size
                if entry.file_size > max_bytes or total > max_bytes:
                    raise InstallError('EVALUATOR_TOO_LARGE')
            for entry in entries:
                if entry.filename in links:
                    continue
                p = target / relative(entry.filename.rstrip('/'))
                if entry.is_dir():
                    p.mkdir(parents=True, exist_ok=True)
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as source, p.open('xb') as dest:
                    count = 0
                    while data := source.read(1 << 20):
                        count += len(data)
                        if count > entry.file_size:
                            raise InstallError('EVALUATOR_TOO_LARGE')
                        dest.write(data)
                    dest.flush()
                    os.fsync(dest.fileno())
                p.chmod(0o755 if (entry.external_attr >> 16) & 0o111 else 0o644)
            for p in sorted((p for p in target.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                sync_dir(p)
            sync_dir(target)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, FileExistsError, IsADirectoryError):
        raise InstallError('UNSAFE_ARCHIVE') from None


def read_json(path):
    try:
        if path.stat().st_size > 4 << 20:
            raise ValueError('oversized metadata')
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise InstallError('INVALID_EVALUATOR_METADATA') from None


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.fragment:
        raise InstallError('INVALID_ASSET_URL')
    # Manifests are signed evaluator code, not miner input. HTTP permits local fixture servers.


def fetch_asset(cache, asset, max_bytes=16 << 30):
    sha, url = asset.get('sha256', ''), asset.get('url', '')
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise InstallError('INVALID_ASSET_HASH')
    check_url(url)
    size = asset.get('size')
    if size is not None and (type(size) is not int or size < 0 or size > max_bytes):
        raise InstallError('INVALID_ASSET_SIZE')
    limit = size if size is not None else max_bytes
    cache.mkdir(parents=True, exist_ok=True)
    dest, partial = cache / sha, cache / (sha + '.part')
    with file_lock(cache / (sha + '.lock')):
        if dest.exists():
            if digest(dest) == sha and (size is None or dest.stat().st_size == size):
                return dest
            dest.unlink()
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > limit or (partial.exists() and offset == limit and digest(partial) != sha):
            partial.unlink()
            offset = 0
        if not partial.exists() or digest(partial) != sha or (size is not None and offset != size):
            headers = {'Range': f'bytes={offset}-', 'Accept-Encoding': 'identity'} if offset else {'Accept-Encoding': 'identity'}
            opener = urllib.request.build_opener(PublicRedirect())
            try:
                with opener.open(urllib.request.Request(url, headers=headers), timeout=120) as response:
                    if response.status == 206:
                        cr = response.headers.get('Content-Range', '')
                        if not re.fullmatch(rf'bytes {offset}-\d+/\d+', cr):
                            raise InstallError('INVALID_ASSET_RANGE')
                    elif response.status == 200:
                        offset = 0  # Server ignored Range: replace, never append a full response.
                    else:
                        raise InstallError('ASSET_FETCH_FAILED')
                    with partial.open('ab' if offset else 'wb') as f:
                        count = offset
                        while data := response.read(1 << 20):
                            count += len(data)
                            if count > limit:
                                raise InstallError('ASSET_TOO_LARGE')
                            f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
            except InstallError:
                partial.unlink(missing_ok=True)
                raise
            except OSError:
                # Keep only an incomplete prefix; next attempt may resume it.
                raise InstallError('ASSET_FETCH_FAILED') from None
        if digest(partial) != sha or (size is not None and partial.stat().st_size != size):
            partial.unlink(missing_ok=True)
            raise InstallError('ASSET_HASH_MISMATCH')
        partial.chmod(0o444)
        os.replace(partial, dest)
        sync_dir(cache)
        return dest


def salt_once(path, root):
    """A separate mount in compose. Never emit bytes, or even the secret's path."""
    path = Path(path)
    if not path.is_absolute() or path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise InstallError('UNSAFE_SALT_LOCATION')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        owner_only(path.parent, 0o700)
    except PrivateModeError:
        raise InstallError('UNSAFE_SALT_LOCATION') from None
    with file_lock(path.parent / '.salt.lock'):
        if not path.exists():
            # Publish a fully fsynced file atomically; a crash cannot leave a short salt.
            fd, name = tempfile.mkstemp(prefix='.salt-', dir=path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'wb') as f:
                    f.write(os.urandom(64))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(name, path)
                sync_dir(path.parent)
            finally:
                Path(name).unlink(missing_ok=True)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size < 32 or info.st_nlink != 1:
            raise InstallError('INVALID_SALT_FILE')
        try:
            owner_only(path, 0o600)
        except PrivateModeError:
            raise InstallError('INVALID_SALT_FILE') from None


def readonly(tree):
    for p in tree.rglob('*'):
        p.chmod(0o555 if p.is_dir() or p.stat().st_mode & 0o111 else 0o444)
    tree.chmod(0o555)


def remove_tree(tree):
    if tree.exists():
        # Our installer made this tree read-only; writable directories permit unlinking.
        for p in tree.rglob('*'):
            if p.is_dir():
                p.chmod(0o755)
        tree.chmod(0o755)
        shutil.rmtree(tree)
