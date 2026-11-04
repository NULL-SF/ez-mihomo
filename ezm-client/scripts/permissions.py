"""Keep project files writable by their installing user."""
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def restore_access(root):
    """Trust the installing user with the whole project; never follow symlinks."""
    root = Path(root)
    privileged = os.geteuid() == 0
    owner = root.stat()
    # Keep the installation owner stable even when another administrator runs sudo.
    uid = owner.st_uid if owner.st_uid != 0 else int(os.environ.get('SUDO_UID', owner.st_uid))
    gid = owner.st_gid if owner.st_uid != 0 else int(os.environ.get('SUDO_GID', owner.st_gid))
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name != '.git']
        directory = Path(parent)
        if not directory.is_symlink():
            if privileged:
                os.chown(directory, uid, gid)
            if privileged or directory.stat().st_uid == os.geteuid():
                directory.chmod(0o700)
        for name in files:
            path = directory / name
            if path.is_symlink():
                if privileged:
                    os.chown(path, uid, gid, follow_symlinks=False)
            elif path.is_file():
                executable = bool(path.stat().st_mode & 0o111)
                if privileged:
                    os.chown(path, uid, gid)
                if privileged or path.stat().st_uid == os.geteuid():
                    path.chmod(0o700 if executable else 0o600)


if __name__ == '__main__':
    try:
        restore_access(Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as error:
        sys.exit('项目文件权限整理失败：' + str(error))
