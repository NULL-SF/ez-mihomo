#!/usr/bin/env bash
# Mock package managers and sudo: these checks never install or change services.
set -euo pipefail
cd -- "${BASH_SOURCE[0]%/*}/.."

for test_manager in apt-get dnf yum; do
  for mode in prepare service stop status test terminal boot-control proxychains log help boot; do
    (
      source ezm-client/scripts/dependencies.sh
      installed=0 cron_ready=0
      calls=()
      command() {
        if [[ "$1" == -v ]]; then
          case "$2" in
            apt-get|dnf|yum) [[ "$2" == "$test_manager" ]]; return ;;
            *) [[ "$installed" == 1 ]]; return ;;
          esac
        fi
        builtin command "$@"
      }
      client_python_ready() { [[ "$installed" == 1 ]]; }
      client_cron_running() { [[ "$cron_ready" == 1 ]]; }
      sudo() { calls+=("$*"); installed=1; [[ "$*" != *'enable --now'* ]] || cron_ready=1; }
      client_dependencies "$mode"
      [[ "$installed" == 1 && ${#calls[@]} -gt 0 ]]
      case "$mode" in
        log|help) [[ " ${calls[*]} " == *coreutils* ]] ;;
        boot) [[ " ${calls[*]} " == *curl* && " ${calls[*]} " == *python3* && "$cron_ready" == 1 ]] ;;
      esac
      before=${#calls[@]}
      client_dependencies "$mode"
      [[ ${#calls[@]} == "$before" ]]
    ) >/dev/null
  done
  (
    source ezm-server/scripts/dependencies.sh
    installed=0
    calls=()
    command() {
      if [[ "$1" == -v ]]; then
        case "$2" in
          apt-get|dnf|yum) [[ "$2" == "$test_manager" ]]; return ;;
          *) [[ "$installed" == 1 ]]; return ;;
        esac
      fi
      builtin command "$@"
    }
    sudo() { calls+=("$*"); installed=1; }
    require_tools tail cat flock ip
    [[ " ${calls[*]} " == *coreutils* && " ${calls[*]} " == *util-linux* ]]
    if [[ "$test_manager" == apt-get ]]; then
      [[ " ${calls[*]} " == *iproute2* ]]
    else
      [[ " ${calls[*]} " == *iproute* ]]
    fi
    installed=0
    select_python() { [[ "$installed" == 1 ]]; }
    require_python basic
    [[ " ${calls[*]} " == *python3* ]]
  ) >/dev/null
done

# Failed sudo/installation must abort preparation, not claim success.
for project in client server; do
  (
    source "ezm-$project/scripts/dependencies.sh"
    command() { [[ "$1" != -v || "$2" != tail ]] || return 1; builtin command "$@"; }
    sudo() { return 23; }
    if [[ "$project" == client ]]; then
      if client_dependencies log; then exit 1; else [[ "$?" == 23 ]]; fi
    else
      if require_tools tail; then exit 1; else [[ "$?" == 23 ]]; fi
    fi
  ) >/dev/null
  # Installation reporting success without restoring a tool must still fail.
  (
    source "ezm-$project/scripts/dependencies.sh"
    command() { [[ "$1" != -v || "$2" != tail ]] || return 1; builtin command "$@"; }
    sudo() { return 0; }
    if [[ "$project" == client ]]; then
      if client_dependencies log; then exit 1; fi
    else
      if require_tools tail; then exit 1; fi
    fi
  ) >/dev/null 2>&1
done
# Boot must invoke the shared start preparer and stop if it fails.
(
  source ezm-client/scripts/dependencies.sh
  eval "$(declare -f client_dependencies | sed '1s/client_dependencies/client_dependencies_impl/')"
  prepared=0
  client_dependencies() {
    if [[ "$1" == prepare ]]; then prepared=1; return 29; fi
    client_dependencies_impl "$@"
  }
  if client_dependencies boot; then exit 1; else [[ "$?" == 29 && "$prepared" == 1 ]]; fi
)
(
  source ezm-server/scripts/dependencies.sh
  eval "$(declare -f prepare_dependencies | sed '1s/prepare_dependencies/prepare_dependencies_impl/')"
  prepared=0
  prepare_dependencies() {
    if [[ "$1" == runtime ]]; then prepared=1; return 29; fi
    prepare_dependencies_impl "$@"
  }
  if prepare_dependencies boot; then exit 1; else [[ "$?" == 29 && "$prepared" == 1 ]]; fi
)
printf 'Dependency checks passed: apt/dnf/yum, all client modes, server tools/Python, sudo failure and post-install validation.\n'
