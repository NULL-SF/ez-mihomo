#!/usr/bin/env bash
# Shell-only bootstrap: it must work before Python or flock is installed.
# Elevate package installation only, preserving the management user's identity.
install_system_packages() {
  local manager package
  local packages=()
  if command -v apt-get >/dev/null; then manager=apt-get
  elif command -v dnf >/dev/null; then manager=dnf
  elif command -v yum >/dev/null; then manager=yum
  else echo '无法自动安装依赖：需要 apt-get、dnf 或 yum。' >&2; return 1; fi
  for package in "$@"; do
    case "$package" in
      cat|tail|mkdir|sleep|uname) package=coreutils ;;
      flock) package=util-linux ;;
      ip) [[ "$manager" == apt-get ]] && package=iproute2 || package=iproute ;;
      python) [[ "$manager" == apt-get ]] && package=python3 || package=python3.11 ;;
    esac
    [[ " ${packages[*]} " == *" $package "* ]] || packages+=("$package")
  done
  printf '正在安装缺少的依赖：%s\n' "${packages[*]}"
  if [[ "$manager" == apt-get ]]; then
    sudo env DEBIAN_FRONTEND=noninteractive apt-get update || return $?
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}" || return $?
  else
    sudo "$manager" install -y "${packages[@]}" || return $?
  fi
}
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
  local modules="${1:-ssl}"
  select_python "$modules" && return 0
  install_system_packages python || return $?
  select_python "$modules" || {
    echo '安装后仍需要可用的 Python 3.10+，请检查软件源和 Python 环境。' >&2
    return 1
  }
}
prepare_dependencies() {
  local profile="${1:-runtime}" ca_file ca_ready=0
  local tools=(uname openssl flock ip mkdir)
  case "$profile" in
    runtime) ;;
    boot)
      prepare_dependencies runtime || return $?
      require_tools flock grep mkdir sleep
      return $?
      ;;
    *) echo "未知依赖准备模式：$profile" >&2; return 1 ;;
  esac
  require_tools "${tools[@]}" || return $?
  require_python || return $?
  for ca_file in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
    [[ ! -s "$ca_file" ]] || ca_ready=1
  done
  if [[ "$ca_ready" == 0 ]]; then
    install_system_packages ca-certificates || return $?
    for ca_file in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
      [[ ! -s "$ca_file" ]] || ca_ready=1
    done
    [[ "$ca_ready" == 1 ]] || { echo '系统 CA 证书仍不可用，请修复 ca-certificates。' >&2; return 1; }
  fi
}

# Install only tools required by the requested command.
require_tools() {
  local tool missing=()
  for tool in "$@"; do
    command -v "$tool" >/dev/null || missing+=("$tool")
  done
  ((${#missing[@]})) || return 0
  install_system_packages "${missing[@]}" || return $?
  for tool in "${missing[@]}"; do
    command -v "$tool" >/dev/null || {
      printf '安装后仍缺少命令 %s。\n' "$tool" >&2
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
    sudo env DEBIAN_FRONTEND=noninteractive apt-get update || return $?
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "$package" || return $?
  elif command -v dnf >/dev/null; then
    sudo dnf install -y "python${version}" || return $?
  elif command -v yum >/dev/null; then
    sudo yum install -y "python${version}" || return $?
  else
    echo '无法自动安装 Python venv，请手动准备对应模块。' >&2; return 1
  fi
  "$PYTHON" -c 'import venv' >/dev/null 2>&1 || { echo '安装后 Python 仍缺少 venv 模块。' >&2; return 1; }
}
