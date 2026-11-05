#!/usr/bin/env bash
# No Python or external utilities are needed to enter this bootstrap.
run_as_root() {
  if [[ "$EUID" == 0 ]]; then
    "$@"
  else
    command -v sudo >/dev/null || {
      echo '此操作需要 root 权限，请安装 sudo 或由 root 执行。' >&2
      return 1
    }
    sudo "$@"
  fi
}

CLIENT_DEPENDENCIES_PREPARED=0
client_python_ready() {
  if [[ "${1:-ssl}" == ssl ]]; then
    python3 -c 'import sys, ssl; sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1
  else
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1
  fi
}
client_packages() {
  local tool package
  CLIENT_PACKAGES=()
  for tool in "$@"; do
    command -v "$tool" >/dev/null && continue
    case "$tool" in
      ss|ip) [[ "$CLIENT_MANAGER" == apt-get ]] && package=iproute2 || package=iproute ;;
      flock) package=util-linux ;;
      ps) [[ "$CLIENT_MANAGER" == apt-get ]] && package=procps || package=procps-ng ;;
      awk) package=gawk ;;
      crontab) [[ "$CLIENT_MANAGER" == apt-get ]] && package=cron || package=cronie ;;
      cat|tail|tr|seq|sleep|nohup|date|mkdir|mktemp|mv|rm|uname) package=coreutils ;;
      *) package="$tool" ;;
    esac
    [[ " ${CLIENT_PACKAGES[*]} " == *" $package "* ]] || CLIENT_PACKAGES+=("$package")
  done
}
client_manager() {
  CLIENT_MANAGER=''
  local manager
  for manager in apt-get dnf yum; do
    if command -v "$manager" >/dev/null; then CLIENT_MANAGER="$manager"; break; fi
  done
}
client_install_packages() {
  ((${#CLIENT_PACKAGES[@]})) || return 0
  [[ -n "$CLIENT_MANAGER" ]] || { echo '无法自动安装：需要 apt-get、dnf 或 yum。' >&2; return 1; }
  printf '正在安装客户端依赖：%s\n' "${CLIENT_PACKAGES[*]}"
  if [[ "$CLIENT_MANAGER" == apt-get ]]; then
    run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update || return $?
    run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${CLIENT_PACKAGES[@]}" || return $?
  else
    run_as_root "$CLIENT_MANAGER" install -y "${CLIENT_PACKAGES[@]}" || return $?
  fi
}
client_cron_running() {
  local proc name
  for proc in /proc/[0-9]*/comm; do
    [[ -r "$proc" ]] || continue
    IFS= read -r name < "$proc" || continue
    [[ "$name" == cron || "$name" == crond ]] && return 0
  done
  return 1
}
client_enable_cron() {
  client_cron_running && return 0
  local unit
  if command -v systemctl >/dev/null; then
    for unit in cron crond; do
      if run_as_root systemctl enable --now "$unit" && client_cron_running; then return 0; fi
    done
  fi
  if command -v service >/dev/null; then
    if command -v update-rc.d >/dev/null; then
      run_as_root update-rc.d cron defaults && run_as_root service cron start && client_cron_running && return 0
    elif command -v chkconfig >/dev/null; then
      run_as_root chkconfig crond on && run_as_root service crond start && client_cron_running && return 0
    fi
  fi
  echo '无法启用 cron，未登记开机自启；请检查系统服务管理器。' >&2
  return 1
}
client_dependencies() {
  local mode="${1:-check}" missing=() tool needs_install=0
  if [[ "$mode" == prepare && "$CLIENT_DEPENDENCIES_PREPARED" == 1 ]]; then return 0; fi
  # Reuse start preparation so boot follows future startup dependency changes.
  if [[ "$mode" == boot ]]; then client_dependencies prepare || return $?; fi
  local tools=(flock ps ss ip awk grep sed cat tr seq sleep nohup date mkdir mktemp mv rm)
  local needs_python=1 python_modules=basic
  case "$mode" in
    stop) tools=(flock ps ss grep sed tr sleep mkdir rm date); needs_python=0 ;;
    status) tools=(ss grep sed tr) ;;
    test) tools=(curl awk mkdir date ss grep sed tr) ;;
    terminal) tools=(flock grep mkdir date rm) ;;
    boot-control) tools=(flock grep mkdir date) ;;
    proxychains) tools=(flock mkdir mktemp mv rm cat) ;;
    service) tools=(flock mkdir mktemp mv rm tr cat seq sleep ip grep); python_modules=ssl ;;
    log) tools=(tail); needs_python=0 ;;
    help) tools=(cat); needs_python=0 ;;
    boot) tools=(crontab sleep mkdir); needs_python=0 ;;
    prepare) tools+=(curl uname); python_modules=ssl ;;
    *) echo "未知依赖检查模式：$mode" >&2; return 1 ;;
  esac
  for tool in "${tools[@]}"; do command -v "$tool" >/dev/null || missing+=("$tool"); done
  [[ "$needs_python" == 0 ]] || client_python_ready "$python_modules" || missing+=("Python 3.9+ ($python_modules)")
  if [[ "$mode" == prepare || "$mode" == test || "$mode" == service ]]; then
    if [[ ! -s /etc/ssl/certs/ca-certificates.crt && ! -s /etc/pki/tls/certs/ca-bundle.crt && ! -s /etc/ssl/cert.pem ]]; then
      needs_install=1
      missing+=(ca-certificates)
    fi
  fi
  if ((${#missing[@]} == 0)) && [[ "$needs_install" == 0 ]] && { [[ "$mode" != boot ]] || client_cron_running; }; then
    [[ "$mode" != prepare ]] || CLIENT_DEPENDENCIES_PREPARED=1
    return 0
  fi
  client_manager
  client_packages "${tools[@]}"
  [[ "$needs_python" == 0 ]] || client_python_ready "$python_modules" || CLIENT_PACKAGES+=(python3)
  [[ "$needs_install" == 0 ]] || CLIENT_PACKAGES+=(ca-certificates)
  client_install_packages || return $?
  [[ "$mode" != boot ]] || client_enable_cron || return $?
  for tool in "${tools[@]}"; do
    command -v "$tool" >/dev/null || { echo "安装后仍缺少 $tool。" >&2; return 1; }
  done
  [[ "$needs_python" == 0 ]] || client_python_ready "$python_modules" || { echo "需要可用的 Python 3.9+ ($python_modules)；当前软件源或 Python 环境不满足要求。" >&2; return 1; }
  if [[ "$mode" == prepare || "$mode" == test || "$mode" == service ]]; then
    [[ -s /etc/ssl/certs/ca-certificates.crt || -s /etc/pki/tls/certs/ca-bundle.crt || -s /etc/ssl/cert.pem ]] || {
      echo '系统 CA 证书仍不可用，请修复 ca-certificates。' >&2; return 1;
    }
  fi
  [[ "$mode" != boot ]] || client_cron_running || return 1
  [[ "$mode" != prepare ]] || CLIENT_DEPENDENCIES_PREPARED=1
  return 0
}
client_platform() {
  [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || { echo '仅支持 Linux x86_64。' >&2; return 1; }
  [[ "$ROOT" != *[[:space:]:]* ]] || { echo '客户端目录不能包含空白或冒号。' >&2; return 1; }
  "$ROOT/bin/mihomo_1.19.33" -v >/dev/null 2>&1 || { echo '核心无法执行，请检查项目完整性、系统兼容性及目录执行权限。' >&2; return 1; }
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  unset LD_PRELOAD HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
  unset _MIHOMO_PROXY_SNAPSHOT
  ROOT="${BASH_SOURCE[0]%/scripts/dependencies.sh}"
  client_dependencies "${1:-prepare}"
fi
