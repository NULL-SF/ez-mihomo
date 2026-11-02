#!/usr/bin/env bash
# Shell-only bootstrap: it must work before Python or flock is installed.
select_python() {
  local candidate modules="${1:-ssl}"
  PYTHON=''
  for candidate in python3 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10; do
    command -v "$candidate" >/dev/null || continue
    if "$candidate" -c 'import sys; __import__("ssl") if sys.argv[1] == "ssl" else None; sys.exit(0 if sys.version_info >= (3,10) else 1)' "$modules" >/dev/null 2>&1; then
      PYTHON="$(command -v "$candidate")"
      return 0
    fi
  done
  return 1
}
require_python() {
  select_python "${1:-ssl}" || {
    echo '需要 Python 3.10+；请先执行 sudo bash ./server.sh start 自动准备依赖（root 可省略 sudo）。' >&2
    return 1
  }
}
prepare_dependencies() {
  local profile="${1:-runtime}" manager packages=() command_name ca_file ca_ready=0
  local tools=(openssl flock ip mkdir)
  case "$profile" in
    runtime) ;;
    boot) tools=(flock grep mkdir sleep) ;;
    *) echo "未知依赖准备模式：$profile" >&2; return 1 ;;
  esac
  if command -v apt-get >/dev/null; then manager=apt-get
  elif command -v dnf >/dev/null; then manager=dnf
  elif command -v yum >/dev/null; then manager=yum
  else manager=''; fi
  for command_name in "${tools[@]}"; do
    command -v "$command_name" >/dev/null && continue
    case "$command_name" in
      flock) packages+=(util-linux) ;;
      mkdir|sleep) [[ " ${packages[*]} " == *" coreutils "* ]] || packages+=(coreutils) ;;
      ip) [[ "$manager" == apt-get ]] && packages+=(iproute2) || packages+=(iproute) ;;
      *) packages+=("$command_name") ;;
    esac
  done
  if [[ "$profile" == runtime ]]; then
    for ca_file in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
      [[ ! -s "$ca_file" ]] || ca_ready=1
    done
    [[ "$ca_ready" == 1 ]] || packages+=(ca-certificates)
  fi
  if ! select_python; then
    if [[ "$manager" == apt-get ]]; then packages+=(python3)
    else packages+=(python3.11); fi
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
  if [[ "$profile" == runtime ]]; then
    local ca_verified=0
    for ca_file in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
      [[ ! -s "$ca_file" ]] || ca_verified=1
    done
    [[ "$ca_verified" == 1 ]] || { echo '系统 CA 证书仍不可用，请修复 ca-certificates。' >&2; return 1; }
  fi
  require_python || return $?
  for command_name in "${tools[@]}"; do
    command -v "$command_name" >/dev/null || { echo "依赖安装后仍缺少 $command_name。" >&2; return 1; }
  done
}

# Read-only commands and stop need only their own tools; no package installation.
require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || {
      printf '缺少命令 %s；请安装对应系统组件后重试。\n' "$tool" >&2
      return 1
    }
  done
}

# Certbot's project-local environment is only needed when the system Certbot
# cannot be reused. PYTHON is the exact interpreter chosen by the caller.
prepare_python_venv() {
  "$PYTHON" -c 'import venv' >/dev/null 2>&1 && return 0
  local version package
  version="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" || return $?
  if command -v apt-get >/dev/null; then
    package="python${version}-venv"
    DEBIAN_FRONTEND=noninteractive apt-get update || return $?
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$package" || return $?
  elif command -v dnf >/dev/null; then
    dnf install -y "python${version}" || return $?
  elif command -v yum >/dev/null; then
    yum install -y "python${version}" || return $?
  else
    echo '无法自动安装 Python venv，请手动准备对应模块。' >&2; return 1
  fi
  "$PYTHON" -c 'import venv' >/dev/null 2>&1 || { echo '安装后 Python 仍缺少 venv 模块。' >&2; return 1; }
}
