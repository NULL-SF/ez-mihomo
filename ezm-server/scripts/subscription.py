#!/usr/bin/env python3
"""Serve an authenticated, dynamically rendered mihomo subscription over HTTPS."""
import fcntl
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
from service import ROOT, SPECS, command, identity, owned, state_file, stop, user_config_access, write_state
from outbound import atomic_write
from ports import load as load_ports

LIVE = ROOT / 'runtime/certs/config/live/ezm-node'


def token(create=False):
    path = ROOT / 'config/subscription.token'
    value = path.read_text().strip() if path.exists() else ''
    if not value and create:
        value = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, (value + '\n').encode(), 0o600)
        user_config_access(path, 0o600)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
        raise ValueError('subscription.token 为空或无效；请使用字母、数字、下划线或连字符')
    return value


def documents():
    docs = {}
    for protocol in SPECS:
        doc = json.loads((ROOT / 'runtime' / protocol / 'config.json').read_text())
        if doc['listeners'][0]['type'] != protocol:
            raise ValueError('节点协议不匹配')
        metadata = doc.setdefault('_ezm', {})
        if not metadata.get('server'):
            from configure import certificate_name
            metadata['server'] = certificate_name()
        metadata.setdefault('tls-name', metadata['server'])
        docs[protocol] = doc
    return docs


def server_address(docs):
    from configure import host
    addresses = {doc.get('_ezm', {}).get('server') for doc in docs.values()}
    if len(addresses) != 1 or not next(iter(addresses)):
        raise ValueError('节点服务器地址不一致或缺失')
    return host(addresses.pop())


def settings(create=False):
    port = load_ports(write_defaults=create)['subscription']
    docs = documents()
    address = server_address(docs)
    # A custom TLS name may differ from the node IP; HTTPS must use the
    # certificate identity, while listening still follows the server IP family.
    names = {doc['_ezm']['tls-name'] for doc in docs.values()}
    if len(names) != 1:
        raise ValueError('节点证书名称不一致')
    tls_name = names.pop()
    secret = token(create)
    listen = '::' if ':' in address else '0.0.0.0'
    signature = hashlib.sha256(f'{listen}:{port}:{secret}'.encode()).hexdigest()
    endpoint = '[' + tls_name + ']' if ':' in tls_name else tls_name
    return {'port': port, 'listen': listen, 'token': secret, 'signature': signature,
            'url': f'https://{endpoint}:{port}/{secret}/mihomo.yaml'}


def proxy(protocol, doc):
    from configure import node_link
    listener = doc['listeners'][0]
    node = {'name': protocol, 'type': 'ss' if protocol == 'shadowsocks' else protocol,
            'server': doc['_ezm']['server'], 'port': listener['port'], 'udp': True}
    if protocol in ('vless', 'vmess'):
        user = listener['users'][0]
        node.update(uuid=user['uuid'], network='tcp', tls=True)
        if protocol == 'vmess':
            node.update(alterId=user.get('alterId', 0), cipher='auto')
        else:
            node['encryption'] = 'none'
            if listener.get('reality-config'):
                params = parse_qs(urlsplit(node_link(protocol, doc)).query)
                node.update(flow=user['flow'], servername=params['sni'][0],
                            **{'client-fingerprint': 'chrome', 'reality-opts': {
                                'public-key': params['pbk'][0], 'short-id': params['sid'][0]}})
            else:
                node['servername'] = doc['_ezm']['tls-name']
        if protocol == 'vmess':
            node['servername'] = doc['_ezm']['tls-name']
    elif protocol == 'shadowsocks':
        node.update(cipher=listener['cipher'], password=listener['password'])
    elif protocol == 'trojan':
        node.update(password=listener['users'][0]['password'], network='tcp')
    elif protocol == 'tuic':
        uid, password = next(iter(listener['users'].items()))
        node.update(uuid=uid, password=password, **{'congestion-controller': listener.get('congestion-controller', 'cubic')})
    else:
        node['password'] = next(iter(listener['users'].values()))
        if protocol == 'hysteria2':
            node.update(obfs=listener['obfs'], **{'obfs-password': listener['obfs-password']})
    if protocol not in ('vless', 'vmess', 'shadowsocks'):
        node['sni'] = doc['_ezm']['tls-name']
    if listener.get('alpn'):
        node['alpn'] = listener['alpn']
    return node


def render(docs):
    nodes = [proxy(p, docs[p]) for p in SPECS]
    group = {'name': 'PROXY', 'type': 'select', 'proxies': list(SPECS)}
    # JSON flow mappings are valid YAML; no third-party YAML parser is needed.
    lines = ['mixed-port: 10808', 'bind-address: "127.0.0.1"', 'allow-lan: false',
             'mode: rule', 'log-level: warning', 'proxies:']
    lines += ['  - ' + json.dumps(n, ensure_ascii=False) for n in nodes]
    lines += ['proxy-groups:', '  - ' + json.dumps(group), 'rules:', '  - "MATCH,PROXY"']
    return ('\n'.join(lines) + '\n').encode()


