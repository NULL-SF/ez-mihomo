#!/usr/bin/env python3
"""Check uniform project permissions without privileged filesystem mutations."""
import importlib.util
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
for project in ('ezm-client', 'ezm-server'):
    spec = importlib.util.spec_from_file_location('permissions_' + project, PROJECT / project / 'scripts/permissions.py')
    layout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / 'project'
        (root / 'config').mkdir(parents=True)
        secret = root / 'config/private.pem'
        secret.write_text('example')
        secret.chmod(0o644)
        executable = root / 'run.sh'
        executable.write_text('example')
        executable.chmod(0o755)
        outside = Path(folder) / 'outside'
        outside.write_text('example')
        outside.chmod(0o644)
        link = root / 'config/link'
        link.symlink_to(outside)
        owner = root.stat()
        layout.restore_access(root)
        assert secret.stat().st_mode & 0o777 == 0o600
        assert executable.stat().st_mode & 0o777 == 0o700
        assert (root / 'config').stat().st_mode & 0o777 == 0o700
        assert outside.stat().st_mode & 0o777 == 0o644
        # Another sudo user must not silently take over an existing installation.
        with patch.object(layout.os, 'geteuid', return_value=0), patch.object(layout.os, 'chown') as chown, patch.dict(os.environ, {'SUDO_UID': '2345', 'SUDO_GID': '2346'}):
            layout.restore_access(root)
            expected = (owner.st_uid, owner.st_gid) if owner.st_uid else (2345, 2346)
            assert all(call.args[1:] == expected for call in chown.call_args_list)
            assert any(call.args[0] == link and call.kwargs == {'follow_symlinks': False} for call in chown.call_args_list)
        assert outside.stat().st_mode & 0o777 == 0o644
print('PASS: both layouts normalize ownership/permissions, preserve executability and leave symlink targets untouched')
