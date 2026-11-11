#!/usr/bin/env bash
# 管理 127.0.0.1 上的 SOCKS / HTTP 混合入口，端口见 config/ports/listening.port。
# node 与 ssh_reverse 共用 mihomo；模式见 config/mode.activate，节点协议见 config/protocol.activate。
# 开机自启写在该用户的 crontab，到点执行 client.sh start，统一走启动准备。
# 运行操作写入 log/mihomo.log，终端自动接入设置写入 log/terminal.log。询问与 status 只显示在终端。
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1
# 管理命令及核心使用自身配置，不继承终端的代理与预加载。
unset LD_PRELOAD HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
unset PROXYCHAINS_CONF_FILE PROXYCHAINS_QUIET_MODE _MIHOMO_PROXY_SNAPSHOT

ROOT="${BASH_SOURCE[0]%/*}"
[[ "$ROOT" != "${BASH_SOURCE[0]}" ]] || ROOT=.
ROOT="$(cd -- "$ROOT" && pwd -P)"
source "${ROOT}/scripts/terminal-env.sh"
source "$ROOT/scripts/dependencies.sh"
client_restore_access() {
  if client_python_ready basic; then
    python3 "$ROOT/scripts/permissions.py" || printf '项目文件归属恢复失败，请检查权限。\n' >&2
  fi
}
trap client_restore_access EXIT
case "${1:-}" in
  start) client_dependencies start-check ;;
  restart) client_dependencies stop ;;
  protocol|mode) client_dependencies selection ;;
  stop) client_dependencies stop ;;
  status|test|terminal|log) client_dependencies "$1" ;;
  boot) client_dependencies boot-control ;;
  __write_proxychains) client_dependencies proxychains >&2 ;;
  ''|help|-h|--help) client_dependencies help ;;
esac
ACTIVE="${ROOT}/config/protocol.activate"
PIDFILE="${ROOT}/runtime/mihomo.pid"
LOG="${ROOT}/log/mihomo.log"
BOOT_LOG="${ROOT}/log/boot.log"
TERMINAL_LOG="${ROOT}/log/terminal.log"
NOTE_LOG="$LOG"
MARK="ezm-client-proxy-boot"
TERMINAL_FLAG="${ROOT}/config/terminal.on"
BASHRC="${HOME}/.bashrc"
MANAGEMENT_LOCK=''
load_port() {
  _mihomo_load_ports "$ROOT" || return 1
  PORT="$PROXY_PORT"
}
PORT_CONFIG_VALID=1
PORT_CONFIG_NEEDED=0
case "${1:-}" in
  status|test|__write_proxychains)
    PORT_CONFIG_NEEDED=1
    if ! load_port; then
      case "${1:-}" in
        status|stop)
          PORT_CONFIG_VALID=0
          PORT=0 PROXY_PORT=0
          ;;
        *) exit 1 ;;
      esac
    fi
    ;;
esac

ensure_port() {
  _mihomo_ensure_ports "$ROOT" || return 1
  PORT="$PROXY_PORT"
}

lock_management() {
  [[ -z "${MANAGEMENT_LOCK:-}" ]] || return 0
  mkdir -p "${ROOT}/runtime"
  exec {MANAGEMENT_LOCK}>"${ROOT}/runtime/management.lock"
  if ! flock -w 45 "$MANAGEMENT_LOCK"; then
    printf '其他管理操作尚未结束，请稍后重试。\n' >&2
    return 1
  fi
  if [[ "$PORT_CONFIG_NEEDED" == 1 && "$PORT_CONFIG_VALID" == 1 ]]; then load_port; fi
}

sync_proxychains() {
  mkdir -p "${ROOT}/runtime" || return 1
  local tmp_config
  tmp_config="$(mktemp "${ROOT}/runtime/.proxychains.conf.XXXXXX")" || return 1
  if ! cat > "$tmp_config" <<EOF
# 由 client.sh 根据 config/ports/listening.port 生成到 runtime。不要手改。
# 仅在 source proxy.env 之后生效。
strict_chain
proxy_dns
remote_dns_subnet 224
tcp_read_time_out 15000
tcp_connect_time_out 8000
quiet_mode

# 本机地址直连，避免再进入代理端口。
localnet 127.0.0.0/255.0.0.0
localnet ::1/128

[ProxyList]
socks5 127.0.0.1 ${PORT}
EOF
  then
    rm -f -- "$tmp_config"
    return 1
  fi
  if ! mv -f -- "$tmp_config" "${ROOT}/runtime/proxychains.conf"; then
    rm -f -- "$tmp_config"
    return 1
  fi
}

