#!/usr/bin/env bash
# VPS 节点管理入口：mihomo 配置，版本固定，状态与配置分开存放。
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
# Do not let a client ProxyChains preload redirect server initialization probes.
unset LD_PRELOAD
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$ROOT/scripts/dependencies.sh"
PYTHON=''
cmd="${1:-help}"
if (($#)); then shift; fi
case "$cmd" in
  start|stop|restart|status|links|boot|help|-h|--help)
    (($# == 0)) || { echo "$cmd 不接受额外参数" >&2; exit 1; } ;;
esac
lock_management() {
  command -v flock >/dev/null || { echo '缺少 flock，请先执行 sudo bash ./server.sh start 准备依赖。' >&2; return 1; }
  mkdir -p "$ROOT/runtime"
  exec 9>"$ROOT/runtime/manage.lock"
  flock -w 30 9
}
boot_menu() {
  local current='' had_crontab=0 choice state=关
  if command -v crontab >/dev/null; then
    had_crontab=1
    if ! current="$(LC_ALL=C crontab -l 2>&1)"; then
      if [[ "$current" == *'no crontab for '* ]]; then current=''; had_crontab=0
      else printf '读取 crontab 失败，未修改：%s\n' "$current" >&2; return 1; fi
    fi
  fi
  if printf '%s\n' "$current" | grep -q ' # ezm-server-proxy-boot$'; then state=开; fi
  printf '开机自启：%s\n  1) 开启\n  2) 关闭\n' "$state"
  read -r -p '输入序号：' choice || { echo '已取消，未修改开机自启。'; return 0; }
  case "$choice" in
    1|on|开启) choice=on ;;
    2|off|关闭) choice=off ;;
    *) echo '输入无效，未修改开机自启。' >&2; return 1 ;;
  esac
  if [[ "$choice" == on ]]; then
    prepare_dependencies || return $?
  fi
  lock_management
  if [[ "$choice" == on ]]; then
    "$PYTHON" "$ROOT/scripts/certificate.py" prepare-cron 9>&- || return 1
  fi
  # Share the runtime directory lock with certificate cron updates, without a new file.
  exec 8<"$ROOT/runtime"
  flock -w 30 8 || return 1
  current=''; had_crontab=0
  if command -v crontab >/dev/null; then
    had_crontab=1
    if ! current="$(LC_ALL=C crontab -l 2>&1)"; then
      if [[ "$current" == *'no crontab for '* ]]; then current=''; had_crontab=0
      else printf '读取 crontab 失败，未修改：%s\n' "$current" >&2; return 1; fi
    fi
  fi
  current="$(printf '%s\n' "$current" | grep -v ' # ezm-server-proxy-boot$' || true)"
  if [[ "$choice" == on ]]; then
    local entry
    entry="$("$PYTHON" - "$ROOT" <<'PYCRON'
import shlex,sys
root=sys.argv[1]
if '\n' in root or '\r' in root:sys.exit('项目路径含换行符，无法登记 crontab。')
q=shlex.quote
line='@reboot sleep 20; mkdir -p '+q(root+'/log')+'; PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /bin/bash '+q(root+'/server.sh')+' start >> '+q(root+'/log/boot.log')+' 2>&1 < /dev/null # ezm-server-proxy-boot'
print(line.replace('%',r'\%'))
PYCRON
)" || return 1
    { [[ -z "$current" ]] || printf '%s\n' "$current"; printf '%s\n' "$entry"; } | crontab - || return 1
    echo '开机自启已开启，下次开机会启动七种协议服务；当前监听不变。'
  else
    if [[ -n "$current" ]]; then printf '%s\n' "$current" | crontab - || return 1
    elif [[ "$had_crontab" == 1 ]]; then crontab -r || return 1; fi
    echo '开机自启已关闭，当前监听不变。'
  fi
}

