#!/usr/bin/env python3
"""Obtain and renew the project's ACME certificate without restarting listeners."""
import argparse
import configparser
from contextlib import contextmanager
from datetime import datetime
import fcntl
import ipaddress
import hashlib
import json
import math
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
RENEW_BEFORE = 48 * 3600
CERTBOT_VERSION = '5.9.0'
SIGN_TIMEOUT = 300
REAPPLY_COOLDOWN = 59 * 60


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


@contextmanager
def cron_lock():
    # All projects for this UID share the same crontab; lock a stable inode.
    directory = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    fd = os.open(directory, os.O_RDONLY)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('其他定时任务设置尚未结束，未修改 crontab')
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


@contextmanager
def certificate_lock(nonblocking=False):
    # The project inode stays stable even if the certificate directory is removed.
    fd = os.open(ROOT, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            yield None
            return
        HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        current_lock = HOME / 'reapply.lock'
        with current_lock.open('a+') as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                yield None
                return
            yield lock
    finally:
        os.close(fd)


def cron_read():
    if not shutil.which('crontab'):
        raise ValueError('证书自动续期需要 crontab 和运行中的 cron 服务，请先安装并启用 cron。')
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
            run(['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'update'], env=env)
            run(['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', 'cron'], env=env)
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
        run(['bash', '-c', 'source "$1"; PYTHON="$2"; prepare_python_venv',
             'bash', ROOT / 'scripts/dependencies.sh', sys.executable])
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
                 '--disable-pip-version-check', 'pip==25.3', 'certbot==' + CERTBOT_VERSION], env=env, timeout=600)
        finally:
            bootstrap.unlink(missing_ok=True)
    return [str(venv / 'bin/python'), str(binary)]


def environment():
    env = dict(os.environ)
    for key in list(env):
        if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') or key in ('LD_PRELOAD', 'PROXYCHAINS_CONF_FILE', 'PROXYCHAINS_QUIET_MODE', '_MIHOMO_PROXY_SNAPSHOT'):
            env.pop(key)
    return env


def base(binary, config_dir=None):
    return [*binary, '--config-dir', str(config_dir or CONFIG), '--work-dir', str(HOME / 'work'),
            '--logs-dir', str(ROOT / 'log/certs'), '--non-interactive']


def validate(cert, key, seconds=3600):
    run(['openssl', 'x509', '-in', cert, '-noout', '-checkend', str(seconds)], capture_output=True)
    a = subprocess.check_output(['openssl', 'x509', '-in', cert, '-pubkey', '-noout'])
    b = subprocess.check_output(['openssl', 'pkey', '-in', key, '-passin', 'pass:', '-pubout'])
    if a != b:
        raise ValueError('新证书与私钥不匹配，保留当前证书。')


def verify_source():
    validate(LIVE / 'fullchain.pem', LIVE / 'privkey.pem')
    print('证书和私钥校验通过。', flush=True)


def set_check_schedule():
    (ROOT / 'log/certs').mkdir(mode=0o700, parents=True, exist_ok=True)
    command = ' '.join(shlex.quote(str(x)) for x in (sys.executable, ROOT / 'scripts/certificate.py', 'renew'))
    interval = '0 * * * *'
    line = interval + ' umask 027; ' + command + ' >> ' + shlex.quote(str(ROOT / 'log/certs/renewal.log')) + ' 2>&1' + MARK
    if '\n' in str(ROOT) or '\r' in str(ROOT):
        raise ValueError('项目路径含换行符，无法设置续期任务')
    line = line.replace('%', r'\%')
    with cron_lock():
        if not (HOME / 'apply.on').is_file():
            return False
        current = cron_read()
        others = [x for x in current.splitlines() if not x.endswith(MARK)]
        updated = '\n'.join([*others, line]) + '\n'
        if updated != current:
            run(['crontab', '-'], input=updated, text=True)
    return True


def schedule():
    ensure_cron()
    if not set_check_schedule():
        print('证书自动续期未启用，未登记检查任务。', flush=True)
        return
    print('证书自动检查任务已登记：每小时整点执行。', flush=True)


def issue(identifier, email, config_dir=None):
    if not (HOME / 'apply.on').is_file():
        raise InterruptedError('证书申请未启用')
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
    if not (HOME / 'apply.on').is_file():
        raise InterruptedError('证书申请已停止')
    result = run(base(binary, config_dir) + ['certonly', '--standalone', '--preferred-profile', 'shortlived',
                       '--cert-name', 'ezm-node', '--agree-tos', '--reuse-key', *contact, *identity],
                 env=environment(), timeout=SIGN_TIMEOUT, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end='', flush=True)
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr, flush=True)
    if config_dir is None:
        verify_source()
    else:
        staged_live = Path(config_dir) / 'live/ezm-node'
        validate(staged_live / 'fullchain.pem', staged_live / 'privkey.pem')
    print('受信任证书申请成功。', flush=True)


