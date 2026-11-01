#!/usr/bin/env bash
# Shell-only bootstrap: it must work before Python or flock is installed.
select_python() {
  local candidate
  PYTHON=''
  for candidate in python3 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10; do
    command -v "$candidate" >/dev/null || continue
    if "$candidate" -c 'import sys, ssl; sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      PYTHON="$(command -v "$candidate")"
      return 0
    fi
  done
  return 1
}
require_python() {
  select_python || {
    echo '需要 Python 3.10+；请先执行 sudo bash ./server.sh start 自动准备依赖（root 可省略 sudo）。' >&2
    return 1
  }
}
prepare_dependencies() {
  local manager packages=() command_name version ca_file ca_ready=0
  if command -v apt-get >/dev/null; then manager=apt-get
  elif command -v dnf >/dev/null; then manager=dnf
  elif command -v yum >/dev/null; then manager=yum
  else manager=''; fi
  for command_name in curl openssl flock crontab ip; do
    command -v "$command_name" >/dev/null && continue
    case "$command_name" in
      flock) packages+=(util-linux) ;;
      ip)
        if [[ "$manager" == apt-get ]]; then packages+=(iproute2); else packages+=(iproute); fi ;;
      crontab)
        if [[ "$manager" == apt-get ]]; then packages+=(cron); else packages+=(cronie); fi ;;
      *) packages+=("$command_name") ;;
    esac
  done
  for ca_file in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
    [[ ! -s "$ca_file" ]] || ca_ready=1
  done
  [[ "$ca_ready" == 1 ]] || packages+=(ca-certificates)
  if ! select_python; then
    if [[ "$manager" == apt-get ]]; then packages+=(python3 python3-venv)
    else packages+=(python3.11); fi
  elif ! "$PYTHON" -c 'import venv' >/dev/null 2>&1; then
    version="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [[ "$manager" == apt-get ]]; then
      if [[ "$(command -v python3 2>/dev/null || true)" == "$PYTHON" ]]; then packages+=(python3-venv)
      else packages+=("python${version}-venv"); fi
    else packages+=("python${version}"); fi
  fi
  if ((${#packages[@]})); then
    [[ "$(id -u)" == 0 ]] || { echo '缺少运行依赖，请以 root 或 sudo 运行 start 自动安装。' >&2; return 1; }
    [[ -n "$manager" ]] || { echo '无法自动安装依赖：需要 apt-get、dnf 或 yum，请按文档准备后重试。' >&2; return 1; }
    printf '正在安装缺少的运行依赖：%s\n' "${packages[*]}"
    if [[ "$manager" == apt-get ]]; then
      DEBIAN_FRONTEND=noninteractive apt-get update || return $?
      DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}" || return $?
    else
      "$manager" install -y "${packages[@]}" || return $?
    fi
  fi
  require_python || return $?
  "$PYTHON" -c 'import ssl, venv' >/dev/null 2>&1 || {
    echo 'Python 缺少 ssl 或 venv 模块，请安装对应组件后重试。' >&2; return 1;
  }
  for command_name in curl openssl flock crontab ip; do
    command -v "$command_name" >/dev/null || { echo "依赖安装后仍缺少 $command_name。" >&2; return 1; }
  done
}