prepare_firewall() {
  if ! command -v ufw >/dev/null; then
    echo '未安装 UFW，跳过关闭 UFW。'
    return 0
  fi
  [[ "$(id -u)" == 0 ]] || {
    echo '关闭 UFW 需要 root 权限，请使用 sudo bash ./server.sh start（root 可省略 sudo）。' >&2
    return 1
  }
  if ufw disable; then
    echo 'UFW 已关闭；其他防火墙和云安全组不作修改。'
  else
    echo '关闭 UFW 失败，未继续安装 cron 或启动服务。' >&2
    return 1
  fi
}
start_services() {
  "$PYTHON" "$ROOT/scripts/certificate.py" prepare-cron 9>&- || return $?
  "$PYTHON" "$ROOT/scripts/configure.py" 9>&- || return $?
  "$PYTHON" "$ROOT/scripts/certificate.py" resume 9>&- || return $?
  if "$PYTHON" "$ROOT/scripts/service.py" start all 9>&-; then
    return 0
  else
    local result=$?
    "$PYTHON" "$ROOT/scripts/certificate.py" pause 9>&- || true
    return "$result"
  fi
}
stop_server() {
  local renew_result=0
  "$PYTHON" "$ROOT/scripts/certificate.py" pause 9>&- || renew_result=$?
  "$PYTHON" "$ROOT/scripts/service.py" stop all 9>&- || return $?
  return "$renew_result"
}
case "$cmd" in
  start) prepare_firewall; prepare_dependencies; lock_management; start_services ;;
  stop) require_python; lock_management; stop_server ;;
  restart) require_python; lock_management; stop_server; prepare_firewall; prepare_dependencies; start_services ;;
  status) require_python; lock_management; "$PYTHON" "$ROOT/scripts/service.py" status all 9>&- ;;
  boot) boot_menu ;;
  log)
    (($# == 0)) || { echo 'log 不接受参数，直接执行 sudo bash ./server.sh log（root 可省略 sudo）。' >&2; exit 1; }
    echo '选择要查看的日志：'
    echo '  0) 全部'
    echo '  1) VLESS'
    echo '  2) VMess'
    echo '  3) Hysteria2'
    echo '  4) Trojan'
    echo '  5) Shadowsocks'
    echo '  6) TUIC'
    echo '  7) AnyTLS'
    echo '  8) 证书续期'
    read -r -p '输入序号 [0]：' choice || { echo '已取消。'; exit 0; }
    protocols=(vless vmess hysteria2 trojan shadowsocks tuic anytls certificate)
    case "${choice:-0}" in
      0) selected=("${protocols[@]}") ;;
      [1-8]) selected=("${protocols[choice-1]}") ;;
      *) echo '输入无效，未查看日志。' >&2; exit 1 ;;
    esac
    log_files=()
    for protocol in "${selected[@]}"; do
      [[ ! -f "$ROOT/log/$protocol.log" ]] || log_files+=("$ROOT/log/$protocol.log")
    done
    ((${#log_files[@]})) || { echo '所选服务暂无运行日志，请先启动服务。' >&2; exit 1; }
    exec tail -v -n 50 -F -- "${log_files[@]}"
    ;;
  links)
    echo '选择要查看的节点链接：'
    echo '  0) 全部'
    echo '  1) VLESS'
    echo '  2) VMess'
    echo '  3) Hysteria2'
    echo '  4) Trojan'
    echo '  5) Shadowsocks'
    echo '  6) TUIC'
    echo '  7) AnyTLS'
    read -r -p '输入序号 [0]：' choice || { echo '已取消。'; exit 0; }
    protocols=(vless vmess hysteria2 trojan shadowsocks tuic anytls)
    case "${choice:-0}" in
      0) selected=all ;;
      [1-7]) selected="${protocols[choice-1]}" ;;
      *) echo '输入无效，未查看链接。' >&2; exit 1 ;;
    esac
    require_python
    lock_management
    "$PYTHON" - "$ROOT" "$selected" 9>&- <<'PYLINKS'
import sys
from pathlib import Path
root=Path(sys.argv[1]);selected=sys.argv[2]
sys.path.insert(0,str(root/'scripts'))
from service import SPECS
if selected!='all' and selected not in SPECS:
    sys.exit('未知协议。')
protocols=SPECS if selected=='all' else (selected,)
try:
    values=[]
    for protocol in protocols:
        value=(root/'output/links'/(protocol+'.txt')).read_text().strip()
        if not value or len(value.splitlines())!=1:
            raise ValueError(protocol+' 的链接文件为空或不是单行')
        values.append(value)
except (OSError, ValueError) as error:
    sys.exit('链接不可用，请检查是否已生成配置：'+str(error))
sys.stdout.write('\n'.join(values)+'\n')
PYLINKS
    ;;
  help|-h|--help)
    cat <<'HELP'
用法：sudo bash ./server.sh <命令>
root 用户可省略 sudo。

  start    启动
  stop     停止
  restart  重启
  status   查看状态
  links    查看节点链接
  log      持续查看日志
  boot     设置开机自启
HELP
    ;;
  *) printf '未知命令：%s\n' "$cmd" >&2; exit 1 ;;
esac
