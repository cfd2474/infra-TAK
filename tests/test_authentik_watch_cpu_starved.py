# SPDX-License-Identifier: AGPL-3.0-or-later
# infra-TAK — TAK Infrastructure Platform
# Copyright (C) 2026 Andreas Johansson (TAKWERX)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Guard Dog stops restarting Authentik into a CPU-starved host (GH #111).

Field (v10.2.9, Ubuntu 22.04, 8 vCPU): host load ~50, cpu pressure 83%. Authentik
answered its health check slowly, so tak-authentik-watch.sh ran a full
`docker compose down && up -d` every ~20 minutes all night (15-min cooldown + 3
checks). Each cold start burned ~1.5 cores for 2 minutes and reset the LDAP
outpost, so the restarts fed the overload they were reacting to.

The REAL watchdog runs here with its absolute paths pointed into a sandbox and
curl/docker stubbed; every docker call and alert is recorded.
"""

import os
import pathlib
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
WATCH = (REPO / 'scripts' / 'guarddog' / 'tak-authentik-watch.sh').read_text()

LIB = '''
gd_find_stack_dir() { echo "%(ak)s"; }
gd_ldap_groups_ok() { return 0; }
'''


@pytest.fixture
def box(tmp_path):
    calls = tmp_path / 'calls.log'
    stubs = tmp_path / 'bin'
    stubs.mkdir()
    ak = tmp_path / 'authentik'
    ak.mkdir()
    gd = tmp_path / 'gd'
    gd.mkdir()
    state = tmp_path / 'state'
    logs = tmp_path / 'logs'
    proc = tmp_path / 'proc'
    proc.mkdir()
    (proc / 'uptime').write_text('99999.00 1.00\n')

    (gd / '_gd-tak-lib.sh').write_text(LIB % {'ak': ak})
    alert = gd / 'send-alert-email.sh'
    alert.write_text('#!/bin/sh\necho "alert $1" >> %s\ncat > /dev/null\n' % calls)
    alert.chmod(0o755)

    # curl prints "<code> <secs>" and exits with the rc in $tmp/curl ("503 12.5 0").
    (stubs / 'curl').write_text(
        '#!/bin/sh\nset -- $(cat %s)\nprintf "%%s %%s" "$1" "$2"\nexit "$3"\n' % (tmp_path / 'curl'))
    (stubs / 'docker').write_text(
        '#!/bin/sh\necho "docker $*" >> %s\n'
        'case "$*" in *"ps --status running --services"*) cat %s ;; esac\nexit 0\n'
        % (calls, tmp_path / 'running'))
    (stubs / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    (stubs / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (stubs / 'nproc').write_text('#!/bin/sh\necho 8\n')
    for f in stubs.iterdir():
        f.chmod(0o755)
    (tmp_path / 'running').write_text('postgresql\nredis\nserver\nworker\nldap\n')

    script = (WATCH.replace('/opt/tak-guarddog/', str(gd) + '/')
                   .replace('/var/lib/takguard', str(state))
                   .replace('/var/log/takguard', str(logs))
                   .replace('/proc/', str(proc) + '/')
                   .replace('/tmp/', str(tmp_path) + '/'))
    assert str(state) in script and script.count(str(proc)) == WATCH.count('/proc/') >= 3
    sp = tmp_path / 'watch.sh'
    sp.write_text(script)

    def tick(curl='503 12.5 0', psi=None, load='1.00'):
        (tmp_path / 'curl').write_text(curl)
        pressure = proc / 'pressure' / 'cpu'
        if psi is None:
            if pressure.exists():
                pressure.unlink()
        else:
            pressure.parent.mkdir(exist_ok=True)
            pressure.write_text('some avg10=80.00 avg60=%.2f avg300=83.01 total=1\n'
                                'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n' % psi)
        (proc / 'loadavg').write_text('%s 40.00 30.00 9/999 1234\n' % load)
        if calls.exists():
            calls.unlink()
        env = {'PATH': '%s:/usr/bin:/bin:/usr/sbin:/sbin' % stubs}
        r = subprocess.run(['bash', str(sp)], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        return calls.read_text() if calls.exists() else ''

    def log():
        p = logs / 'restarts.log'
        return p.read_text() if p.exists() else ''

    def age(name, secs):
        (state / name).write_text('%d\n' % (time.time() - secs))

    tick.log, tick.age, tick.state = log, age, state
    return tick


def _restarts(calls):
    return [l for l in calls.splitlines()
            if l.startswith('docker compose') and ('restart' in l or 'down' in l or ' up ' in l)]


def test_starved_host_holds_instead_of_restarting(box):
    for _ in range(3):
        calls = box(psi=83.0)
    assert _restarts(calls) == []
    assert 'NOT restarting' in box.log() and 'CPU pressure 83%' in box.log()
    assert calls.count('alert Guard Dog: Authentik slow, host CPU overloaded') == 1
    calls = box(psi=83.0)                                 # still starved: no restart, no repeat alert
    assert _restarts(calls) == [] and 'alert' not in calls


def test_load_average_is_the_fallback_without_psi(box):
    for _ in range(3):
        calls = box(psi=None, load='50.12')               # RHEL 9: PSI disabled by default
    assert _restarts(calls) == []
    assert 'load average 50 on 8 CPUs' in box.log()


def test_recovery_clears_the_hold(box):
    for _ in range(3):
        box(psi=83.0)
    box(curl='200 0.02 0', psi=83.0)
    assert 'recovered without a restart' in box.log()
    assert not (box.state / 'authentik_hold_since').exists()


def test_connection_refused_restarts_even_when_starved(box):
    for _ in range(3):
        calls = box(curl='000 0.00 7', psi=83.0)
    assert any('restart -t 30 server worker' in l for l in _restarts(calls))


def test_hold_is_capped_at_two_hours(box):
    for _ in range(3):
        box(psi=83.0)
    box.age('authentik_hold_since', 7201)
    calls = box(psi=83.0)
    assert _restarts(calls) and 'restarting anyway' in box.log()


def test_first_restart_touches_only_server_and_worker(box):
    for _ in range(3):
        calls = box(psi=5.0)
    assert [l for l in _restarts(calls)] == ['docker compose restart -t 30 server worker']
    assert 'server-worker restart' in box.log()


def test_missing_postgres_goes_straight_to_full_restart(box, tmp_path):
    (tmp_path / 'running').write_text('redis\nserver\nworker\n')
    for _ in range(3):
        calls = box(psi=5.0)
    assert 'docker compose down --timeout 30' in calls


def test_repeat_restarts_back_off_and_escalate(box):
    for _ in range(3):
        box(psi=5.0)                                      # restart 1: server+worker
    box.age('authentik_last_restart', 1000)               # past the old 15-min cooldown...
    for _ in range(3):
        calls = box(psi=5.0)
    assert _restarts(calls) == []                         # ...but restart 2 now waits 30 min
    box.age('authentik_last_restart', 1801)
    calls = box(psi=5.0)
    assert 'docker compose down --timeout 30' in calls    # second in 6h escalates to full
    assert 'restart 2 in 6h; next allowed after 60 min' in box.log()
