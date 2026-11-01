#!/usr/bin/env bash
set -euo pipefail

# Download one self-contained project without requiring Git.
if (($# > 1)); then
  echo '用法：bash install.sh [安装目录]（默认 ./ezm-server 或 ./ezm-client）' >&2
  exit 1
fi
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo '仅支持 Linux x86_64。' >&2; exit 1;
}
printf '选择安装项目：\n  1) ezm-server 服务端\n  2) ezm-client 客户端\n'
while true; do
  if ! read -r -p '请选择 [1/2]：' choice; then
    echo '未选择，已取消安装。' >&2; exit 1
  fi
  case "$choice" in
    1) project=ezm-server; entry=server.sh; break ;;
    2) project=ezm-client; entry=client.sh; break ;;
    *) echo '请输入 1 或 2。' >&2 ;;
  esac
done

target="${1:-$PWD/$project}"
if [[ -L "$target" || ( -e "$target" && ! -d "$target" ) ]]; then
  echo "安装路径必须是目录：$target" >&2; exit 1
fi

packages=()
for tool in curl tar gzip; do
  command -v "$tool" >/dev/null || packages+=("$tool")
done
if [[ ! -s /etc/ssl/certs/ca-certificates.crt && ! -s /etc/pki/tls/certs/ca-bundle.crt && ! -s /etc/ssl/cert.pem ]]; then
  packages+=(ca-certificates)
fi
if ((${#packages[@]})); then
  [[ "$EUID" == 0 ]] || {
    echo "缺少下载组件：${packages[*]}；请先安装这些组件后重试。" >&2; exit 1;
  }
  if command -v apt-get >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null; then
    dnf install -y "${packages[@]}"
  elif command -v yum >/dev/null; then
    yum install -y "${packages[@]}"
  else
    echo "请先安装下载组件：${packages[*]}" >&2; exit 1
  fi
fi

mkdir -p -- "$(dirname -- "$target")"
parent="$(cd -- "$(dirname -- "$target")" && pwd -P)"
target="$parent/$(basename -- "$target")"
if [[ "$project" == ezm-client ]]; then
  [[ "$target" != *[[:space:]:]* ]] || {
    echo '客户端安装路径不能包含空白或冒号，ProxyChains 无法使用该路径。' >&2; exit 1;
  }
  missing=()
  for tool in bash python3 curl ss ip awk flock ps; do
    command -v "$tool" >/dev/null || missing+=("$tool")
  done
  if ((${#missing[@]})); then
    printf '缺少客户端运行组件：%s。请安装后重试。\n' "${missing[*]}" >&2
    exit 1
  fi
  if ! command -v crontab >/dev/null; then
    echo '提示：未安装 crontab，开机自启不可用；不影响手动启动。' >&2
  elif ! ps -eo comm= | awk '$1 == "cron" || $1 == "crond" { found=1 } END { exit !found }'; then
    echo '提示：未检测到运行中的 cron 服务，开机自启暂不可用；不影响手动启动。' >&2
  fi
fi
work="$(mktemp -d "$parent/.ez-mihomo-install.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT
printf '正在下载 %s，安装目录：%s\n' "$project" "$target"
curl -fL --retry 3 --connect-timeout 20 \
  https://codeload.github.com/NULL-SF/ez-mihomo/tar.gz/refs/heads/main \
  -o "$work/project.tar.gz"
mkdir "$work/project"
tar -xzf "$work/project.tar.gz" -C "$work/project" --strip-components=2 --no-same-owner "ez-mihomo-main/$project"
[[ -f "$work/project/$entry" && -x "$work/project/bin/mihomo_1.19.33" && -f "$work/project/bin/LICENSE.mihomo" ]] || {
  echo '下载内容不完整，未安装。' >&2; exit 1;
}
# Replace each program file by rename, including a core that is currently running.
# Existing configuration is preserved; missing default ports are installed.
mkdir -p -- "$target"
while IFS= read -r -d '' source_file; do
  relative="${source_file#"$work/project/"}"
  destination="$target/$relative"
  directory="$(dirname -- "$destination")"
  check="$directory"
  while [[ "$check" != "$parent" && "$check" != / ]]; do
    [[ ! -L "$check" ]] || { echo "安装目录中存在符号链接：$check" >&2; exit 1; }
    check="$(dirname -- "$check")"
  done
  if [[ "$relative" == config/* && ( -e "$destination" || -L "$destination" ) ]]; then
    continue
  fi
  mkdir -p -- "$directory"
  temporary="$(mktemp "$directory/.ezm-install.XXXXXXXX")"
  if ! cp -p -- "$source_file" "$temporary" || ! mv -fT -- "$temporary" "$destination"; then
    rm -f -- "$temporary"
    echo "更新失败：$destination" >&2; exit 1
  fi
done < <(find "$work/project" -type f -print0)
printf '下载完成：%s\n' "$target"
printf '使用方法见：%s/README.md\n' "$target"
