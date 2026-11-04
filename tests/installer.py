#!/usr/bin/env python3
"""Fresh install/update checks with local archives; no GitHub download."""
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
for project in ('ezm-client', 'ezm-server'):
    with tempfile.TemporaryDirectory(prefix='ezm-install-check-') as folder:
        base = Path(folder)
        archive = base / 'project.tar.gz'
        files = ({'client.sh': ('client program', 0o755), 'config/listening.port': ('10808\n', 0o644),
                  'config/protocol.activate': ('hysteria2\n', 0o644), 'config/links/vless.link': ('', 0o644)}
                 if project == 'ezm-client' else
                 {'server.sh': ('server program', 0o755), 'config/ports/subscription.port': ('11800\n', 0o644),
                  'config/subscription.token': ('', 0o644)})
        with tarfile.open(archive, 'w:gz') as tar:
            for name, (content, mode) in files.items():
                data = content.encode()
                info = tarfile.TarInfo('ez-mihomo-main/' + project + '/' + name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
        tools = base / 'tools'
        tools.mkdir()
        curl = tools / 'curl'
        curl.write_text('#!/bin/bash\nwhile (($#)); do if [[ "$1" == -o ]]; then cp -- "$TEST_ARCHIVE" "$2"; exit; fi; shift; done\nexit 1\n')
        curl.chmod(0o700)
        env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], TEST_ARCHIVE=str(archive))
        target = base / 'installed'
        def install():
            result = subprocess.run(['bash', str(PROJECT / 'install.sh'), str(target)],
                input='2\n' if project == 'ezm-client' else '1\n', text=True, capture_output=True, env=env)
            assert result.returncode == 0, result.stdout + result.stderr
        install()
        for name, (content, mode) in files.items():
            path = target / name
            assert path.read_text() == content
            assert path.stat().st_mode & 0o777 == (0o700 if mode & 0o111 else 0o600)
        assert (target / 'config').stat().st_mode & 0o777 == 0o700
        config = target / ('config/listening.port' if project == 'ezm-client' else 'config/ports/subscription.port')
        config.write_text('19800\n')
        if project == 'ezm-server':
            (target / 'config/subscription.token').write_text('retained-token\n')
        program = target / ('client.sh' if project == 'ezm-client' else 'server.sh')
        program.write_text('old program')
        install()
        assert config.read_text() == '19800\n'
        assert program.read_text() == files[program.name][0]
        if project == 'ezm-server':
            assert (target / 'config/subscription.token').read_text() == 'retained-token\n'
print('PASS: both fresh installers, updated program files, preserved current config/token and uniform permissions')
