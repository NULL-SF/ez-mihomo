#!/usr/bin/env bash
# VPS 节点管理入口：mihomo 配置，版本固定，状态与配置分开存放。
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1
# Management uses explicit egress configuration, not inherited shell proxies.
unset LD_PRELOAD HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
unset PROXYCHAINS_CONF_FILE PROXYCHAINS_QUIET_MODE
unset _MIHOMO_PROXY_SNAPSHOT
ROOT="${BASH_SOURCE[0]%/*}"
[[ "$ROOT" != "${BASH_SOURCE[0]}" ]] || ROOT=.
ROOT="$(cd -- "$ROOT" && pwd -P)"
source "$ROOT/scripts/dependencies.sh"
PYTHON=''
MANAGEMENT_LOCKED=0
cmd="${1:-help}"
if (($#)); then shift; fi
case "$cmd" in
  start|stop|restart|status|links|boot|help|-h|--help)
    (($# == 0)) || { echo "$cmd 不接受额外参数" >&2; exit 1; } ;;
esac
case "$cmd" in
  start|stop|restart|boot)
    [[ "$EUID" == 0 ]] || {
      printf '%s 需要 root 权限，请执行 sudo bash ./server.sh %s（root 用户可省略 sudo）。\n' "$cmd" "$cmd" >&2
      exit 1
    }
    ;;
esac
lock_management() {
  [[ "${MANAGEMENT_LOCKED:-0}" == 0 ]] || return 0
  require_tools flock mkdir || return $?
  mkdir -p "$ROOT/runtime"
  exec 9>"$ROOT/runtime/manage.lock"
  flock -w 30 9 || return $?
  MANAGEMENT_LOCKED=1
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
  if [[ "$current"$'\n' == *' # ezm-server-proxy-boot'$'\n'* ]]; then state=开; fi
  printf '开机自启：%s\n  1) 开启\n  2) 关闭\n' "$state"
  read -r -p '输入序号：' choice || { echo '已取消，未修改开机自启。'; return 0; }
  case "$choice" in
    1|on|开启) choice=on ;;
    2|off|关闭) choice=off ;;
    *) echo '输入无效，未修改开机自启。' >&2; return 1 ;;
  esac
  if [[ "$choice" == on ]]; then
    prepare_dependencies boot || return $?
  else
    require_tools grep || return $?
  fi
  lock_management
  if [[ "$choice" == on ]]; then
    "$PYTHON" "$ROOT/scripts/certificate.py" prepare-cron 9>&- || return 1
  fi
  # Share a per-user lock with both client boot and certificate cron updates.
  [[ -n "$PYTHON" ]] || require_python basic
  local cron_home
  cron_home="$("$PYTHON" -c 'import os,pwd; print(pwd.getpwuid(os.geteuid()).pw_dir)')" || return 1
  exec 8<"$cron_home"
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
    echo '开机自启已开启。已写入 crontab，下次开机会自动启动。'
  else
    if [[ -n "$current" ]]; then printf '%s\n' "$current" | crontab - || return 1
    elif [[ "$had_crontab" == 1 ]]; then crontab -r || return 1; fi
    echo '开机自启已关闭。已从 crontab 移除，下次开机不会自动启动。'
  fi
}

check_platform() {
  require_tools uname || return $?
  [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
    echo '本项目仅支持 Linux x86_64。' >&2; return 1;
  }
  [[ -x "$ROOT/bin/mihomo_1.19.33" ]] || {
    echo '缺少可执行的 bin/mihomo_1.19.33，请重新安装完整项目。' >&2; return 1;
  }
  "$ROOT/bin/mihomo_1.19.33" -v >/dev/null 2>&1 || {
    echo '核心无法在当前环境运行，请检查系统兼容性及目录执行权限。' >&2; return 1;
  }
}

