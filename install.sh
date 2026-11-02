#!/usr/bin/env bash
set -euo pipefail

# Download one self-contained project without requiring Git.
if (($# > 1)); then
  echo '用法：bash install.sh [安装目录]（默认 ./ezm-server 或 ./ezm-client）' >&2
  exit 1
fi
printf '选择安装项目：\n  1) ezm-server 服务端\n  2) ezm-client 客户端\n'
while true; do
  if ! read -r -p '请选择 [1/2]：' choice; then
    echo '未选择，已取消安装。' >&2; exit 1
  fi
  case "$choice" in
    1) project=ezm-server; break ;;
    2) project=ezm-client; break ;;
    *) echo '请输入 1 或 2。' >&2 ;;
  esac
done

target="${1:-$PWD/$project}"
if [[ -L "$target" || ( -e "$target" && ! -d "$target" ) ]]; then
  echo "安装路径必须是目录：$target" >&2; exit 1
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
