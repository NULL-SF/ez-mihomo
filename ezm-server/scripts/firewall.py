#!/usr/bin/env python3
"""Add and remove only this project's UFW allow rules."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
MARK = 'ezm-server-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12] + ':'


def ufw(*args):
    command = ['ufw', *args]
    try:
        result = subprocess.run(command, env=dict(os.environ, LC_ALL='C'),
                                check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or '').strip() or (error.stdout or '').strip()
        raise ValueError(f'{shlex.join(command)} 执行失败（退出码 {error.returncode}）'
                         + (f'：\n{detail}' if detail else '')) from error
    except subprocess.TimeoutExpired as error:
        raise ValueError(f'{shlex.join(command)} 执行超时；规则可能已部分写入，'
                         '请检查 ufw show added 后重试。') from error
    return result.stdout


def rules():
    result = []
    for line in ufw('show', 'added').splitlines():
        if line.startswith('ufw '):
            result.append(shlex.split(line)[1:])
    return result


def owned(rule):
    return ('comment' in rule and rule.index('comment') + 1 < len(rule)
            and rule[rule.index('comment') + 1].startswith(MARK))


def target(rule):
    if not rule or rule[0] != 'allow':
        return None
    parts = rule[1:rule.index('comment')] if 'comment' in rule else rule[1:]
    if parts and parts[0] == 'in':
        parts = parts[1:]
    if len(parts) == 1 and re.fullmatch(r'\d+(?:/(?:tcp|udp))?', parts[0]):
        return parts[0]
    return None


def required_ports():
    from configure import load_ports
    ports = load_ports(ROOT / 'config/ports')
    from ports import load
    required = {'80/tcp', str(load()['subscription']) + '/tcp'}
    # Retain the live subscription port until a port change has been applied.
    try:
        from service import owned, state_file
        if owned('subscription'):
            active = json.loads(state_file('subscription').read_text())['port']
            required.add(str(active) + '/tcp')
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for protocol, port in ports.items():
        transports = ('tcp', 'udp') if protocol == 'shadowsocks' else (
            ('udp',) if protocol in ('hysteria2', 'tuic') else ('tcp',))
        # A changed port file takes effect when its node is rebuilt. Keep the
        # currently configured listener reachable until then.
        listener_ports = {port}
        try:
            doc = json.loads((ROOT / 'runtime' / protocol / 'config.json').read_text())
            listener = doc['listeners'][0]
            active_port = listener.get('port')
            if listener.get('type') == protocol and type(active_port) is int and 1024 <= active_port <= 65535:
                listener_ports.add(active_port)
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            pass
        required.update(f'{number}/{transport}' for number in listener_ports for transport in transports)
    return required


def configure(action, quiet=False):
    if not shutil.which('ufw'):
        if not quiet:
            print('未安装 UFW，跳过防火墙规则管理。')
        return
    current = rules()
    if action == 'stop':
        for rule in current:
            if owned(rule):
                ufw('--force', 'delete', *rule)
        print('已移除本项目的 UFW 放行规则，原有规则保留。')
        return
    required = required_ports()
    added, removed = [], []
    existing = {target(rule) for rule in current}
    for port in sorted(required):
        if port in existing or port.split('/')[0] in existing:
            continue
        # prepend works with empty IPv4/IPv6 lists and, unlike plain allow,
        # does not overwrite an existing rule's comment.
        ufw('prepend', 'allow', port, 'comment', MARK + port)
        added.append(port)
    # Remove only old project rules, including ports changed since last start.
    for rule in current:
        if owned(rule) and target(rule) not in required:
            ufw('--force', 'delete', *rule)
            removed.append(target(rule))
    for title, ports in (('UFW 新增放行规则：', added), ('UFW 移除放行规则：', removed)):
        if not ports:
            continue
        print(title)
        if '80/tcp' in ports:
            print('  证书验证：80/TCP')
        for transport in ('tcp', 'udp'):
            numbers = sorted(int(port.split('/')[0]) for port in ports
                             if port.endswith('/' + transport) and port != '80/tcp')
            if numbers:
                print('  ' + transport.upper() + '：' + '、'.join(map(str, numbers)))
    if not quiet and not added and not removed:
        print('UFW 放行规则无变化。')



if __name__ == '__main__':
    try:
        if (len(sys.argv) not in (2, 3) or sys.argv[1] not in ('start', 'stop')
                or (len(sys.argv) == 3 and sys.argv[2] != '--quiet')):
            raise ValueError('用法：firewall.py start|stop [--quiet]')
        configure(sys.argv[1], quiet=len(sys.argv) == 3)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit('UFW 规则管理失败：' + str(error))
