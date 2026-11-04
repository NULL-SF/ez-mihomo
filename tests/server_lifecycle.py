#!/usr/bin/env python3
"""Isolated real-core integration checks; no firewall, cron or public ACME calls."""
import contextlib
import io
import fcntl
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]


def free_port(used):
    while True:
        with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind(('127.0.0.1', 0))
            port = tcp.getsockname()[1]
            if port in used:
                continue
            try:
                udp.bind(('127.0.0.1', port))
            except OSError:
                continue
            used.add(port)
            return port


def worker():
    root = Path.cwd()
    sys.path.insert(0, str(root / 'scripts'))
    import service
    import configure
    import ports
    import outbound
    import subscription
    import firewall

    directory = root / 'config/ports'
    # Missing ports directory restores defaults and does not reset the token.
    token_path = root / 'config/subscription.token'
    token_path.write_text('preserved-token\n')
    shutil.rmtree(directory)
    defaults = ports.load()
    assert defaults == ports.DEFAULTS
    assert token_path.read_text() == 'preserved-token\n'
    token_path.write_text('')
    for invalid in ('0', '1023', '65536', '-1', 'abc'):
        (directory / 'subscription.port').write_text(invalid)
        try:
            ports.load()
            raise AssertionError('invalid subscription port accepted')
        except ValueError:
            pass
    (directory / 'subscription.port').write_text('11800\n')
    if os.geteuid() != 0:
        for action in ('start', 'stop', 'restart', 'boot'):
            result = subprocess.run(['bash', str(root / 'server.sh'), action], capture_output=True, text=True)
            assert result.returncode != 0 and '需要 root 权限' in result.stdout + result.stderr
        assert not (root / 'runtime').exists()
    # Subscription defaults and port conflicts use the same registry.
    (directory / 'subscription.port').unlink()
    values = ports.load()
    assert values['subscription'] == 11800
    (directory / 'subscription.port').write_text('')
    assert ports.load()['subscription'] == 11800
    (directory / 'subscription.port').write_text(str(values['vless']))
    try:
        ports.load()
        raise AssertionError('duplicate subscription accepted')
    except ValueError:
        pass
    (directory / 'subscription.port').write_text('11800\n')
    (directory / 'local_relay.port').write_text('11800\n')
    try:
        ports.load()
        raise AssertionError('relay/subscription collision accepted')
    except ValueError:
        pass
    (directory / 'local_relay.port').write_text('0\n')

    used = set()
    for name in (*service.SPECS, 'subscription'):
        (directory / (name + '.port')).write_text(str(free_port(used)) + '\n')
    args = SimpleNamespace(server='127.0.0.1', tls_name='127.0.0.1', listen='127.0.0.1',
                           ports=directory, base_port=None, self_signed=True, cert=None, key=None,
                           vless_security='reality', reality_target='www.microsoft.com', reality_port=443)
    configure.generate(args)
    subscription.validate()
    original = {p: (root / 'runtime' / p / 'config.json').read_bytes() for p in service.SPECS}
    for protocol in service.SPECS:
        assert configure.complete(protocol)
        (root / 'output/links' / (protocol + '.link')).write_text('damaged output')
        assert configure.complete(protocol), protocol
    shutil.rmtree(root / 'output')
    assert all(configure.complete(p) for p in service.SPECS)
    configure.initialize()
    assert {p: (root / 'runtime' / p / 'config.json').read_bytes() for p in service.SPECS} == original

    secret = subscription.token(create=True)
    assert subscription.token(create=True) == secret
    token_path = root / 'config/subscription.token'
    token_path.write_text('')
    assert subscription.token(create=True) != secret
    secret = subscription.token()
    options = subscription.settings()
    preexisting = options['url']
    try:
        (root / 'log').mkdir(exist_ok=True)
        for protocol in service.SPECS:
            service.start(protocol)
        # Exercise local end-to-end forwarding, without an external website.
        spec = importlib.util.spec_from_file_location('link_config', PROJECT / 'ezm-client/scripts/link_config.py')
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        class Echo(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'ezm-local-forwarding-check'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        echo = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Echo)
        thread = threading.Thread(target=echo.serve_forever, daemon=True)
        thread.start()
        try:
            for protocol in service.SPECS:
                with tempfile.TemporaryDirectory(prefix='ezm-client-probe-') as home:
                    mixed = free_port(used)
                    link = configure.node_link(protocol, json.loads((root / 'runtime' / protocol / 'config.json').read_text()))
                    config = parser.parse_link(protocol, link, mixed)
                    config['tls'] = {'custom-certifactes': [(subscription.LIVE / 'fullchain.pem').read_text()]}
                    path = Path(home) / 'client.json'
                    path.write_text(json.dumps(config))
                    check = subprocess.run([str(root / 'bin/mihomo_1.19.33'), '-t', '-d', home, '-f', str(path)], capture_output=True)
                    assert check.returncode == 0, protocol + ': client configuration rejected'
                    # REALITY's handshake target is public; validate its config only.
                    if protocol == 'vless':
                        continue
                    env = {key: value for key, value in os.environ.items() if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
                    env['SSL_CERT_FILE'] = str(subscription.LIVE / 'fullchain.pem')
                    child = subprocess.Popen([str(root / 'bin/mihomo_1.19.33'), '-d', home, '-f', str(path)], env=env,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        for _ in range(100):
                            assert child.poll() is None, protocol + ': client core exited'
                            try:
                                with socket.create_connection(('127.0.0.1', mixed), timeout=.1):
                                    break
                            except OSError:
                                time.sleep(.03)
                        connection = http.client.HTTPConnection('127.0.0.1', mixed, timeout=10)
                        try:
                            connection.request('GET', f'http://127.0.0.1:{echo.server_port}/check')
                            response = connection.getresponse()
                            assert response.status == 200 and response.read() == b'ezm-local-forwarding-check', protocol
                        finally:
                            connection.close()
                    finally:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
        finally:
            echo.shutdown()
            echo.server_close()
            thread.join()
        print('PASS: seven exported share links parsed/validated, six protocols forwarded real local HTTP traffic (REALITY external target excluded)')
        subscription.start()
        subscription_pid = service.owned('subscription')
        # Status reports the deployed port even while an invalid edit is pending.
        portfile = directory / 'subscription.port'
        configured_port = portfile.read_text()
        portfile.write_text('0\n')
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                subscription.status()
            assert '运行中' in output.getvalue() and str(options['port']) in output.getvalue()
        finally:
            portfile.write_text(configured_port)
        subscription.start()
        assert service.owned('subscription') == subscription_pid
        configure.export_links()
        assert all((root / 'output/links' / (p + '.link')).is_file() for p in (*service.SPECS, 'subscription'))
        assert not list(root.rglob('*.yaml')), 'YAML must not be persisted'

        ctx = ssl.create_default_context(cafile=str(subscription.LIVE / 'fullchain.pem'))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        def get(url):
            return opener.open(url, timeout=10)
        with get(preexisting) as response:
            first = response.read()
            assert response.status == 200 and b'vless' in first and b'proxy-groups:' in first
            assert response.headers['Cache-Control'] == 'no-store'
        for suffix in ('/wrong/mihomo.yaml', '/../../config/subscription.token'):
            invalid = f'https://127.0.0.1:{options["port"]}' + suffix
            try:
                get(invalid)
                raise AssertionError('unauthenticated download accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 404
        # Export is valid YAML according to the actual core.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'download.yaml'
            path.write_bytes(first)
            result = subprocess.run([str(root / 'bin/mihomo_1.19.33'), '-t', '-d', temp, '-f', str(path)], capture_output=True)
            assert result.returncode == 0, result.stdout.decode()

        # Request during a management transaction cannot see a partial snapshot.
        with (root / 'runtime/manage.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                get(preexisting)
                raise AssertionError('concurrent partial snapshot allowed')
            except urllib.error.HTTPError as error:
                assert error.code == 503

        # Delete just one config; retain its PID so repair can stop the old core.
        pid_before = {p: service.owned(p) for p in service.SPECS}
        path = root / 'runtime/vless/config.json'
        path.unlink()
        repair = SimpleNamespace(**vars(args))
        repair.self_signed = False
        repair.cert = subscription.LIVE / 'fullchain.pem'
        repair.key = subscription.LIVE / 'privkey.pem'
        repair.protocols = ['vless']
        repair.repair = True
        repair.managed_source = True
        configure.generate(repair)
        service.start('vless')
        assert path.read_bytes() != original['vless']
        for protocol in service.SPECS[1:]:
            assert (root / 'runtime' / protocol / 'config.json').read_bytes() == original[protocol]
            assert service.owned(protocol) == pid_before[protocol]
        assert subscription.settings()['url'] == preexisting
        with get(preexisting) as response:
            assert response.read() != first, 'subscription did not reflect rebuild'
        configure.export_links()
        # Downloads never depend on the exported link files.
        shutil.rmtree(root / 'output')
        with get(preexisting) as response:
            assert response.status == 200
        configure.export_links()

        # Token and port changes restart only the subscription service.
        node_pids = {p: service.owned(p) for p in service.SPECS}
        token_path.write_text('replacement-token\n')
        subscription.start()
        try:
            get(preexisting)
            raise AssertionError('old token still accepted')
        except urllib.error.HTTPError as error:
            assert error.code == 404
        with get(subscription.settings()['url']) as response:
            assert response.status == 200
        new_port = free_port(used)
        (directory / 'subscription.port').write_text(str(new_port))
        subscription.start()
        with get(subscription.settings()['url']) as response:
            assert response.status == 200
        assert {p: service.owned(p) for p in service.SPECS} == node_pids
        required = firewall.required_ports()
        assert f'{new_port}/tcp' in required and '80/tcp' in required

        # UFW changes only project rules, never enable/disable/reset.
        original_rules = [['allow', '22/tcp'], ['allow', '443/tcp', 'comment', 'user-rule']]
        rules = [list(rule) for rule in original_rules]
        def fake_ufw(*args):
            if args == ('show', 'added'):
                return '\n'.join('ufw ' + shlex.join(row) for row in rules)
            if args[:2] == ('prepend', 'allow'):
                rules.append(list(args[1:]))
            elif args[:2] == ('--force', 'delete'):
                rules.remove(list(args[2:]))
            else:
                raise AssertionError(args)
            return ''
        old_ufw = firewall.ufw
        firewall.ufw = fake_ufw
        try:
            with patch.object(firewall.shutil, 'which', return_value='/mock/ufw'):
                firewall.configure('start', quiet=True)
                assert any(firewall.target(rule) == f'{new_port}/tcp' for rule in rules)
                firewall.configure('stop')
                assert rules == original_rules
        finally:
            firewall.ufw = old_ufw
        assert secret not in (root / 'log/subscription.log').read_text()

        # Reload the certificate at the same URL without restarting any service.
        sub_pid = service.owned('subscription')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes', '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', str(subscription.LIVE / 'privkey.pem'), '-out', str(subscription.LIVE / 'fullchain.pem')],
                       check=True, capture_output=True)
        new_ctx = ssl.create_default_context(cafile=str(subscription.LIVE / 'fullchain.pem'))
        renewed = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=new_ctx))
        with renewed.open(subscription.settings()['url'], timeout=10) as response:
            assert response.status == 200
        assert service.owned('subscription') == sub_pid
    finally:
        service.stop('subscription')
        for protocol in service.SPECS:
            service.stop(protocol)
    print('PASS: port validation, seven real cores, export/rebuild isolation, HTTPS auth, dynamic updates, locking, token/port changes and TLS reload')


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        with tempfile.TemporaryDirectory(prefix='ezm-integration-') as folder:
            root = Path(folder) / 'ezm-server'
            shutil.copytree(PROJECT / 'ezm-server', root, ignore=shutil.ignore_patterns('bin', 'runtime', 'output', 'log', '__pycache__'))
            (root / 'bin').mkdir()
            (root / 'bin/mihomo_1.19.33').symlink_to(PROJECT / 'ezm-server/bin/mihomo_1.19.33')
            subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'], cwd=root, check=True)