note() {
  local logfile="${NOTE_LOG:-$LOG}"
  printf '%s\n' "$*"
  mkdir -p "${ROOT}/log"
  printf '%s [mihomo] %s\n' "$(date '+%Y/%m/%d %H:%M:%S')" "$*" >> "$logfile"
}

current_profile() {
  local name=""
  if [[ -r "$ACTIVE" ]]; then
    name="$(tr -d '[:space:]' < "$ACTIVE")"
  fi
  # 旧文件里可能写过这些简称。
  [[ "$name" == "reality" ]] && name="vless"
  [[ "$name" == "hy2" || "$name" == "hysteria" ]] && name="hysteria2"
  printf '%s\n' "${name:-hysteria2}"
}

core_for() {
  printf '%s\n' "bin/mihomo_1.19.33"
}

own_listener() {
  local pid cmd
  pid="$(listener_pid)"
  [[ -n "$pid" ]] || return 1
  cmd="$(pid_cmdline "$pid")"
  [[ "$cmd" == "${ROOT}/bin/mihomo_1.19.33 -d ${ROOT}/runtime -f ${ROOT}/runtime/mihomo.json " ]]
}

port_open() {
  ss -lnt 2>/dev/null | grep "127\\.0\\.0\\.1:${PORT}[[:space:]]" >/dev/null
}

listener_pid() {
  ss -lntp 2>/dev/null | sed -n "s/.*127\\.0\\.0\\.1:${PORT} .*pid=\\([0-9]*\\).*/\\1/p" | sed -n '1p'
}

tracked_daemon_alive() {
  local pid cmd
  [[ -f "$PIDFILE" ]] || return 1
  pid="$(tr -d '[:space:]' < "$PIDFILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(pid_cmdline "$pid")"
  [[ "$cmd" == "${ROOT}/bin/mihomo_1.19.33 -d ${ROOT}/runtime -f ${ROOT}/runtime/mihomo.json " ]] || {
    [[ "/proc/$pid/exe" -ef "/proc/$$/exe" ]] &&
      [[ "$cmd" == *" ${ROOT}/scripts/client-service.sh "* ]]
  }
}

daemon_alive() {
  tracked_daemon_alive || [[ -n "$(instance_pids)" ]]
}

pid_cmdline() {
  [[ "${1:-}" =~ ^[0-9]+$ ]] || return 0
  tr '\0' ' ' 2>/dev/null < "/proc/${1}/cmdline" || true
}