def process_tick(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text.rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def renewal_group_active(group):
    command = str(ROOT / 'scripts/certificate.py').encode()
    prefix = (str(HOME) + os.sep).encode()
    for process in Path('/proc').glob('[0-9]*'):
        try:
            text = (process / 'stat').read_text()
            fields = text[text.rfind(')') + 2:].split()
            if fields[0] == 'Z' or int(fields[2]) != group:
                continue
            argv = (process / 'cmdline').read_bytes().split(b'\0')
            if command in argv or any(arg.startswith(prefix) for arg in argv):
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def pause():
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    (HOME / 'apply.on').unlink(missing_ok=True)
    error = None
    # Validate the exact project command and session, even if its PID record was lost.
    workers = {}
    for process in Path('/proc').glob('[0-9]*'):
        try:
            argv = (process / 'cmdline').read_bytes().split(b'\0')
            command = str(ROOT / 'scripts/certificate.py').encode()
            if command not in argv:
                continue
            index = argv.index(command)
            if argv[index:] != [command, b'renew', b'']:
                continue
            pid = int(process.name)
            tick = process_tick(pid)
            if pid != os.getpid() and tick and os.getpgid(pid) == pid:
                workers[pid] = tick
        except (OSError, ValueError):
            continue
    for pid, tick in workers.items():
        try:
            if process_tick(pid) != tick or os.getpgid(pid) != pid:
                continue
            os.killpg(pid, signal.SIGTERM)
            for _ in range(50):
                if not renewal_group_active(pid):
                    break
                time.sleep(0.1)
            if renewal_group_active(pid):
                os.killpg(pid, signal.SIGKILL)
                for _ in range(20):
                    if not renewal_group_active(pid):
                        break
                    time.sleep(0.1)
                if renewal_group_active(pid):
                    error = ValueError('续期进程仍未退出，请检查系统进程状态')
        except ProcessLookupError:
            pass
    with cron_lock():
        if shutil.which('crontab'):
            try:
                current = cron_read()
                kept = [line for line in current.splitlines() if not line.endswith(MARK)]
                if len(kept) != len(current.splitlines()):
                    if kept:
                        run(['crontab', '-'], input='\n'.join(kept) + '\n', text=True)
                    else:
                        run(['crontab', '-r'])
            except (OSError, ValueError, subprocess.SubprocessError) as failure:
                error = failure
    print('已停止证书自动续期，现有证书、节点配置和链接凭据保留。', flush=True)
    if error:
        raise ValueError('证书检查已暂停，但未完全停止：' + str(error))


def renew():
    binary = tool()
    if not (HOME / 'apply.on').is_file():
        raise InterruptedError('证书申请已停止')
    run(base(binary) + ['renew', '--cert-name', 'ezm-node', '--reuse-key', '--force-renewal', '--quiet'],
        env=environment(), capture_output=True, text=True, timeout=SIGN_TIMEOUT)
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
        state = HOME / 'reapply.lock'
        if not state.is_file():
            return 0
        value = float(state.read_text())
        if not math.isfinite(value) or value < 0:
            return 0
        datetime.fromtimestamp(value)
        return value
    except (OSError, ValueError, OverflowError):
        return 0


def write_retry_at(deadline):
    # Caller holds certificate_lock. Never replace/unlink the locked inode.
    with (HOME / 'reapply.lock').open('r+') as state:
        state.write(str(deadline) + '\n')
        state.truncate()
        state.flush()
        os.fsync(state.fileno())


def finish_check():
    set_check_schedule()


def schedule_retry(error=None, announce=True):
    if not (HOME / 'apply.on').is_file():
        return False
    # Hourly checks and repeated starts obey this persisted deadline.
    deadline = time.time() + REAPPLY_COOLDOWN
    write_retry_at(deadline)
    scheduled = set_check_schedule()
    if scheduled and announce:
        print('申请失败，保留每小时整点检查；达到重试时间后再申请。', file=sys.stderr, flush=True)
        if getattr(error, 'stderr', None):
            print(error.stderr.strip(), file=sys.stderr)
    return scheduled


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
        try:
            result = subprocess.run(['openssl', 'x509', '-in', str(LIVE / 'fullchain.pem'), '-noout', '-ext', 'subjectAltName'],
                                    capture_output=True, text=True, check=True)
            match = re.search(r'(?:IP Address:|DNS:)([^,\s]+)', result.stdout)
            if match:
                return match.group(1)
        except (OSError, subprocess.SubprocessError):
            from configure import detect_server
            return detect_server()
    raise ValueError('无法确定唯一节点地址，未重新申请证书')


def recover_pending_reissue():
    # A process may die between the two directory renames. Restore old material.
    for folder in HOME.glob('.reissue-*'):
        if not folder.is_dir() or folder.is_symlink():
            continue
        backup = folder / 'previous'
        if not backup.exists():
            backup = folder / 'config'  # Older interrupted transaction layout.
        if backup.is_dir():
            if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0) or not valid_renewal_config():
                if CONFIG.exists():
                    shutil.rmtree(CONFIG)
                backup.rename(CONFIG)
        shutil.rmtree(folder)


