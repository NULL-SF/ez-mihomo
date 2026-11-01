#!/usr/bin/env python3
"""Obtain and renew the project's ACME certificate without restarting listeners."""
import argparse
import configparser
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import ipaddress
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shlex
import shutil
import socket
import signal
import time
import subprocess
import sys
import tempfile
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / 'runtime/certs'
CONFIG = HOME / 'config'
LIVE = CONFIG / 'live/ezm-node'
MARK = ' # ezm-server-certificate-renew'
RETRY_MARK = ' # ezm-server-certificate-retry'
RENEW_BEFORE = 48 * 3600
CERTBOT_VERSION = '5.9.0'


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


@contextmanager
def cron_lock():
    # All projects for this UID share the same crontab; lock a stable inode.
    directory = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def cron_read():
    if not shutil.which('crontab'):
        raise ValueError('自动续期需要 crontab 和运行中的 cron 服务，请先安装并启用 cron。')
    result = subprocess.run(['crontab', '-l'], env=dict(os.environ, LC_ALL='C'), capture_output=True, text=True)
    if result.returncode:
        if 'no crontab for ' in result.stderr:
            return ''
        raise ValueError('读取 crontab 失败，未修改：' + result.stderr.strip())
    return result.stdout


def cron_running():
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if (proc / 'comm').read_text().strip() in ('cron', 'crond'):
                state = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                if state != 'Z':
                    return True
        except (OSError, IndexError):
            continue
    return False


def ensure_cron():
    installed = bool(shutil.which('crontab'))
    if installed and cron_running():
        return
    if os.geteuid() != 0:
        raise ValueError('cron 缺失或未运行，请由 root 执行 start 或 boot 自动安装启用。')
    if not installed:
        print('缺少 cron，正在安装系统定时任务服务。', flush=True)
        env = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
        if shutil.which('apt-get'):
            run(['apt-get', 'update'], env=env)
            run(['apt-get', 'install', '-y', 'cron'], env=env)
        elif shutil.which('dnf'):
            run(['dnf', 'install', '-y', 'cronie'], env=env)
        elif shutil.which('yum'):
            run(['yum', 'install', '-y', 'cronie'], env=env)
        else:
            raise ValueError('当前系统不支持自动安装 cron，请安装 crontab 和 cron 服务后重试。')
        if not shutil.which('crontab'):
            raise ValueError('cron 安装后仍未找到 crontab，请检查安装结果。')
    if shutil.which('systemctl'):
        for unit in ('cron', 'crond'):
            result = subprocess.run(['systemctl', 'enable', '--now', unit], capture_output=True)
            if result.returncode == 0 and cron_running():
                return
    # Debian/Ubuntu VPS images may still use SysV init instead of systemd.
    if shutil.which('update-rc.d') and shutil.which('service'):
        run(['update-rc.d', 'cron', 'defaults'])
        run(['service', 'cron', 'start'])
        if cron_running():
            return
    if shutil.which('chkconfig') and shutil.which('service'):
        run(['chkconfig', 'crond', 'on'])
        run(['service', 'crond', 'start'])
        if cron_running():
            return
    raise ValueError('无法启用 cron，请检查系统服务管理器；尚未登记定时任务。')