# 结束该进程及其子进程。
kill_tree() {
  local pid="$1" child
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  [[ "$pid" -gt 1 ]] || return 0
  for child in $(ps -o pid= --ppid "$pid" 2>/dev/null || true); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

cron_line() {
  local service="${ROOT}/client.sh" logfile="$BOOT_LOG" logdir="${ROOT}/log" line
  [[ "$ROOT" != *$'\n'* ]] || { printf '路径含换行符，无法登记 crontab。\n' >&2; return 1; }
  service="${service//\'/\'\\\'\'}"
  logfile="${logfile//\'/\'\\\'\'}"
  logdir="${logdir//\'/\'\\\'\'}"
  printf -v line "@reboot sleep 20; mkdir -p '%s'; PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /bin/bash '%s' start >> '%s' 2>&1 < /dev/null # %s" \
    "$logdir" "$service" "$logfile" "$MARK"
  # cron 即使在引号内也会解释百分号，需先转义。
  printf '%s\n' "${line//%/\\%}"
}

cron_enabled() {
  command -v crontab >/dev/null || return 1
  local expected
  expected="$(cron_line)" || return 1
  crontab -l 2>/dev/null | grep -Fx -- "$expected" >/dev/null
}

set_cron() (
  if [[ "$1" == off ]] && ! command -v crontab >/dev/null; then return 0; fi
  local mode="$1" current entry had_crontab=1 cron_home
  cron_home="$(python3 -c 'import os,pwd; print(pwd.getpwuid(os.geteuid()).pw_dir)')" || return 1
  exec 8<"$cron_home"
  flock -w 30 8 || { echo '其他开机任务设置尚未结束，请稍后重试。' >&2; return 1; }
  if ! current="$(LC_ALL=C crontab -l 2>&1)"; then
    if [[ "$current" == *'no crontab for '* ]]; then
      current=""
      had_crontab=0
    else
      printf '读取 crontab 失败，未修改：%s\n' "$current" >&2
      return 1
    fi
  fi
  current="$(printf '%s\n' "$current" | grep -v " # ${MARK}$" || true)"
  if [[ "$mode" == "on" ]]; then
    entry="$(cron_line)" || return 1
    { [[ -z "$current" ]] || printf '%s\n' "$current"; printf '%s\n' "$entry"; } | crontab -
    return
  fi
  if [[ -z "$current" ]]; then
    [[ "$had_crontab" == 1 ]] || return 0
    crontab -r
    return
  fi
  printf '%s\n' "$current" | crontab -
)

start_proxy() {
  lock_management || return $?
  FRESH_START=0
  if daemon_alive; then
    note "代理已运行。运行模式：$(running_mode)"
    return 0
  fi
  client_dependencies prepare || return $?
  client_platform || return $?
  ensure_port || return $?
  local selected reverse_port
  selected="$(_mihomo_load_mode "$ROOT")" || return 1
  if [[ "$selected" == ssh_reverse ]]; then
    reverse_port="$(_mihomo_reverse_port "$ROOT")" || return 1
    [[ "$reverse_port" != "$PORT" ]] || { note 'SSH 转发端口不能与代理入口相同。'; return 1; }
  fi
  _mihomo_check_available_ports "$ROOT" "$PROXY_PORT" || return 1
  # 核心不能持有管理锁，否则后续 stop/restart 会一直等待。
  mkdir -p "${ROOT}/log"
  ( exec {MANAGEMENT_LOCK}>&-; exec nohup "${ROOT}/scripts/client-service.sh" --managed ) >> "$LOG" 2>&1 < /dev/null &
  local child=$!
  disown || true
  # 核心在默认路由出现前最多等待 30 秒，这里留出余量。端口一旦打开即返回。
  # 启动程序已经退出时不再继续等，原因在日志里。
  local i
  for i in $(seq 1 40); do
    if port_open; then
      if ! own_listener; then
        note "启动失败：混合代理入口被其他进程占用。"
        return 1
      fi
      note "已启动。模式 $(running_mode)，监听 127.0.0.1:${PROXY_PORT}（SOCKS / HTTP 混合代理）。"
      if [[ "$selected" == ssh_reverse ]]; then
        note "SSH 反向转发上游：127.0.0.1:$reverse_port（config/ports/ssh_reverse.port）；隧道由用户建立，仅支持 TCP，不支持 UDP。"
      fi
      sync_proxychains || return 1
      FRESH_START=1
      return 0
    fi
    if ! kill -0 "$child" 2>/dev/null; then
      note "启动失败：SOCKS / HTTP 混合代理入口未就绪。详见 ${LOG}"
      return 1
    fi
    sleep 1
  done
  if daemon_alive; then
    note "40 秒内 SOCKS / HTTP 混合代理入口未就绪。核心进程仍在启动。详见 ${LOG}"
  else
    note "启动失败：40 秒内 SOCKS / HTTP 混合代理入口未就绪。详见 ${LOG}"
  fi
  return 1
}

# Locate only this installation's actual core, independent of editable configuration.
instance_pids() {
  local pid args
  while read -r pid args; do
    if [[ "$args" == "${ROOT}/bin/mihomo_1.19.33 -d ${ROOT}/runtime -f ${ROOT}/runtime/mihomo.json" ]]; then
      [[ "$(pid_cmdline "$pid")" == "$args " ]] && printf '%s\n' "$pid"
    fi
  done < <(ps -ww -eo pid=,args=)
  return 0
}

# Stop actual processes belonging to this installation, never a configured port.
stop_proxy() {
  client_dependencies stop || return $?
  lock_management || return $?
  local pid attempt remaining
  if tracked_daemon_alive; then
    pid="$(tr -d '[:space:]' < "$PIDFILE")"
    kill_tree "$pid"
  fi
  for pid in $(instance_pids); do kill_tree "$pid"; done
  for attempt in {1..30}; do
    remaining="$(instance_pids)"
    if ! daemon_alive && [[ -z "$remaining" ]]; then
      rm -f "$PIDFILE" "$ROOT/runtime/mode.running"
      note "本项目核心已停止。"
      return 0
    fi
    sleep 0.1
  done
  note "停止失败：本项目进程尚未退出。"
  return 1
}

# 常见外网页面。跟随跳转后的最终响应为 200，视为该页能打开。
PROBE_URL="https://www.google.com"

# 只报告当前连接，不改线路，也不启停监听。
report_connection() {
  local kind label port url result code secs ms
  local failed=0 entry_failed
  if daemon_alive && [[ "$(running_mode)" == ssh_reverse ]]; then
    echo '当前运行 ssh_reverse：仅支持 TCP，不支持 UDP；以下测试只验证 HTTP / SOCKS 的 TCP 出网。'
  fi
  local urls=() proxy_args=()
  for kind in http socks; do
    if [[ "$kind" == http ]]; then
      label=HTTP port="$PROXY_PORT"
      urls=("$PROBE_URL")
      proxy_args=(--proxy "http://127.0.0.1:${PROXY_PORT}")
    else
      label=SOCKS port="$PROXY_PORT"
      urls=("$PROBE_URL")
      proxy_args=(--socks5-hostname "127.0.0.1:${PROXY_PORT}")
    fi
    ms="未测得"
    # 同一监听端口，分别验证 HTTP 与 SOCKS 协议。
    if ! ( PORT="$port"; port_open ); then
      note "${label} 代理：连接失败，127.0.0.1:${port} 未在监听；访问 ${PROBE_URL} 的首字节耗时：未测得。"
      failed=1
      continue
    fi
    if ! ( PORT="$port"; own_listener ); then
      note "${label} 代理：连接失败，127.0.0.1:${port} 被其他进程占用；访问 ${PROBE_URL} 的首字节耗时：未测得。"
      failed=1
      continue
    fi
    entry_failed=0
    for url in "${urls[@]}"; do
      if ! result="$(curl -sS -L --noproxy '' "${proxy_args[@]}" \
        --connect-timeout 12 --max-time 20 -o /dev/null \
        -w '%{http_code} %{time_starttransfer}' "$url" 2>/dev/null)"; then
        note "${label} 代理访问 ${url} 失败（连接、TLS 或传输未完成）。" >/dev/null
        entry_failed=1
        continue
      fi
      code="${result%% *}"
      secs="${result#* }"
      if [[ "$code" != 200 ]]; then
        note "${label} 代理访问 ${url} 失败（响应 ${code}）。" >/dev/null
        entry_failed=1
      elif [[ "$url" == "$PROBE_URL" && "$secs" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        ms="$(awk -v t="$secs" 'BEGIN { printf "%d", t * 1000 }') 毫秒"
      fi
    done
    if [[ "$entry_failed" == 0 ]]; then
      note "${label} 代理：连接正常；访问 ${PROBE_URL} 的首字节耗时：${ms}。"
    else
      note "${label} 代理：连接失败；访问 ${PROBE_URL} 的首字节耗时：${ms}。详见日志。"
      failed=1
    fi
  done
  return "$failed"
}

running_mode() {
  local value=未知
  if [[ -r "$ROOT/runtime/mode.running" ]]; then
    value="$(tr -d '[:space:]' < "$ROOT/runtime/mode.running")"
  fi
  case "$value" in node|ssh_reverse) echo "$value" ;; *) echo 未知 ;; esac
}

select_mode() {
  local choice
  echo "配置模式：$(_mihomo_load_mode "$ROOT")"
  echo '  1) node'
  echo '  2) ssh_reverse（仅支持 TCP，不支持 UDP；需自行建立 SSH 反向隧道）'
  printf '输入序号或模式名：'
  read -r choice || { echo '已取消，未作更改。'; return 0; }
  case "$choice" in
    1|node) choice=node ;;
    2|ssh_reverse) choice=ssh_reverse ;;
    *) echo '已取消，未作更改。'; return 1 ;;
  esac
  lock_management
  mkdir -p "$ROOT/config"
  if [[ "$choice" == ssh_reverse ]]; then
    local reverse_port
    reverse_port="$(_mihomo_reverse_port "$ROOT")" || return 1
    _mihomo_write_text "$ROOT/config/ports/ssh_reverse.port" "$reverse_port"
    echo "SSH 反向转发上游：127.0.0.1:$reverse_port（config/ports/ssh_reverse.port）"
    echo '上游必须通向 SOCKS5 代理，不是 SSH 登录端口；ezm 不建立或重连隧道。'
    echo '仅支持 TCP，不支持 UDP；上游断开时请求失败，不自动回退其他出口。'
  fi
  _mihomo_write_text "$ROOT/config/mode.activate" "$choice"
  note "已保存模式：$choice。未启停代理；运行中需执行 restart 生效。"
}