def reissue(name):
    # Keep watched directories stable; only atomically replace their files.
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    recover_pending_reissue()
    with tempfile.TemporaryDirectory(prefix='.reissue-', dir=HOME) as folder:
        candidate = Path(folder) / 'candidate'
        issue(name, '', config_dir=candidate)
        if not (HOME / 'apply.on').is_file():
            raise InterruptedError('证书检查已暂停，未切换新证书')
        for path in (candidate / 'renewal').glob('*.conf'):
            path.write_text(path.read_text().replace(str(candidate), str(CONFIG)))
        # New archive names must not overwrite files referenced by the old live
        # symlinks while the certificate/key pair is being published.
        pattern = re.compile(r'(cert|chain|fullchain|privkey)(\d+)\.pem')
        archive = CONFIG / 'archive/ezm-node'
        offset = max((int(match[2]) for path in archive.glob('*.pem')
                      if (match := pattern.fullmatch(path.name))), default=0)
        staged_archive = candidate / 'archive/ezm-node'
        for path in list(staged_archive.glob('*.pem')):
            match = pattern.fullmatch(path.name)
            if match and offset:
                path.rename(path.with_name(f'{match[1]}{int(match[2]) + offset}.pem'))
        staged_live = candidate / 'live/ezm-node'
        for path in staged_live.iterdir():
            if path.is_symlink():
                target = path.readlink()
                match = pattern.fullmatch(target.name)
                if match and offset:
                    path.unlink()
                    path.symlink_to(target.with_name(f'{match[1]}{int(match[2]) + offset}.pem'))
        files = [path for path in candidate.rglob('*') if path.is_file() or path.is_symlink()]
        files.sort(key=lambda path: (path.is_relative_to(staged_live), str(path)))
        for path in files:
            destination = CONFIG / path.relative_to(candidate)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.replace(destination)


