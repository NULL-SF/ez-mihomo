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
[[ ! -e "$target" && ! -L "$target" ]] || {
  echo "安装目录已存在，不覆盖已有文件：$target" >&2
  printf '如需启动已有项目，请进入该目录执行 bash ./%s start。\n' "$entry" >&2
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
# Preserve an existing directory even if another installation created it meanwhile.
mv -T -n -- "$work/project" "$target"
[[ ! -e "$work/project" ]] || {
  echo "安装目录已存在，未覆盖：$target" >&2; exit 1;
}
printf '下载完成：%s\n' "$target"
printf '使用方法见：%s/README.md\n' "$target"
