"""One port registry for nodes, subscription and local relay."""
from pathlib import Path
from service import ROOT, SPECS, user_config_access

SUBSCRIPTION_PORT = 443
DEFAULTS = {p: 11801 + i for i, p in enumerate(SPECS)} | {'local_relay': 0}


def load(directory=None, base_port=None, write_defaults=True):
    directory = Path(directory) if directory is not None else ROOT / 'config/ports'
    if directory.exists() and not directory.is_dir():
        raise ValueError('端口配置必须是目录')
    defaults = dict(DEFAULTS)
    if base_port is not None:
        if not 1024 <= base_port <= 65536 - len(SPECS):
            raise ValueError('连续端口范围必须在 1024—65535 内')
        defaults.update({p: base_port + i for i, p in enumerate(SPECS)})
    values, missing = {'subscription': SUBSCRIPTION_PORT}, []
    for name, default in defaults.items():
        path = directory / (name + '.port')
        raw = path.read_text().strip() if path.exists() else ''
        if base_port is not None and name in SPECS:
            raw = str(default)
        if not raw:
            raw = str(default)
            missing.append(path)
        minimum = 0 if name == 'local_relay' else 1024
        if not raw.isascii() or not raw.isdecimal() or not minimum <= int(raw) <= 65535:
            raise ValueError(f'{name}：端口文件应填写 {minimum}–65535 的整数')
        values[name] = int(raw)
    nonzero = [value for value in values.values() if value]
    if len(set(nonzero)) != len(nonzero):
        raise ValueError('协议端口、订阅端口和非零本机中继端口不能重复')
    # No defaults are written until every port and conflict has been validated.
    if write_defaults and missing:
        from outbound import atomic_write
        directory.mkdir(parents=True, exist_ok=True)
        for path in missing:
            atomic_write(path, (str(values[path.stem]) + '\n').encode(), 0o600)
            user_config_access(path, 0o600)
    return values