def tool():
    candidate = shutil.which('certbot')
    if candidate:
        try:
            result = subprocess.run([candidate, '--version'], capture_output=True, text=True, timeout=10)
            version = re.search(r'\b(\d+)\.(\d+)', result.stdout)
            if result.returncode == 0 and version and tuple(map(int, version.groups())) >= (5, 4):
                return [candidate]
        except (OSError, subprocess.SubprocessError):
            pass  # Fall back to the project-local environment if the system tool is broken.
    venv = HOME / 'venv'
    binary = venv / 'bin/certbot'
    usable = False
    if binary.is_file():
        try:
            usable = subprocess.run([str(venv / 'bin/python'), str(binary), '--version'],
                                    capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    if not usable:
        if venv.exists():
            shutil.rmtree(venv)
        print('在项目运行目录安装 Certbot ' + CERTBOT_VERSION + '，不修改系统 Python。', flush=True)
        run([sys.executable, '-m', 'venv', '--without-pip', venv])
        # Debian minimal images may omit ensurepip. Bootstrap a verified PyPI wheel
        # in this local environment rather than installing system packages.
        with urllib.request.urlopen('https://pypi.org/pypi/pip/25.3/json', timeout=30) as response:
            release = json.load(response)
        wheel = next(item for item in release['urls'] if item['filename'].endswith('.whl'))
        with urllib.request.urlopen(wheel['url'], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != wheel['digests']['sha256']:
            raise ValueError('pip 安装包校验失败。')
        bootstrap = HOME / 'pip-bootstrap.whl'
        bootstrap.write_bytes(data)
        try:
            env = dict(os.environ, PYTHONPATH=str(bootstrap))
            run([venv / 'bin/python', '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                 '--disable-pip-version-check', 'pip==25.3', 'certbot==' + CERTBOT_VERSION], env=env)
        finally:
            bootstrap.unlink(missing_ok=True)
    return [str(venv / 'bin/python'), str(binary)]


def environment():
    env = dict(os.environ)
    for key in list(env):
        if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') or key == 'LD_PRELOAD':
            env.pop(key)
    return env


def base(binary):
    return [*binary, '--config-dir', str(CONFIG), '--work-dir', str(HOME / 'work'),
            '--logs-dir', str(ROOT / 'log/certs'), '--non-interactive']


def validate(cert, key, seconds=3600):
    run(['openssl', 'x509', '-in', cert, '-noout', '-checkend', str(seconds)], capture_output=True)
    a = subprocess.check_output(['openssl', 'x509', '-in', cert, '-pubkey', '-noout'])
    b = subprocess.check_output(['openssl', 'pkey', '-in', key, '-passin', 'pass:', '-pubout'])
    if a != b:
        raise ValueError('新证书与私钥不匹配，保留当前证书。')


def verify_source():
    validate(LIVE / 'fullchain.pem', LIVE / 'privkey.pem')
    print('原始证书及私钥已校验；核心直接读取并热加载。', flush=True)


def schedule():
    if not (CONFIG / 'renewal/ezm-node.conf').is_file():
        raise ValueError('没有本项目的自动证书配置，请先执行 start。')
    ensure_cron()
    with cron_lock():
        current = cron_read()
        command = ' '.join(shlex.quote(str(x)) for x in (sys.executable, ROOT / 'scripts/certificate.py', 'renew'))
        now = datetime.now()
        line = f'{now.minute} {now.hour} * * * umask 027; ' + command + ' >> ' + shlex.quote(str(ROOT / 'log/certs/renewal.log')) + ' 2>&1' + MARK
        if '\n' in str(ROOT) or '\r' in str(ROOT):
            raise ValueError('项目路径含换行符，无法设置续期任务。')
        line = line.replace('%', r'\%')
        others = [x for x in current.splitlines() if not x.endswith(MARK)]
        updated = '\n'.join([*others, line]) + '\n'
        if updated != current:
            run(['crontab', '-'], input=updated, text=True)
    print('已按本次启动时间重排续期检查，下一次约 24 小时后，之后每天检查。', flush=True)


def issue(identifier, email):
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    (ROOT / 'log/certs').mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_cron()  # Prepare the scheduler before issuing a short-lived certificate.
    cron_read()
    try:
        address = ipaddress.ip_address(identifier)
        if not address.is_global:
            raise ValueError('自动申请需要公网 IP，不能使用回环或内网地址。')
        identity = ['--ip-address', str(address)]
    except ValueError as error:
        if re.fullmatch(r'[0-9.:]+', identifier) or ':' in identifier:
            raise ValueError('请输入有效公网 IP。') from error
        name = identifier.encode('idna').decode().lower()
        if len(name) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in name.split('.')):
            raise ValueError('证书名称必须是公网 IP 或域名。')
        identity = ['-d', name]
    # Standalone validation needs TCP 80; never stop another process to claim it.
    probe = socket.socket(socket.AF_INET6 if ':' in identifier else socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(('::' if ':' in identifier else '0.0.0.0', 80))
    except OSError as error:
        raise ValueError('无法监听验证端口 TCP 80，请检查权限和占用：' + str(error)) from error
    finally:
        probe.close()
    binary = tool()
    contact = ['--email', email] if email else ['--register-unsafely-without-email']
    run(base(binary) + ['certonly', '--standalone', '--preferred-profile', 'shortlived',
                       '--cert-name', 'ezm-node', '--agree-tos', '--reuse-key', *contact, *identity], env=environment())
    verify_source()
    print('受信任证书申请成功。', flush=True)


def process_tick(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text.rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def pause():
    if not HOME.exists():
        return
    marker = HOME / 'paused'
    marker.touch(mode=0o600)
    error = None
    # The renewal worker owns a separate session, so its Certbot children stop too.
    try:
        record = json.loads((HOME / 'renew.lock').read_text())
        pid, tick = int(record['pid']), record['start']
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        expected = [str(ROOT / 'scripts/certificate.py').encode(), b'renew']
        if process_tick(pid) == tick and argv[1:3] == expected and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(50):
                if process_tick(pid) != tick:
                    break
                time.sleep(.1)
            if process_tick(pid) == tick:
                os.killpg(pid, signal.SIGKILL)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    with cron_lock():
        if shutil.which('crontab'):
            try:
                current = cron_read()
                kept = [line for line in current.splitlines() if not line.endswith((MARK, RETRY_MARK))]
                if len(kept) != len(current.splitlines()):
                    if kept:
                        run(['crontab', '-'], input='\n'.join(kept) + '\n', text=True)
                    else:
                        run(['crontab', '-r'])
            except (OSError, ValueError, subprocess.SubprocessError) as failure:
                error = failure
    (HOME / 'retry-at').unlink(missing_ok=True)
    print('自动续期已暂停，当前证书和节点凭据保留。', flush=True)
    if error:
        raise ValueError('续期已暂停，但删除 cron 登记失败：' + str(error))


def renew():
    binary = tool()
    run(base(binary) + ['renew', '--cert-name', 'ezm-node', '--reuse-key', '--force-renewal', '--quiet'],
        env=environment(), capture_output=True, text=True)
    verify_source()


def ready(cert, key, seconds=RENEW_BEFORE):
    if not cert.is_file() or not key.is_file():
        return False
    try:
        validate(cert, key, seconds)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def retry_at():
    try:
        return float((HOME / 'retry-at').read_text())
    except (OSError, ValueError):
        return 0


def clear_retry():
    with cron_lock():
        if shutil.which('crontab'):
            current = cron_read()
            kept = [line for line in current.splitlines() if not line.endswith(RETRY_MARK)]
            if len(kept) != len(current.splitlines()):
                if kept:
                    run(['crontab', '-'], input='\n'.join(kept) + '\n', text=True)
                else:
                    run(['crontab', '-r'])
    (HOME / 'retry-at').unlink(missing_ok=True)


def schedule_retry(error):
    if (HOME / 'paused').exists():
        return
    # Align to a cron minute; daily checks and repeated starts obey this deadline.
    deadline = (int(time.time()) // 60 + 60) * 60
    detail = '\n'.join(str(value) for value in (error, getattr(error, 'stdout', ''), getattr(error, 'stderr', '')) if value)
    match = re.search(r'retry after\s+(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)(?:Z|\s+UTC)', detail, re.I)
    if match:
        try:
            requested = datetime.strptime(match[1].replace('T', ' '), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
            deadline = max(deadline, requested)
        except ValueError:
            pass
    header = re.search(r'retry-after:\s*([^\r\n]+)', detail, re.I)
    if header:
        value = header[1].strip()
        try:
            requested = time.time() + int(value) if value.isdecimal() else parsedate_to_datetime(value).timestamp()
            deadline = max(deadline, requested)
        except (ValueError, TypeError, OverflowError):
            pass
    state = HOME / 'retry-at'
    staged = state.with_suffix('.tmp')
    staged.write_text(str(deadline))
    staged.chmod(0o600)
    staged.replace(state)
    with cron_lock():
        current = cron_read()
        command = ' '.join(shlex.quote(str(x)) for x in (sys.executable, ROOT / 'scripts/certificate.py', 'renew'))
        minute = datetime.fromtimestamp(deadline).minute
        line = f'{minute} * * * * umask 027; ' + command + ' >> ' + shlex.quote(str(ROOT / 'log/certs/renewal.log')) + ' 2>&1' + RETRY_MARK
        line = line.replace('%', r'\%')
        kept = [entry for entry in current.splitlines() if not entry.endswith(RETRY_MARK)]
        run(['crontab', '-'], input='\n'.join([*kept, line]) + '\n', text=True)
    print('续期失败，已安排约 1 小时后重试；证书机构要求更晚时按其时间等待。', file=sys.stderr, flush=True)
    if getattr(error, 'stderr', None):
        print(error.stderr.strip(), file=sys.stderr)


def identifier():
    values = set()
    for path in (ROOT / 'runtime').glob('*/config.json'):
        try:
            doc = json.loads(path.read_text())
            if not isinstance(doc, dict) or not isinstance(doc.get('_ezm', {}), dict):
                continue
            value = doc.get('_ezm', {}).get('server')
            if value:
                values.add(value)
        except (OSError, ValueError, TypeError):
            continue
    if len(values) == 1:
        return values.pop()
    if not values:
        result = subprocess.run(['openssl', 'x509', '-in', str(LIVE / 'fullchain.pem'), '-noout', '-ext', 'subjectAltName'],
                                capture_output=True, text=True, check=True)
        match = re.search(r'(?:IP Address:|DNS:)([^,\s]+)', result.stdout)
        if match:
            return match.group(1)
    raise ValueError('无法确定唯一节点地址，未重新申请证书')


def reissue(name):
    # A caller holds renew.lock. Keep the previous data until signing succeeds.
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reissue-', dir=HOME) as folder:
        backup = Path(folder) / 'config'
        if CONFIG.exists():
            CONFIG.rename(backup)
        try:
            issue(name, '')
        except BaseException:
            if CONFIG.exists():
                shutil.rmtree(CONFIG)
            if backup.exists():
                backup.rename(CONFIG)
            raise


def valid_renewal_config():
    try:
        parser = configparser.RawConfigParser(strict=False)
        parser.read_string('[certificate]\n' + (CONFIG / 'renewal/ezm-node.conf').read_text())
        return parser.get('renewalparams', 'authenticator', fallback='') == 'standalone'
    except (OSError, configparser.Error, UnicodeError):
        return False


def check_certificate():
    source_valid = ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0)
    managed = valid_renewal_config()
    if source_valid:
        if ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 3600):
            verify_source()
        if managed and ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem'):
            clear_retry()
            print('原始证书剩余超过 48 小时，无需申请。', flush=True)
            return
    deadline = retry_at()
    if time.time() < deadline:
        print('尚未到续期重试时间：' + str(datetime.fromtimestamp(deadline)), flush=True)
        return
    try:
        if not source_valid or not managed:
            print('原始证书或续期资料缺失、无效，重新申请证书；不更换节点凭据。', flush=True)
            reissue(identifier())
        else:
            renew()
        if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem'):
            raise ValueError('申请后的证书仍不足 48 小时，保留重试任务')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        schedule_retry(error)
        raise
    clear_retry()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['issue', 'schedule', 'renew', 'prepare-cron', 'pause', 'resume'])
    parser.add_argument('--identifier')
    parser.add_argument('--email', default='')
    args = parser.parse_args()
    if args.action == 'issue' and not args.identifier:
        parser.error('issue 需要 identifier')
    if args.action == 'prepare-cron':
        ensure_cron()
        return
    if args.action == 'pause':
        pause()
        return
    marker = HOME / 'paused'
    if args.action == 'renew' and marker.exists():
        return
    if args.action == 'resume' and not (CONFIG / 'renewal/ezm-node.conf').is_file():
        return  # Existing externally managed certificates remain externally managed.
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    (ROOT / 'log/certs').mkdir(mode=0o700, parents=True, exist_ok=True)
    with (HOME / 'renew.lock').open('a+') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'issue':
            issue(args.identifier, args.email)
        elif args.action == 'schedule':
            schedule()
        elif args.action == 'resume':
            marker.unlink(missing_ok=True)
            schedule()
            try:
                check_certificate()
            except (OSError, ValueError, subprocess.SubprocessError):
                if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0):
                    raise
                print('暂时沿用当前有效证书启动，续期将在后台重试。', file=sys.stderr)
            if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0):
                raise ValueError('当前证书不可用，已安排后台续期；成功后请重新执行 start。')
        else:
            if marker.exists():
                return
            if os.getpgrp() != os.getpid():
                os.setsid()
            lock.seek(0)
            lock.truncate()
            json.dump({'pid': os.getpid(), 'start': process_tick(os.getpid())}, lock)
            lock.flush()
            try:
                if not marker.exists():
                    check_certificate()
            finally:
                lock.seek(0)
                lock.truncate()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit('证书操作失败：' + str(error))
    finally:
        try:
            sys.path.insert(0, str(ROOT / 'scripts'))
            from service import information_access
            information_access()
        except OSError as error:
            print('证书日志读取权限设置失败：' + str(error), file=sys.stderr)
