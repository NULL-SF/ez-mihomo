# 仅使用 Bash 内建命令，旧 proxychains 配置损坏时也可解除本项目的预加载。
_mihomo_remove_preload() {
  local root="$1" kept="" part found=0
  local parts=()
  IFS=$' :\t' read -ra parts <<< "${LD_PRELOAD:-}"
  for part in "${parts[@]}"; do
    if [[ "$part" == "${root}/lib/libproxychains4.so" ]]; then
      found=1
    elif [[ -n "$part" ]]; then
      kept="${kept:+${kept}:}${part}"
    fi
  done
  if [[ "$found" == 1 ]]; then
    if [[ -n "$kept" ]]; then
      export LD_PRELOAD="$kept"
    else
      unset LD_PRELOAD
    fi
  fi
  if [[ "$found" == 1 || "${PROXYCHAINS_CONF_FILE:-}" == "${root}/runtime/proxychains.conf" || "${PROXYCHAINS_CONF_FILE:-}" == "${root}/tmp/proxychains.conf" ]]; then
    unset PROXYCHAINS_CONF_FILE PROXYCHAINS_QUIET_MODE
  fi
  return 0
}

# 只读加载统一端口；缺失、空白或非法时回退，状态查询不写文件。
_mihomo_load_ports() {
  local root="$1" value="" file
  PROXY_PORT_DEFAULTED=0
  file="$root/config/port.txt"
  if [[ -e "$file" && ( ! -f "$file" || ! -r "$file" ) ]]; then
    printf '端口配置不是可读的普通文件：%s\n' "$file" >&2
    return 1
  fi
  [[ ! -r "$file" ]] || value=$(<"$file")
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ ! "$value" =~ ^[0-9]{1,5}$ ]] || (( 10#$value < 1 || 10#$value > 65535 )); then
    value=10808
    PROXY_PORT_DEFAULTED=1
  fi
  PROXY_PORT=$((10#$value))
}

# 启动/切换/终端接入时保存默认回退值；有效配置保持不变。
_mihomo_ensure_ports() {
  local root="$1" temporary
  _mihomo_load_ports "$root" || return 1
  if [[ "$PROXY_PORT_DEFAULTED" == 1 ]]; then
    mkdir -p "$root/config" || return 1
    temporary=$(mktemp "$root/config/.port.XXXXXX") || return 1
    if ! printf '%s\n' "$PROXY_PORT" > "$temporary" || ! mv -f -- "$temporary" "$root/config/port.txt"; then
      rm -f -- "$temporary"
      return 1
    fi
    printf '统一代理端口已写入 config/port.txt：%s。\n' "$PROXY_PORT" >&2
  fi
}

# 检查本项目核心与统一入口。
_mihomo_check_running_ports() {
  _mihomo_check_ports ready "$1" "$2"
}

_mihomo_check_available_ports() {
  _mihomo_check_ports available "$1" "$2"
}

_mihomo_check_ports() {
  python3 -B - "$@" <<'PY_PORT_CHECK'
#!/usr/bin/env python3
"""检查本项目进程拥有的监听套接字，或在停止旧进程前预检新端口。"""
import json
from pathlib import Path
import socket
import sys


def owned_ports(root):
    pid = int((root / 'runtime/mihomo.pid').read_text())
    proc = Path(f'/proc/{pid}')
    command = (proc / 'cmdline').read_bytes().split(b'\0')
    expected = [str(root / 'bin/mihomo_1.19.33'), '-d', str(root / 'runtime'), '-f', str(root / 'runtime/mihomo.json')]
    if command != [arg.encode() for arg in expected] + [b'']:
        raise ValueError('未找到本项目正在运行的核心')
    inodes = set()
    for fd in (proc / 'fd').iterdir():
        try:
            target = fd.readlink().as_posix()
        except FileNotFoundError:
            continue  # 核心可能正在关闭另一个连接。
        if target.startswith('socket:['):
            inodes.add(target[8:-1])
    ports = {'tcp': set(), 'udp': set()}
    for network in ports:
        for row in (proc / f'net/{network}').read_text().splitlines()[1:]:
            fields = row.split()
            host, port = fields[1].split(':')
            bound = fields[3] == ('0A' if network == 'tcp' else '07')
            if bound and host == '0100007F' and fields[9] in inodes:
                ports[network].add(int(port, 16))
    return ports


def main():
    mode, directory, port = sys.argv[1:]
    root = Path(directory)
    expected = {'socks': int(port)}
    if mode == 'ready':
        ports = owned_ports(root)
        cfg = json.loads((root / 'runtime/mihomo.json').read_text())
        if cfg.get('mixed-port') != expected['socks'] or cfg.get('bind-address') != '127.0.0.1' or cfg.get('allow-lan') is not False:
            raise ValueError('运行入口与当前配置不一致，请择机重启后接入')
        if not set(expected.values()) <= ports['tcp']:
            raise ValueError('统一代理端口未由本项目核心监听')
    elif mode == 'available':
        try:
            ports = owned_ports(root)
        except (OSError, ValueError, IndexError):
            ports = {'tcp': set(), 'udp': set()}
        port = expected['socks']
        for network, socket_type in (('tcp', socket.SOCK_STREAM), ('udp', socket.SOCK_DGRAM)):
            if port in ports[network]:
                continue
            try:
                with socket.socket(socket.AF_INET, socket_type) as sock:
                    if socket_type == socket.SOCK_STREAM:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('127.0.0.1', port))
                    if socket_type == socket.SOCK_STREAM:
                        sock.listen(1)
            except OSError as exc:
                raise ValueError(f'SOCKS / HTTP 混合代理 {network.upper()} 端口 127.0.0.1:{port} 不可用：{exc.strerror}；未停止现有服务') from None
    else:
        raise ValueError('未知端口检查模式')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        print(f'代理入口检查失败：{error}', file=sys.stderr)
        sys.exit(1)
PY_PORT_CHECK
}
