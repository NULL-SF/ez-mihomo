#!/usr/bin/env python3
"""Apply config/ports/local_relay.txt without changing inbound credentials or links.

Called under server.sh's existing management lock. No menus or saved mode state.
"""
import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from service import ROOT, SPECS, command, owned, start, stop, user_config_access

PROXY = 'ezm-local-socks'


def load_port(write_default=False, check_ports=True):
    path = ROOT / 'config/ports/local_relay.txt'
    value = path.read_text().strip() if path.exists() else ''
    if not value:
        if write_default:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, b'0\n', 0o644)
            user_config_access(path)
        return 0
    if not value.isascii() or not value.isdecimal() or not 0 <= int(value) <= 65535:
        raise ValueError('config/ports/local_relay.txt 必须填写 0–65535 的整数；0 表示直接出口。')
    port = int(value)
    if port and check_ports:
        for protocol in SPECS:
            path = ROOT / 'config/ports' / (protocol + '.txt')
            if path.is_file():
                value = path.read_text().strip()
                if value.isascii() and value.isdecimal() and int(value) == port:
                    raise ValueError('本机中继端口不能与服务端入站端口相同。')
    return port


def describe(port):
    return ('出站配置：直接出口' if port == 0 else
            f'出站配置：本机中继端口 127.0.0.1:{port}（SOCKS，不可用时不回退直连）')


def render(doc, port):
    result = copy.deepcopy(doc)
    proxies = [p for p in result.get('proxies', []) if p.get('name') != PROXY]
    if port:
        if any(listener.get('port') == port for listener in result.get('listeners', [])):
            raise ValueError('本机中继端口不能与服务端入站端口相同。')
        proxies.append({'name': PROXY, 'type': 'socks5', 'server': '127.0.0.1', 'port': port, 'udp': True})
    if proxies:
        result['proxies'] = proxies
    else:
        result.pop('proxies', None)
    result['rules'] = ['MATCH,' + (PROXY if port else 'DIRECT')]
    return result


def atomic_write(path, data, mode):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def synchronize(protocols):
    """Validate first; restart changed services, and report failure without fallback."""
    port = load_port(write_default=True)
    changes = []
    env = {k: v for k, v in dict(os.environ, SAFE_PATHS=str(ROOT / 'runtime/certs')).items()
           if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
           and k not in ('LD_PRELOAD', 'PROXYCHAINS_CONF_FILE', 'PROXYCHAINS_QUIET_MODE', '_MIHOMO_PROXY_SNAPSHOT')}
    for protocol in protocols:
        path = ROOT / 'runtime' / protocol / 'config.json'
        original = path.read_bytes()
        doc = json.loads(original)
        updated = render(doc, port)
        if updated == doc:
            continue
        data = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode()
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.json') as stream:
            stream.write(data)
            stream.flush()
            args = command(protocol)
            args[-1] = stream.name
            result = subprocess.run([args[0], '-t', *args[1:]], env=env,
                                    capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise ValueError(f'{protocol}：出站配置校验失败，未修改服务。\n{result.stdout}{result.stderr}')
        changes.append((protocol, path, original, data, path.stat().st_mode & 0o777))
    active = [protocol for protocol, *_ in changes if owned(protocol)]
    stopped, written = [], []
    try:
        for protocol in active:
            stopped.append(protocol)
            stop(protocol)
        for protocol, path, original, data, mode in changes:
            atomic_write(path, data, mode)
            written.append((path, original, mode))
        for protocol in active:
            start(protocol)
    except BaseException as error:
        failures = []
        for protocol in stopped:
            try:
                stop(protocol)
            except Exception as failure:
                failures.append(str(failure))
        for path, original, mode in written:
            try:
                atomic_write(path, original, mode)
            except Exception as failure:
                failures.append(str(failure))
        if failures:
            raise ValueError(f'出站应用失败：{error}；恢复时遇到错误：' + '；'.join(failures)) from error
        raise
    if changes:
        print(f'已更新 {len(changes)} 份出站配置，重启 {len(active)} 个运行中服务；节点链接不变。')
    return len(changes), len(active)
