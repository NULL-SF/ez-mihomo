"""Exercise dependency recovery with fake installers; never alter the host or services."""
import pathlib
import shlex
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def shell(source, body):
    return subprocess.run(
        ['bash', '-c', 'set -euo pipefail\nsource ' + shlex.quote(str(ROOT / source)) + '\n' + body],
        text=True, capture_output=True,
    )


class DependencyTests(unittest.TestCase):
    def client(self, body, ok=True):
        result = shell('ezm-client/scripts/dependencies.sh', 'ROOT=/unused\n' + body)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def server(self, body, ok=True):
        result = shell('ezm-server/scripts/dependencies.sh', body)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def test_client_complete_does_not_install(self):
        self.client('''
command() { return 0; }
client_python_ready() { return 0; }
sudo() { echo UNEXPECTED; return 99; }
client_dependencies prepare
''')

    def test_client_read_only_missing_does_not_install(self):
        r = self.client('''
command() { [[ "${2:-}" != ss ]]; }
client_python_ready() { return 0; }
sudo() { echo UNEXPECTED; return 99; }
client_dependencies status
''', False)
        self.assertIn('ss', r.stderr)
        self.assertNotIn('UNEXPECTED', r.stdout)

    def test_stop_works_without_python_or_curl(self):
        self.client('''
command() { [[ "${2:-}" != curl ]]; }
client_python_ready() { return 1; }
client_dependencies stop
''')

    def test_client_sudo_failure_propagates(self):
        r = self.client('''
command() { [[ "${2:-}" != ss ]]; }
client_python_ready() { return 0; }
sudo() { return 42; }
client_dependencies prepare
''', False)
        self.assertEqual(r.returncode, 42)

    def test_client_rechecks_after_elevation(self):
        self.client('''
ready=0
command() { [[ "${2:-}" != ss || "$ready" == 1 ]]; }
client_python_ready() { return 0; }
sudo() { ready=1; }
client_dependencies prepare
[[ "$ready" == 1 ]]
''')

    def test_client_successful_installer_still_missing_fails(self):
        r = self.client('''
command() { [[ "${2:-}" != ss ]]; }
client_python_ready() { return 0; }
sudo() { return 0; }
client_dependencies prepare
''', False)
        self.assertIn('安装后仍缺少 ss', r.stderr)

    def test_client_python_failure_after_install_is_explicit(self):
        r = self.client('''
command() { return 0; }
client_python_ready() { return 1; }
sudo() { return 0; }
client_dependencies prepare
''', False)
        self.assertIn('Python', r.stderr)

    def test_client_without_sudo_fails_cleanly(self):
        r = self.client('''
command() { [[ "${2:-}" != ss && "${2:-}" != sudo ]]; }
client_python_ready() { return 0; }
client_dependencies prepare
''', False)
        self.assertIn('未安装 sudo', r.stderr)

    def test_package_mapping_and_deduplication(self):
        self.client('''
command() { return 1; }
CLIENT_MANAGER=apt-get
client_packages ss ip ps awk flock crontab
[[ "${CLIENT_PACKAGES[*]}" == 'iproute2 procps gawk util-linux cron' ]]
CLIENT_MANAGER=dnf
client_packages ss ip ps awk flock crontab
[[ "${CLIENT_PACKAGES[*]}" == 'iproute procps-ng gawk util-linux cronie' ]]
''')

    def test_apt_update_failure_prevents_install(self):
        r = self.client('''
CLIENT_MANAGER=apt-get
CLIENT_PACKAGES=(python3)
apt-get() { [[ "$1" != update ]] && echo UNEXPECTED; return 23; }
client_install_packages
''', False)
        self.assertEqual(r.returncode, 23)
        self.assertNotIn('UNEXPECTED', r.stdout)

    def test_boot_checks_daemon_after_installation(self):
        self.client('''
ready=0
command() { return 0; }
client_python_ready() { return 0; }
client_cron_running() { [[ "$ready" == 1 ]]; }
sudo() { ready=1; }
client_dependencies boot
''')

    def test_boot_rejects_inactive_daemon(self):
        self.client('''
command() { return 0; }
client_python_ready() { return 0; }
client_cron_running() { return 1; }
sudo() { return 0; }
client_dependencies boot
''', False)

    def test_each_client_command_checks_its_required_tools(self):
        for mode, tool in [('stop', 'ps'), ('status', 'ss'), ('test', 'sed'),
                           ('terminal', 'flock'), ('boot-control', 'grep'),
                           ('log', 'tail'), ('help', 'cat')]:
            with self.subTest(mode=mode):
                r = self.client('''
command() { [[ "${2:-}" != ''' + tool + ''' ]]; }
client_python_ready() { return 0; }
client_dependencies ''' + mode, False)
                self.assertIn(tool, r.stderr)

    def test_client_lightweight_commands_do_not_require_python(self):
        for mode in ['stop', 'test', 'log', 'help']:
            with self.subTest(mode=mode):
                self.client('''
command() { return 0; }
client_python_ready() { return 1; }
client_dependencies ''' + mode)

    def test_client_status_does_not_require_curl_or_cron(self):
        self.client('''
command() { [[ "${2:-}" != curl && "${2:-}" != crontab ]]; }
client_python_ready() { return 0; }
client_dependencies status
''')

    def test_client_boot_does_not_require_proxy_runtime(self):
        self.client('''
command() { case "${2:-}" in python3|ss|ip|ps|awk|curl|nohup) return 1 ;; *) return 0 ;; esac; }
client_python_ready() { return 1; }
client_cron_running() { return 0; }
client_dependencies boot
''')

    def test_client_service_has_no_probe_or_management_tools(self):
        self.client('''
command() { case "${2:-}" in ss|ps|awk|curl|nohup) return 1 ;; *) return 0 ;; esac; }
client_python_ready() { return 0; }
client_dependencies service
''')

    def test_proxychains_needs_no_curl_or_routing_utilities(self):
        self.client('''
command() { case "${2:-}" in curl|ip|ss|ps|awk) return 1 ;; *) return 0 ;; esac; }
client_python_ready() { [[ "$1" == basic ]]; }
client_dependencies proxychains
''')

    def test_client_runtime_preparation_is_reused(self):
        self.client('''
command() { return 0; }
client_python_ready() { return 0; }
client_dependencies prepare
client_python_ready() { echo UNEXPECTED >&2; return 99; }
client_dependencies prepare
''')

    def test_server_boot_does_not_prepare_node_or_certificate_tools(self):
        self.server('''
command() {
  case "${2:-}" in openssl|ip|curl|tail|cat|crontab) return 1 ;; esac
  printf '%s\\n' "${2:-}"
}
select_python() { PYTHON=fake_python; }
fake_python() { return 0; }
apt-get() { return 99; }
prepare_dependencies boot
''')

    def test_existing_venv_module_requires_no_package_manager(self):
        self.server('''
PYTHON=fake_python
fake_python() { return 0; }
apt-get() { return 99; }
prepare_python_venv
''')

    def test_server_required_tool_failure_is_explicit(self):
        r = self.server('''
command() { return 1; }
require_tools tail
''', False)
        self.assertIn('tail', r.stderr)

    def test_server_complete_does_not_install(self):
        self.server('''
command() { printf '%s\n' "${2:-}"; }
select_python() { PYTHON=fake_python; }
fake_python() { return 0; }
apt-get() { return 99; }
prepare_dependencies
''')

    def test_server_installs_and_rechecks(self):
        self.server('''
ready=0
command() {
  [[ "${2:-}" != ip || "$ready" == 1 ]] || return 1
  printf '%s\n' "${2:-}"
}
id() { echo 0; }
select_python() { PYTHON=fake_python; }
fake_python() { return 0; }
apt-get() { [[ "$1" != install ]] || ready=1; return 0; }
prepare_dependencies
[[ "$ready" == 1 ]]
''')

    def test_server_rejects_incomplete_install(self):
        r = self.server('''
command() { [[ "${2:-}" != ip ]] || return 1; printf '%s\n' "${2:-}"; }
id() { echo 0; }
select_python() { PYTHON=fake_python; }
fake_python() { return 0; }
apt-get() { return 0; }
prepare_dependencies
''', False)
        self.assertIn('仍缺少 ip', r.stderr)

    def test_client_boot_registers_public_start_entry(self):
        text = (ROOT / 'ezm-client/client.sh').read_text()
        function = text[text.index('cron_line() {'):text.index('cron_enabled() {')]
        result = shell('ezm-client/scripts/dependencies.sh', function + '''
ROOT=/example/ezm-client
LOG=$ROOT/log/mihomo.log
MARK=ezm-client-proxy-boot
cron_line
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/bin/bash '/example/ezm-client/client.sh' start", result.stdout)
        self.assertNotIn('client-service.sh', result.stdout)

    def test_server_restart_reuses_stop_then_start(self):
        text = (ROOT / 'ezm-server/server.sh').read_text()
        dispatch = text[text.index('case "$cmd" in\n  start)'):]
        r = shell('ezm-server/scripts/dependencies.sh', '''
cmd=restart
stop_server() { echo STOP; }
start_services() { echo START; return 37; }
''' + dispatch)
        self.assertEqual(r.returncode, 37, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['STOP', 'START'])

    def test_client_restart_reuses_stop_then_start(self):
        text = (ROOT / 'ezm-client/client.sh').read_text()
        dispatch = text[text.index('main() {'):]
        r = shell('ezm-client/scripts/dependencies.sh', '''
stop_proxy() { echo STOP; }
start_proxy() { echo START; return 37; }
set -- restart
''' + dispatch)
        self.assertEqual(r.returncode, 37, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['STOP', 'START'])


if __name__ == '__main__':
    unittest.main()
