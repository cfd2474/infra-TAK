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
"""Help → Diagnostics fixes (v10.2.2).

10.2.1's Guard Dog section tailed /var/log/takguard/watchdog.log, which no Guard
Dog script writes, so every box (root included — lutak.net, 2026-10-01) said
"not readable by the console". Guard Dog's event log is restarts.log. A missing
file and an unreadable one are now told apart, and the Box section says what is
using the CPU (that report had load 28 on 12 CPUs and no way to say why).
"""

import os
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')


def _cut(name):
    return re.search(rf'^def {name}\(.*?(?=^\S)', APP, re.S | re.M).group(0)


def _ns(**extra):
    ns = {'os': os, 're': re, '_DIAG_TAIL_BYTES': 1 << 20}
    ns.update(extra)
    for n in ('_diag_tail', '_diag_unreadable', '_diag_top', '_diag_section_guarddog'):
        exec(compile(_cut(n), 'app.py:' + n, 'exec'), ns)
    return ns


def test_no_guard_dog_script_writes_watchdog_log_and_diagnostics_no_longer_reads_it():
    writers = [p for p in (REPO / 'scripts' / 'guarddog').iterdir()
               if 'watchdog.log' in p.read_text(errors='replace')]
    assert writers == []
    assert 'watchdog.log' not in re.sub(r'#.*', '', _cut('_diag_section_guarddog'))
    assert "'/var/log/takguard/restarts.log'" in _cut('_diag_section_guarddog')


def test_guarddog_section_lists_restarts_and_alerts_not_fail2ban_bans(tmp_path, monkeypatch):
    gd = tmp_path / 'opt-tak-guarddog'
    gd.mkdir()
    log = tmp_path / 'restarts.log'
    log.write_text(
        'Thu Oct  1 01:39:47 PM UTC 2026: RETENTION-GUARD: batched delete complete\n'
        '2026-10-01T13:44:53Z | fail2ban: Banned 77.239.124.71 (Authentik brute-force)\n'
        '2026-10-01T13:50:00Z | restart | Node-RED unhealthy (HTTP 502) — restarting container\n'
        'Thu Oct  1 02:00:00 PM UTC 2026: TAK Server missing processes: api (3 failures) - restarting\n')
    src = _cut('_diag_section_guarddog').replace("'/opt/tak-guarddog'", repr(str(gd))) \
                                         .replace("'/var/log/takguard/restarts.log'", repr(str(log)))
    ns = _ns(_diag_cmd=lambda *a, **k: 'tak-a.timer\ntak-b.timer')
    exec(compile(src, 'gd', 'exec'), ns)
    out = ns['_diag_section_guarddog']({'guarddog_deployed_version': '10.2.2-alpha'})
    assert out[1] == 'timers: 2'
    assert out[2] == 'restarts.log: 2 restart/alert line(s) in the recent tail'
    assert 'Node-RED unhealthy' in out[3] and 'missing processes' in out[4]
    assert not any('Banned' in l for l in out)


def test_missing_and_unreadable_are_different_answers(tmp_path):
    ns = _ns()
    assert ns['_diag_unreadable'](str(tmp_path / 'nope.log')) == 'missing'
    p = tmp_path / 'locked.log'
    p.write_text('x')
    os.chmod(p, 0)
    try:
        if os.geteuid() != 0:
            assert ns['_diag_tail'](str(p)) is None
        assert ns['_diag_unreadable'](str(p)) == 'not readable by the console on this box'
    finally:
        os.chmod(p, 0o600)


TOP = """top - 12:50:30 up 1 day,  3 users,  load average: 28.57, 21.06, 18.11
%Cpu(s):  2.0 us,  1.0 sy,  0.0 ni, 96.0 id,  1.0 wa,  0.0 hi,  0.0 si,  0.0 st
    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
      1 root      20   0  167000  12000   8000 S   0.0   0.0   1:00.00 systemd

top - 12:50:31 up 1 day,  3 users,  load average: 28.57, 21.06, 18.11
%Cpu(s): 61.2 us,  9.3 sy,  0.0 ni,  4.1 id, 25.0 wa,  0.0 hi,  0.4 si,  0.0 st
MiB Mem :  48000.0 total,  40000.0 free
    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
  87273 tak       20   0   12.1g   4.1g  30000 S 812.0   8.6 900:00.00 java
   2201 70        20   0  300000  90000  80000 R  95.0   0.2   1:00.00 postgres
   3301 root      20   0  900000  50000  20000 S  12.0   0.1   9:00.00 dockerd
"""


def test_top_reports_the_second_sample_and_program_names_only():
    ns = _ns(_diag_cmd=lambda *a, **k: TOP)
    out = ns['_diag_top']()
    assert out[0].startswith('%Cpu(s): 61.2 us') and '25.0 wa' in out[0]   # 2nd iteration, iowait visible
    assert out[1].startswith('busiest processes')
    assert out[2].split() == ['812.0', '8.6', 'tak', 'java']
    assert out[3].split()[3] == 'postgres'
    assert len(out) == 5


def test_top_failure_is_one_line_not_an_exception():
    ns = _ns(_diag_cmd=lambda *a, **k: '(top failed: not found)')
    assert ns['_diag_top']() == ['top: (top failed: not found)']


def test_top_is_called_without_command_lines():
    body = _cut('_diag_top')
    assert "'-c'" not in body                  # full command lines can carry secrets
    assert "'-n', '2'" in body
