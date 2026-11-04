#!/usr/bin/env python3
"""Exercise fresh client configuration and lifecycle in a disposable project."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='ezm-client-integration-') as folder:
    root = Path(folder) / 'ezm-client'
    shutil.copytree(PROJECT / 'ezm-client', root, ignore=shutil.ignore_patterns('bin', 'runtime', 'log', '__pycache__'))
    (root / 'bin').mkdir()
    (root / 'bin/mihomo_1.19.33').symlink_to(PROJECT / 'ezm-client/bin/mihomo_1.19.33')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    (root / 'config/listening.port').write_text(str(port) + '\n')
    (root / 'config/protocol.activate').write_text('shadowsocks\n')
    (root / 'config/links/shadowsocks.link').write_text('ss://aes-128-gcm:example-password@127.0.0.1:9\n')
    tools = Path(folder) / 'tools'
    tools.mkdir()
    # Connection probes are deterministic and make no external network requests.
    curl = tools / 'curl'
    curl.write_text('#!/bin/bash\nprintf "200 0.001"\n')
    curl.chmod(0o700)
    home = Path(folder) / 'home'
    home.mkdir()
    cronfile = Path(folder) / 'crontab'
    cronfile.write_text('0 0 * * * echo preserve-existing-task\n')
    crontab = tools / 'crontab'
    crontab.write_text("#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\np=Path(os.environ['TEST_CRONTAB'])\na=sys.argv[1]\nif a=='-l':\n print(p.read_text() if p.exists() else '',end='')\nelif a=='-r':\n p.unlink(missing_ok=True)\nelif a=='-':\n p.write_text(sys.stdin.read())\nelse: sys.exit(1)\n")
    crontab.chmod(0o700)
    # The test prepares no real cron daemon; only its availability is stubbed.
    with (root / 'scripts/dependencies.sh').open('a') as stream:
        stream.write('\nclient_cron_running() { return 0; }\n')
    env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], HOME=str(home), TEST_CRONTAB=str(cronfile))
    def call(*args, check=True, input_text=None):
        result = subprocess.run(['bash', str(root / 'client.sh'), *args], env=env, capture_output=True, text=True, timeout=25, input=input_text)
        if check and result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return result
    try:
        call('start')
        assert (root / 'config/listening.port').read_text().strip() == str(port)
        assert (root / 'config/links/shadowsocks.link').exists()
        config = json.loads((root / 'runtime/mihomo.json').read_text())
        assert config['mixed-port'] == port and config['proxies'][0]['type'] == 'ss'
        status = call('status').stdout
        assert f'127.0.0.1:{port}' in status and '未在监听' not in status and '被其他进程占用' not in status
        pid = (root / 'runtime/mihomo.pid').read_text()
        call('start')
        assert (root / 'runtime/mihomo.pid').read_text() == pid
        call('test')
        call('restart')
        call('switch', input_text='5\n')
        assert (root / 'config/protocol.activate').read_text().strip() == 'shadowsocks'
        # Enabling boot touches only the fake crontab, preserving other entries.
        call('boot', input_text='1\n')
        assert '# ezm-client-proxy-boot' in cronfile.read_text()
        assert str(root / 'client.sh') in cronfile.read_text()
        call('boot', input_text='2\n')
        assert cronfile.read_text().strip() == '0 0 * * * echo preserve-existing-task'
        call('terminal', input_text='1\n')
        assert (root / 'config/terminal.on').exists()
        assert '# ezm-client-proxy-terminal-begin' in (home / '.bashrc').read_text()
        call('terminal', input_text='2\n')
        assert not (root / 'config/terminal.on').exists()
        assert '# ezm-client-proxy-terminal-begin' not in (home / '.bashrc').read_text()
        shell = subprocess.run(['bash', '-c',
            'export HTTP_PROXY=http://before.example; source "$1/proxy.env" on; '
            '[[ "$HTTP_PROXY" == "http://127.0.0.1:$2" ]] || exit 1; '
            'source "$1/proxy.env" off; [[ "$HTTP_PROXY" == http://before.example ]]',
            '_', str(root), str(port)], env=env, capture_output=True, text=True, timeout=15)
        assert shell.returncode == 0, shell.stdout + shell.stderr
        call('stop')
        assert not (root / 'runtime/mihomo.pid').exists()
    finally:
        call('stop', check=False)
print('PASS: fresh client protocol.activate/listening.port/.link reads, real core start/restart/switch/status/test/stop, boot/terminal and proxy.env restoration')