select_protocol() {
  local current choice
  current="$(current_profile)"
  echo "当前配置线路: ${current}（$(core_for "$current")）"
  echo "  1) vless"
  echo "  2) hysteria2"
  echo "  3) vmess"
  echo "  4) trojan"
  echo "  5) shadowsocks"
  echo "  6) tuic"
  echo "  7) anytls"
  printf '输入序号或线路名：'
  read -r choice || { echo "已取消，未作更改。"; return 0; }
  case "$choice" in
    1|vless) choice="vless" ;;
    2|hysteria2) choice="hysteria2" ;;
    3|vmess) choice="vmess" ;;
    4|trojan) choice="trojan" ;;
    5|shadowsocks|ss) choice="shadowsocks" ;;
    6|tuic) choice="tuic" ;;
    7|anytls) choice="anytls" ;;
    *)
      echo "已取消，未作更改。"
      return 1
      ;;
  esac
  lock_management
  mkdir -p "$ROOT/config"
  _mihomo_write_text "$ACTIVE" "$choice"
  note "已保存节点协议：${choice}。未启停代理；运行中需执行 restart 生效。"
}

terminal_enabled() {
  [[ -f "$TERMINAL_FLAG" ]] || return 1
  python3 - "$BASHRC" "$ROOT" <<'PY_TERMINAL_STATUS'
import pathlib, shlex, sys
try:
    text = pathlib.Path(sys.argv[1]).read_text()
    block = text.split('# ezm-client-proxy-terminal-begin', 1)[1].split('# ezm-client-proxy-terminal-end', 1)[0]
    expected = '  source ' + shlex.quote(sys.argv[2] + '/proxy.env')
    sys.exit(0 if expected in block.splitlines() else 1)
except (OSError, UnicodeError, IndexError):
    sys.exit(1)
PY_TERMINAL_STATUS
}