def valid_renewal_config():
    try:
        path = CONFIG / 'renewal/ezm-node.conf'
        if not path.is_file():
            return False
        parser = configparser.RawConfigParser(strict=False)
        parser.read_string('[certificate]\n' + path.read_text())
        return parser.get('renewalparams', 'authenticator', fallback='') == 'standalone'
    except (OSError, configparser.Error, UnicodeError):
        return False


def enable_application():
    (HOME / 'apply.on').touch(mode=0o600)


def check_certificate(requested=None):
    if not (HOME / 'apply.on').is_file():
        return
    source_valid = ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0)
    managed = valid_renewal_config()
    if source_valid:
        if ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 3600):
            verify_source()
        if managed and ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem'):
            finish_check()
            print('证书剩余有效期超过 48 小时，无需申请新证书。', flush=True)
            return
    deadline = retry_at()
    if time.time() < deadline:
        print('下次允许申请证书的时间：' + str(datetime.fromtimestamp(deadline)), flush=True)
        return
    try:
        # Persist the cooldown before contacting the CA, including on crashes.
        if not schedule_retry(announce=False) or not (HOME / 'apply.on').is_file():
            return
        if not source_valid or not managed:
            print('证书、私钥或续期配置缺失、无效，将重新申请；现有节点配置和链接不变。', flush=True)
            reissue(requested or identifier())
        else:
            renew()
        if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem'):
            raise ValueError('申请后的证书仍不足 48 小时，保留重试任务')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        schedule_retry(error)
        raise
    # Keep the same cooldown after a successful request as well.
    schedule_retry(announce=False)


def refresh_application_state():
    from service import SPECS, owned
    # Only hold the management lock during this decision, so stop can still
    # cancel a long-running certificate request after the decision completes.
    with (ROOT / 'runtime/manage.lock').open('a+') as management:
        try:
            fcntl.flock(management, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('节点管理操作正在执行，本次整点检查跳过。', flush=True)
            return False
        if not any(owned(protocol, report_permission=True) for protocol in SPECS):
            (HOME / 'apply.on').unlink(missing_ok=True)
            print('全部节点核心均未运行，已关闭证书自动续期；执行 start 可恢复。', flush=True)
        return True


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
    marker = HOME / 'apply.on'
    if args.action == 'renew':
        if marker.is_file() and not refresh_application_state():
            return  # A management operation is still running.
        if not marker.is_file():
            return
    if args.action == 'resume' and not (CONFIG / 'renewal/ezm-node.conf').is_file():
        return  # Existing externally managed certificates remain externally managed.
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    (ROOT / 'log/certs').mkdir(mode=0o700, parents=True, exist_ok=True)
    with certificate_lock(nonblocking=args.action == 'renew') as lock:
        if lock is None:
            print('已有证书任务正在执行，本次检查跳过。', flush=True)
            return
        if args.action == 'renew':
            if not marker.is_file():
                return
            if time.time() < retry_at():
                print('证书申请仍在等待期，本次整点检查跳过。', flush=True)
                return
            if os.getpgrp() != os.getpid():
                os.setsid()
        recover_pending_reissue()
        if args.action == 'issue':
            enable_application()
            check_certificate(requested=args.identifier)
            if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0):
                raise ValueError('当前没有可用证书，请稍后重新执行 start')
        elif args.action == 'schedule':
            schedule()
        elif args.action == 'resume':
            enable_application()
            schedule()
            try:
                check_certificate()
            except (OSError, ValueError, subprocess.SubprocessError):
                if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0):
                    raise
                print('暂时沿用当前有效证书启动；失败与重试情况请查看证书日志。', file=sys.stderr)
            if not ready(LIVE / 'fullchain.pem', LIVE / 'privkey.pem', 0):
                raise ValueError('当前证书不可用，请稍后重新执行 start。')
        else:
            if marker.is_file():
                check_certificate()


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
            print('项目文件权限设置失败：' + str(error), file=sys.stderr)