def validate():
    data = render(documents())
    # Check the exported client config with the bundled core, without listeners.
    with tempfile.TemporaryDirectory(dir=ROOT / 'runtime') as folder:
        path = Path(folder) / 'mihomo.yaml'
        path.write_bytes(data)
        result = subprocess.run([str(ROOT / 'bin/mihomo_1.19.33'), '-t', '-d', folder, '-f', str(path)],
                                capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError('订阅客户端配置未通过 mihomo 校验')


def context():
    result = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    result.minimum_version = ssl.TLSVersion.TLSv1_2
    result.load_cert_chain(LIVE / 'fullchain.pem', LIVE / 'privkey.pem')
    return result


def check_port(port, listen):
    # Detect conflicts before rebuilding nodes, including a previous live port.
    for protocol in SPECS:
        try:
            doc = json.loads((ROOT / 'runtime' / protocol / 'config.json').read_text())
            if doc['listeners'][0]['port'] == port:
                raise ValueError('订阅端口与已部署节点端口冲突')
        except (OSError, json.JSONDecodeError, KeyError, TypeError, IndexError):
            continue
    pid = owned('subscription')
    if pid:
        saved = json.loads(state_file('subscription').read_text())
        if saved.get('port') == port and saved.get('listen') == listen:
            return
    family = socket.AF_INET6 if ':' in listen else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((listen, port))
        except OSError as error:
            raise ValueError('订阅 TCP 端口不可用，请检查监听占用') from error


def preflight(options):
    check_port(options['port'], options['listen'])


def start():
    options = settings(create=True)
    validate()
    context()
    preflight(options)
    pid = owned('subscription')
    if pid:
        saved = json.loads(state_file('subscription').read_text())
        if saved.get('signature') == options['signature']:
            print('subscription: 已运行')
            return
        stop('subscription')
    home = ROOT / 'runtime/subscription'
    home.mkdir(parents=True, exist_ok=True)
    (ROOT / 'log').mkdir(parents=True, exist_ok=True)
    with (ROOT / 'log/subscription.log').open('ab') as log:
        child = subprocess.Popen(command('subscription'), cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True, umask=0o077)
    try:
        for _ in range(100):
            if child.poll() is not None:
                raise ValueError('订阅服务启动失败，请查看 log/subscription.log')
            if owned('subscription') == child.pid:
                print('subscription: 已启动')
                return
            time.sleep(.05)
        raise ValueError('订阅服务启动超时')
    except BaseException:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise


def status():
    try:
        pid = owned('subscription', report_permission=True)
        if pid:
            port = json.loads(state_file('subscription').read_text()).get('port', '未知')
        else:
            try:
                port = load_ports(write_defaults=False)['subscription']
            except (OSError, ValueError):
                port = '配置无效'
        print(f'subscription: {"运行中" if pid else "未运行"}；端口 {port}；TCP/HTTPS')
    except PermissionError:
        print('subscription: 无法确认状态（需 sudo 查看）')


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, options):
        self.options = options
        self.tls = context()
        self.tls_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(32)
        self.address_family = socket.AF_INET6 if ':' in options['listen'] else socket.AF_INET
        super().__init__((options['listen'], options['port']), Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def finish_request(self, request, client_address):
        request.settimeout(10)
        # Certificate renewal holds this inode exclusively. Keep the previous
        # context during renewal; refresh only while a complete pair is stable.
        fd = os.open(ROOT, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                with self.tls_lock:
                    try:
                        self.tls = context()
                    except (OSError, ssl.SSLError):
                        pass
        finally:
            os.close(fd)
        try:
            with self.tls.wrap_socket(request, server_side=True) as connection:
                Handler(connection, client_address, self)
        except (OSError, ssl.SSLError):
            request.close()

    def handle_error(self, request, client_address):
        # Never log request paths or tokens, including malformed requests.
        print('订阅请求处理失败', file=sys.stderr, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'ezm-subscription'
    sys_version = ''

    def log_message(self, *_):
        pass

    def respond(self, code, data=b''):
        self.send_response(code)
        self.send_header('Content-Type', 'application/yaml; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if code == 200:
            self.send_header('Content-Disposition', 'attachment; filename="mihomo.yaml"')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_GET(self):
        expected = '/' + self.server.options['token'] + '/mihomo.yaml'
        if not hmac.compare_digest(self.path.encode(), expected.encode()):
            self.respond(404)
            return
        # Readers never see partial multi-node publication during start/rebuild.
        try:
            with (ROOT / 'runtime/manage.lock').open('a+') as lock:
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.respond(503)
                            return
                        time.sleep(.02)
                data = render(documents())
        except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, subprocess.SubprocessError):
            self.respond(503)
            return
        self.respond(200, data)

    do_HEAD = do_GET


def run():
    options = settings()
    with Server(options) as server:
        write_state(state_file('subscription'), {'pid': os.getpid(), 'start': identity(os.getpid()),
                    'port': options['port'], 'listen': options['listen'], 'signature': options['signature']})
        # Default SIGTERM exits immediately; the parent owns PID cleanup.
        server.serve_forever(poll_interval=.2)


if __name__ == '__main__':
    try:
        action = sys.argv[1]
        if action == 'run':
            run()
        elif action == 'start':
            start()
        elif action == 'stop':
            stop('subscription')
        elif action == 'status':
            status()
        elif action == 'preflight':
            preflight(settings(create=True))
        else:
            raise ValueError('未知订阅管理命令')
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as error:
        # Avoid rendering configuration contents (including tokens/passwords).
        sys.exit('订阅操作失败：' + str(error))
