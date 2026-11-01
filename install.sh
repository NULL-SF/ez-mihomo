#!/usr/bin/env bash
set -euo pipefail

# Download the complete project without requiring Git.
if (($# > 1)); then
  echo '用法：bash install.sh [安装目录]（默认 ./ez-mihomo）' >&2
  exit 1
fi
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo '仅支持 Linux x86_64。' >&2; exit 1;
}
[[ "$EUID" == 0 ]] || {
  echo '请以 root 运行，或使用 sudo bash install.sh [安装目录]。' >&2; exit 1;
}

target="${1:-$PWD/ez-mihomo}"
[[ ! -e "$target" && ! -L "$target" ]] || {
  echo "安装目录已存在，不覆盖已有文件：$target" >&2
  echo '如需启动已有服务，请进入其 ezm-server 目录执行 bash ./server.sh start。' >&2
  exit 1
}

packages=()
for tool in curl tar gzip; do
  command -v "$tool" >/dev/null || packages+=("$tool")
done
if [[ ! -s /etc/ssl/certs/ca-certificates.crt && ! -s /etc/pki/tls/certs/ca-bundle.crt && ! -s /etc/ssl/cert.pem ]]; then
  packages+=(ca-certificates)
fi
if ((${#packages[@]})); then
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
work="$(mktemp -d "$parent/.ez-mihomo-install.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT
printf '正在下载 ez-mihomo，安装目录：%s\n' "$target"
curl -fL --retry 3 --connect-timeout 20 \
  https://codeload.github.com/NULL-SF/ez-mihomo/tar.gz/refs/heads/main \
  -o "$work/project.tar.gz"
mkdir "$work/project"
tar -xzf "$work/project.tar.gz" -C "$work/project" --strip-components=1 --no-same-owner
[[ -f "$work/project/ezm-server/server.sh" && -x "$work/project/ezm-server/bin/mihomo_1.19.33" && -f "$work/project/ezm-client/client.sh" ]] || {
  echo '下载内容不完整，未安装。' >&2; exit 1;
}
# Preserve an existing directory even if another installation created it meanwhile.
mv -T -n -- "$work/project" "$target"
[[ ! -e "$work/project" ]] || {
  echo "安装目录已存在，未覆盖：$target" >&2; exit 1;
}
printf '下载完成：%s\n' "$target"
printf '启动服务端：\n  cd %q\n  bash ./server.sh start\n' "$target/ezm-server"
