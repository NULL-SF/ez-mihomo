#!/usr/bin/env bash
# 读取当前线路，生成 mihomo 配置。无效链接保留监听并拒绝转发。
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
# 管理命令及核心使用自身配置，不继承终端的代理与预加载。
unset LD_PRELOAD HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
unset PROXYCHAINS_CONF_FILE PROXYCHAINS_QUIET_MODE _MIHOMO_PROXY_SNAPSHOT
ROOT="$(cd -- "${BASH_SOURCE[0]%/*}/.." && pwd -P)"
source "$ROOT/scripts/terminal-env.sh"
source "$ROOT/scripts/dependencies.sh"
# Managed starts have already prepared these dependencies in start_proxy.
if [[ "${1:-}" != --managed ]]; then client_dependencies service; fi
CORE="${ROOT}/bin/mihomo_1.19.33"
CONFIG="$ROOT/runtime/mihomo.json"
ACTIVE="$ROOT/config/protocol.activate"
umask 077
[[ -x "$CORE" ]] || { echo '缺少仓库自带的 bin/mihomo_1.19.33，请重新获取完整项目' >&2; exit 1; }
mkdir -p "$ROOT/runtime" "$ROOT/config/links"
# The interactive start already holds management.lock while waiting for readiness.
# A direct/cron start must acquire it itself and release it before exec.
STARTUP_LOCK=0
if (($# > 1)); then echo '后台入口参数无效。' >&2; exit 1; fi
case "${1:-}" in
  --managed) ;;
  '')
    exec 8>"$ROOT/runtime/management.lock"
    flock -w 45 8 || { echo '其他管理操作尚未结束，未启动。' >&2; exit 1; }
    STARTUP_LOCK=1
    ;;
  *) echo '后台入口参数无效。' >&2; exit 1 ;;
esac
exec 9>"$ROOT/runtime/service.lock"
flock -n 9 || exit 0
for protocol in vless vmess hysteria2 trojan shadowsocks tuic anytls; do
  [[ -e "$ROOT/config/links/$protocol.link" ]] || : > "$ROOT/config/links/$protocol.link"
done
_mihomo_ensure_ports "$ROOT"
profile="$(tr -d '[:space:]' < "$ACTIVE" 2>/dev/null || true)"
case "$profile" in
  '') profile=hysteria2 ;;
  reality) profile=vless ;;
  hy2|hysteria) profile=hysteria2 ;;
  ss) profile=shadowsocks ;;
  vless|vmess|hysteria2|trojan|shadowsocks|tuic|anytls) ;;
  *) echo '当前线路名称无效' >&2; exit 1 ;;
esac
_mihomo_write_text "$ACTIVE" "$profile"
_mihomo_check_available_ports "$ROOT" "$PROXY_PORT"
_mihomo_write_text "$ROOT/runtime/mihomo.pid" "$$"
trap 'rm -f "$ROOT/runtime/mihomo.pid"' EXIT
for _ in $(seq 1 30); do
  if ip route show default 2>/dev/null | grep . >/dev/null; then break; fi
  sleep 1
done
export MIHOMO_ROOT="$ROOT" MIHOMO_PROFILE="$profile" MIHOMO_PORT="$PROXY_PORT"
MIHOMO_LINK="$(cat -- "$ROOT/config/links/$profile.link")";export MIHOMO_LINK
reject_config() {
  python3 - "$ROOT" "$PROXY_PORT" "$CONFIG" <<'PY'
import sys,json,os
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/scripts')
from link_config import config
p=Path(sys.argv[3]);t=p.with_name(p.name+f'.{os.getpid()}.tmp')
t.write_text(json.dumps(config(None,sys.argv[2]))+'\n');t.chmod(0o600);t.replace(p)
PY
  echo '[mihomo] 链接不可用，保留混合入口并拒绝转发。' >&2
}
if ! python3 "$ROOT/scripts/link_config.py" "$CONFIG"; then reject_config; fi
# QUIC outbounds may build their TLS pools before mihomo applies global custom CAs.
# Supply the same explicitly imported CAs at process startup; keep system trust.
if python3 - "$CONFIG" "$ROOT/runtime/ca-bundle.pem" <<'PY_CA'
import json,os,ssl,sys
from pathlib import Path
certs=json.loads(Path(sys.argv[1]).read_text()).get('tls',{}).get('custom-certifactes',[])
if not certs:sys.exit(1)
system=os.environ.get('SSL_CERT_FILE') or ssl.get_default_verify_paths().cafile
base=Path(system).read_text() if system and Path(system).is_file() else ''
p=Path(sys.argv[2]);p.write_text(base+'\n'+'\n'.join(certs));p.chmod(0o600)
PY_CA
then export SSL_CERT_FILE="$ROOT/runtime/ca-bundle.pem"; fi
if ! "$CORE" -t -d "$ROOT/runtime" -f "$CONFIG" > "$ROOT/runtime/config-check.log" 2>&1; then
  echo '[mihomo] 核心配置校验失败，保留混合入口并拒绝转发。' >&2
  reject_config
fi
# Credentials are only needed for parsing, not as exported process environment.
unset MIHOMO_LINK MIHOMO_PROFILE MIHOMO_PORT MIHOMO_ROOT
printf '[mihomo] 启动线路 %s，混合入口 127.0.0.1:%s。\n' "$profile" "$PROXY_PORT" >&2
if [[ "$STARTUP_LOCK" == 1 ]]; then exec 8>&-; fi
python3 "$ROOT/scripts/permissions.py"
exec "$CORE" -d "$ROOT/runtime" -f "$CONFIG"