set_terminal() {
  local mode="$1"
  if [[ "$mode" == "on" ]]; then
    mkdir -p "${TERMINAL_FLAG%/*}" || return 1
  fi
  python3 - "$BASHRC" "$mode" "$ROOT" <<'PY' || return $?
import os, pathlib, shlex, sys, tempfile
path, mode, root = sys.argv[1:]
begin = "# ezm-client-proxy-terminal-begin"
end = "# ezm-client-proxy-terminal-end"
file = pathlib.Path(path).resolve()
text = file.read_text() if file.exists() else ""
def save_text(text):
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=file.parent, prefix="." + file.name + ".", delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(text)
        if file.exists():
            temporary.chmod(file.stat().st_mode & 0o777)
        temporary.replace(file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
while True:
    start = text.find(begin)
    if start < 0:
        break
    stop = text.find(end, start)
    if stop < 0:
        text = text[:start]
        break
    stop = text.find("\n", stop)
    stop = len(text) if stop < 0 else stop + 1
    text = text[:start] + text[stop:]
existed = file.exists()
if mode != "on":
    if existed:
        save_text(text)
    raise SystemExit(0)
if mode == "on":
    block = f"""# ezm-client-proxy-terminal-begin
if [[ "${{VSCODE_RESOLVING_ENVIRONMENT:-}}" != 1 && -f {shlex.quote(root + '/config/terminal.on')} && -f {shlex.quote(root + '/proxy.env')} ]]; then
  source {shlex.quote(root + '/proxy.env')}
fi
# ezm-client-proxy-terminal-end
"""
    if text and not text.endswith("\n"):
        text += "\n"
    text += block
save_text(text)
PY
  if [[ "$mode" == "on" ]]; then
    : > "$TERMINAL_FLAG"
  else
    rm -f "$TERMINAL_FLAG"
  fi
}

terminal_mihomo() {
  local state choice detail NOTE_LOG="$TERMINAL_LOG"
  if terminal_enabled; then
    state="开"
  else
    state="关"
  fi
  echo "终端自动接入代理: ${state}"
  echo "  1) 开启"
  echo "  2) 关闭"
  printf '输入序号：'
  read -r choice || { echo "已取消，未作更改。"; return 0; }
  case "$choice" in
    1|on|开启)
      lock_management
      if ! detail="$(set_terminal on 2>&1)"; then
        note "终端自动接入代理开启失败：${detail}"
        return 1
      fi
      note "终端自动接入代理已开启。此后新打开的终端会自动接入代理。已经打开的窗口不受影响。"
      ;;
    2|off|关闭)
      lock_management
      if ! detail="$(set_terminal off 2>&1)"; then
        note "终端自动接入代理关闭失败：${detail}"
        return 1
      fi
      note "终端自动接入代理已关闭。此后新打开的终端不会自动接入代理。已经打开的窗口不受影响。"
      ;;
    *)
      echo "已取消，未作更改。"
      return 1
      ;;
  esac
}