prepare_firewall() {
  "$PYTHON" "$ROOT/scripts/firewall.py" start "$@" 9>&-
}
start_services() {
  prepare_dependencies
  check_platform
  lock_management
  prepare_firewall
  local certificate_checked=0
  "$PYTHON" "$ROOT/scripts/certificate.py" prepare-cron 9>&- || return $?
  # Validate or renew original certificates before validating node configurations.
  if [[ -f "$ROOT/runtime/certs/config/renewal/ezm-node.conf" ]]; then
    "$PYTHON" "$ROOT/scripts/certificate.py" resume 9>&- || return $?
    certificate_checked=1
  fi
  "$PYTHON" "$ROOT/scripts/configure.py" 9>&- || return $?
  prepare_firewall --quiet || return $?
  if [[ "$certificate_checked" == 0 ]]; then
    "$PYTHON" "$ROOT/scripts/certificate.py" resume 9>&- || return $?
  fi
  "$PYTHON" "$ROOT/scripts/subscription.py" preflight 9>&- || return $?
  if "$PYTHON" "$ROOT/scripts/service.py" start all 9>&-; then
    "$PYTHON" "$ROOT/scripts/subscription.py" start 9>&- || return $?
    prepare_firewall --quiet || return $?
    "$PYTHON" - "$ROOT" 9>&- <<'PYEXPORT' || return $?
import sys
sys.path.insert(0,sys.argv[1]+'/scripts')
from configure import export_links
export_links()
PYEXPORT
    return 0
  else
    local result=$?
    "$PYTHON" "$ROOT/scripts/certificate.py" pause 9>&- || true
    return "$result"
  fi
}
stop_server() {
  require_python
  lock_management
  local result=0
  "$PYTHON" "$ROOT/scripts/certificate.py" pause 9>&- || result=$?
  "$PYTHON" "$ROOT/scripts/subscription.py" stop 9>&- || result=$?
  "$PYTHON" "$ROOT/scripts/service.py" stop all 9>&- || result=$?
  "$PYTHON" "$ROOT/scripts/firewall.py" stop 9>&- || result=$?
  return "$result"
}
refresh_information_access() {
  local result=$?
  if [[ "$EUID" == 0 && -n "$PYTHON" ]]; then
    "$PYTHON" - "$ROOT" <<'PYACCESS' || echo '信息文件权限设置失败，请检查目录权限。' >&2
import sys
sys.path.insert(0,sys.argv[1]+'/scripts')
from service import information_access
information_access()
PYACCESS
  fi
  return "$result"
}
case "$cmd" in start|stop|restart|boot) trap refresh_information_access EXIT ;; esac
case "$cmd" in
  start) start_services ;;
  stop) stop_server ;;
  restart) stop_server; start_services ;;
  status)
    require_python
    "$PYTHON" "$ROOT/scripts/service.py" status all 9>&-
    "$PYTHON" "$ROOT/scripts/subscription.py" status 9>&-
    ;;
  boot) boot_menu ;;
  log)
    (($# == 0)) || { echo 'log 不接受参数，直接执行 bash ./server.sh log。' >&2; exit 1; }
    require_tools tail
    echo '选择要查看的日志：'
    echo '  0) 全部（节点、证书、订阅、开机启动）'
    echo '  1) VLESS'
    echo '  2) VMess'
    echo '  3) Hysteria2'
    echo '  4) Trojan'
    echo '  5) Shadowsocks'
    echo '  6) TUIC'
    echo '  7) AnyTLS'
    echo '  8) 证书申请与续期'
    echo '  9) 订阅服务'
    echo '  10) 开机启动'
    read -r -p '输入序号 [0]：' choice || { echo '已取消。'; exit 0; }
    protocols=(vless vmess hysteria2 trojan shadowsocks tuic anytls certificate subscription boot)
    case "${choice:-0}" in
      0) selected=("${protocols[@]}") ;;
      [1-9]|10) selected=("${protocols[choice-1]}") ;;
      *) echo '输入无效，未查看日志。' >&2; exit 1 ;;
    esac
    log_files=()
    for protocol in "${selected[@]}"; do
      if [[ "$protocol" == certificate ]]; then
        for file in "$ROOT/log/certs/renewal.log" "$ROOT/log/certs/letsencrypt.log"; do
          [[ ! -f "$file" ]] || log_files+=("$file")
        done
      else
        [[ ! -f "$ROOT/log/$protocol.log" ]] || log_files+=("$ROOT/log/$protocol.log")
      fi
    done
    ((${#log_files[@]})) || { echo '所选项目暂无日志。' >&2; exit 1; }
    exec tail -v -n 50 -F -- "${log_files[@]}"
    ;;
  links)
    echo '选择要查看的节点链接或订阅链接：'
    echo '  0) 全部节点链接'
    echo '  1) VLESS'
    echo '  2) VMess'
    echo '  3) Hysteria2'
    echo '  4) Trojan'
    echo '  5) Shadowsocks'
    echo '  6) TUIC'
    echo '  7) AnyTLS'
    echo '  8) 白名单订阅链接（名单内直连，其余代理）'
    echo '  9) 黑名单订阅链接（名单内代理，其余直连）'
    echo '  10) 全部订阅链接'
    read -r -p '输入序号 [0]：' choice || { echo '已取消。'; exit 0; }
    protocols=(vless vmess hysteria2 trojan shadowsocks tuic anytls subscriptions/whitelist subscriptions/blacklist)
    case "${choice:-0}" in
      0) selected=all ;;
      10) selected=subscriptions ;;
      [1-9]) selected="${protocols[choice-1]}" ;;
      *) echo '输入无效，未查看链接。' >&2; exit 1 ;;
    esac
    require_python
    "$PYTHON" - "$ROOT" "$selected" 9>&- <<'PYLINKS'
import sys
from pathlib import Path
root=Path(sys.argv[1]);selected=sys.argv[2]
sys.path.insert(0,str(root/'scripts'))
from service import SPECS
subscriptions=('subscriptions/whitelist','subscriptions/blacklist')
names=(*SPECS,*subscriptions)
if selected not in ('all','subscriptions') and selected not in names:
    sys.exit('未知协议。')
protocols=tuple(SPECS) if selected=='all' else subscriptions if selected=='subscriptions' else (selected,)
try:
    values=[]
    for protocol in protocols:
        value=(root/'output/links'/(protocol+'.link')).read_text().strip()
        if not value or len(value.splitlines())!=1:
            raise ValueError(protocol+' 的链接文件为空或不是单行')
        values.append(value)
except (OSError, ValueError) as error:
    sys.exit('链接不可用，请检查是否已生成配置：'+str(error))
sys.stdout.write('\n'.join(values)+'\n')
PYLINKS
    ;;
  help|-h|--help)
    require_tools cat
    cat <<'HELP'
用法：bash ./server.sh <命令>
start、stop、restart、boot 需要 root 权限，普通用户请在命令前加 sudo。

  start    启动
  stop     停止
  restart  重启
  status   查看状态
  links    查看节点链接或订阅链接
  log      持续查看日志
  boot     设置开机自启
HELP
    ;;
  *) printf '未知命令：%s\n' "$cmd" >&2; exit 1 ;;
esac