boot_mihomo() {
  local state choice
  if cron_enabled; then
    state="开"
  else
    state="关"
  fi
  echo "开机自启: ${state}"
  echo "  1) 开启"
  echo "  2) 关闭"
  printf '输入序号：'
  read -r choice || { echo "已取消，未作更改。"; return 0; }
  case "$choice" in
    1|on|开启)
      client_dependencies boot
      lock_management
      set_cron on
      note "开机自启已开启。已写入 crontab，下次开机会自动启动。"
      ;;
    2|off|关闭)
      lock_management
      set_cron off
      note "开机自启已关闭。已从 crontab 移除，下次开机不会自动启动。"
      ;;
    *)
      echo "已取消，未作更改。"
      return 1
      ;;
  esac
}

status_mihomo() {
  local profile
  echo "配置模式：$(_mihomo_load_mode "$ROOT" || echo 无效)"
  if daemon_alive; then
    echo "运行模式：$(running_mode)"
    if [[ "$(running_mode)" == ssh_reverse ]]; then
      python3 - "$ROOT/runtime/mihomo.json" <<'PY_UPSTREAM'
import json,sys,subprocess
try:
    cfg=json.load(open(sys.argv[1]))
    node=next(n for n in cfg.get('proxies',[]) if n.get('name')=='node' and n.get('type')=='socks5')
    print(f"运行 SSH 上游：{node['server']}:{node['port']}")
    # Check the local listener only; it cannot prove the SSH session or remote egress works.
    listeners=subprocess.run(['ss','-Hlnt'],capture_output=True,text=True,check=True).stdout
    endpoint=f"127.0.0.1:{node['port']}"
    listening=any(len(row.split())>3 and row.split()[3]==endpoint for row in listeners.splitlines())
    print('SSH 上游端口已监听；完整出网能力请运行 test 检查。' if listening else '等待 SSH 上游：本机反向转发端口未监听，当前无法转发。')
except (OSError,ValueError,KeyError,TypeError,StopIteration,subprocess.SubprocessError):
    print('运行 SSH 上游配置不可读或已拒绝转发')
PY_UPSTREAM
      echo '运行限制：仅支持 TCP，不支持 UDP；隧道断开不会自动回退其他出口。'
    fi
  fi
  if [[ "$(_mihomo_load_mode "$ROOT" 2>/dev/null || true)" == ssh_reverse ]]; then
    echo "SSH 反向转发上游：127.0.0.1:$(_mihomo_reverse_port "$ROOT" || echo 无效)"
    echo '配置文件：config/ports/ssh_reverse.port；隧道由用户建立，仅支持 TCP，不支持 UDP。'
  fi
  profile="$(current_profile)"
  echo "配置线路: ${profile}（$(core_for "$profile")）"
  if cron_enabled; then
    echo "开机自启: 开"
  else
    echo "开机自启: 关"
  fi
  if terminal_enabled; then
    echo "终端自动接入代理: 开"
  else
    echo "终端自动接入代理: 关"
  fi
  echo "日志目录: ${ROOT}/log/"
  if [[ "${PROXY_PORT_DEFAULTED:-0}" == 1 ]]; then
    echo "代理端口配置缺失、为空或无效，按默认 10808 查询；未写入文件。"
  fi
  if [[ "$PORT_CONFIG_VALID" == 0 ]]; then
    echo "端口配置无效；以下运行配置仅供诊断，未修改文件。"
  fi
  if daemon_alive; then
    python3 - "$ROOT/runtime/mihomo.json" "$PROXY_PORT" "$PORT_CONFIG_VALID" <<'PY_STATUS'
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
    actual = (cfg.get('bind-address'), cfg.get('mixed-port'))
    if sys.argv[3] != '1' or actual != ('127.0.0.1', int(sys.argv[2])):
        print(f"运行配置入口: SOCKS / HTTP 混合代理 {actual[0]}:{actual[1]}；与当前端口配置不一致")
except (OSError, ValueError, KeyError, TypeError):
    print('运行配置不可读')
PY_STATUS
  fi
  [[ "$PORT_CONFIG_VALID" == 1 ]] || return 0
  if port_open; then
    local pid cmd
    if own_listener; then
      echo "SOCKS / HTTP 混合代理监听: 127.0.0.1:${PORT}"
    else
      echo "SOCKS / HTTP 混合代理监听: 127.0.0.1:${PORT} 已被其他进程占用"
    fi
    pid="$(listener_pid)"
    if [[ -n "$pid" && -r "/proc/${pid}/cmdline" ]]; then
      cmd="$(pid_cmdline "$pid")"
      if [[ -n "$cmd" ]]; then
        echo "进程 PID: ${pid}"
      fi
    fi
  else
    echo "SOCKS / HTTP 混合代理监听: 127.0.0.1:${PORT} 未在监听"
  fi
}

usage() {
  cat <<EOF
用法: $0 <命令>

  start    按 config/mode.activate 启动。已在监听时不重复启动。启动后保持后台运行
  stop     停止本项目核心及混合代理入口。依赖该代理的程序随即无法经该代理出网
  restart  关闭监听后按保存的模式与协议再启动，然后报告连接是否正常
  status   查看配置线路、开机自启、终端自动接入代理与监听。不写入日志
  test     分别测试 HTTP 和 SOCKS 代理，均访问 ${PROBE_URL}，显示连接结果和首字节耗时。不改线路，也不启停监听
  protocol 选择并保存节点协议，不启停代理；restart 后应用
  mode     选择并保存 node / ssh_reverse 模式，不启停代理；restart 后应用。SSH 模式仅支持 TCP，不支持 UDP
  boot     询问后开启或关闭开机自启；开启时准备启动依赖。不启动、不停止当前监听
  terminal 询问后开启或关闭新终端自动接入代理。不开启、不关闭监听
  log      菜单查看运行、开机启动和终端自动接入设置日志
EOF
}

main() {
  if (($# > 1)); then
    printf '客户端命令不接受额外参数。\n' >&2
    return 1
  fi
  case "${1:-}" in
    ''|help|-h|--help) usage ;;
    start)
      start_proxy
      if [[ "${FRESH_START:-0}" -eq 1 ]]; then
        report_connection || true
      fi
      ;;
    stop) stop_proxy ;;
    restart)
      stop_proxy
      start_proxy
      report_connection || true
      ;;
    test) report_connection ;;
    protocol) select_protocol ;;
    mode) select_mode ;;
    boot) boot_mihomo ;;
    terminal) terminal_mihomo ;;
    status) status_mihomo ;;
    log)
      echo '选择要查看的日志：'
      echo '  0) 全部（运行、开机启动、终端自动接入设置）'
      echo '  1) 运行'
      echo '  2) 开机启动'
      echo '  3) 终端自动接入设置'
      local choice files=() file
      read -r -p '输入序号 [0]：' choice || { echo '已取消。'; return 0; }
      case "${choice:-0}" in
        0) files=("$LOG" "$BOOT_LOG" "$TERMINAL_LOG") ;;
        1) files=("$LOG") ;;
        2) files=("$BOOT_LOG") ;;
        3) files=("$TERMINAL_LOG") ;;
        *) echo '输入无效，未查看日志。' >&2; return 1 ;;
      esac
      local existing=()
      for file in "${files[@]}"; do
        [[ ! -f "$file" ]] || existing+=("$file")
      done
      ((${#existing[@]})) || { echo '所选项目暂无日志。' >&2; return 1; }
      tail -v -n 50 -F -- "${existing[@]}"
      ;;
    __write_proxychains) lock_management; ensure_port; _mihomo_check_running_ports "$ROOT" "$PROXY_PORT"; sync_proxychains; printf '%s\n' "$PROXY_PORT" ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
