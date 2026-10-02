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
"""A snapshot's cot dump can't stall silently, and says what stalled it (v10.2.3).

Field, lutak.net 2026-10-01 and -02 (root console, native 5.7, PostgreSQL 15, 495 MB):
the 5.8 pre-migration backup ran pg_dump for the full window both days (300 s, then
600 s) and failed with "did not finish within 10 min — A long VACUUM FULL / repack …
is the usual cause". A dump that size takes well under a minute; one that connects and
makes no progress is waiting for a table lock, which pg_dump does FOREVER by default.
And a timeout killed only runuser — pg_dump itself lived on, holding its locks.
"""

import importlib.util
import os
import pathlib
import re
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')
BROKER_PY = REPO / 'broker' / 'takwerx_broker.py'


def _cut(name):
    return re.search(rf'^def {name}\(.*?(?=^\S)', APP, re.S | re.M).group(0)


def _code(body):
    return re.sub(r'#.*', '', body)


@pytest.fixture(scope='module')
def broker():
    spec = importlib.util.spec_from_file_location('takwerx_broker_under_test', BROKER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_tools(tmp_path, pg_dump_body):
    """A runuser that FORKS its command and waits (as util-linux's does, to close the
    PAM session) and a pg_dump with the given body."""
    pidfile = tmp_path / 'pg_dump.pid'
    runuser = tmp_path / 'runuser'
    runuser.write_text('#!/bin/sh\nshift 3\n"$@" &\necho $! > %s\nwait $!\n' % pidfile)
    pg_dump = tmp_path / 'pg_dump'
    pg_dump.write_text('#!/bin/sh\n' + pg_dump_body)
    for f in (runuser, pg_dump):
        f.chmod(0o755)
    return {'runuser': str(runuser), 'pg_dump': str(pg_dump)}, pidfile


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); ps says whether it is really running
    st = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout
    return bool(st.strip()) and not st.strip().startswith('Z')


def test_broker_timeout_kills_pg_dump_itself_not_just_runuser(broker, tmp_path, monkeypatch):
    tools, pidfile = _fake_tools(tmp_path, 'exec sleep 60\n')
    monkeypatch.setattr(broker.shutil, 'which', lambda n, path=None: tools.get(n))
    dump = tmp_path / 'snap' / 'cot.pgdump'
    t0 = time.time()
    res = broker._do_pg_dump({'path': str(dump), 'db': 'cot', 'timeout': 1})
    assert time.time() - t0 < 10
    assert res == {'ok': False, 'error': 'pg_dump did not finish within 1 s'}
    assert not dump.exists()                       # no truncated dump left behind
    pid = int(pidfile.read_text())
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid), 'pg_dump outlived the timeout (orphaned, still holding its locks)'


def test_broker_pg_dump_gives_up_on_locks_and_its_error_reaches_the_console(broker, tmp_path, monkeypatch):
    # The failure path: pg_dump's own stderr (which names the table it could not lock)
    # is the error. Popen's .stderr is a pipe object, not bytes — reading it there was
    # an AttributeError waiting to happen.
    tools, _ = _fake_tools(tmp_path, 'echo "args: $*" >&2\nexit 1\n')
    monkeypatch.setattr(broker.shutil, 'which', lambda n, path=None: tools.get(n))
    dump = tmp_path / 'snap' / 'cot.pgdump'
    res = broker._do_pg_dump({'path': str(dump), 'db': 'cot', 'timeout': 30})
    assert res['ok'] is False and res['returncode'] == 1
    assert res['error'] == 'args: --lock-wait-timeout=120s -Fc cot\n'
    assert not dump.exists()


def test_container_dump_carries_the_same_lock_wait(broker):
    body = _code(BROKER_PY.read_text()[BROKER_PY.read_text().index('def _do_pg_dump('):])
    body = body[:body.index('\ndef ')]
    assert "'pg_dump', '--lock-wait-timeout=' + _PG_DUMP_LOCK_WAIT, '-Fc', db]" in body
    assert body.count("'--lock-wait-timeout=' + _PG_DUMP_LOCK_WAIT") == 2


def test_console_and_broker_wait_the_same_time_for_locks(broker):
    m = re.search(r"^_COT_DUMP_LOCK_WAIT = '([^']+)'", APP, re.M)
    assert m and m.group(1) == broker._PG_DUMP_LOCK_WAIT == '120s'


def test_root_console_dump_gives_up_on_locks_kills_its_group_and_reports():
    body = _code(_cut('_tak_snapshot'))
    root = body[body.index("['runuser', '-u', 'postgres', '--', 'pg_dump',"):]
    root = root[:root.index('Certificates') if 'Certificates' in root else 3000]
    assert "'--lock-wait-timeout=' + _COT_DUMP_LOCK_WAIT, '-Fc', 'cot']" in root
    assert 'start_new_session=True' in root and "cwd='/'" in root and 'shell=True' not in root
    assert 'communicate(timeout=600)' in root
    to = root[root.index('except subprocess.TimeoutExpired'):]
    # the report runs BEFORE the kill — afterwards pg_dump's own session is gone
    assert to.index('_cot_stall_report()') < to.index('os.killpg(_p.pid, _signal.SIGKILL)')
    assert root.count('_cot_stall_report()') == 2   # timeout AND a failed exit


def test_broker_path_failure_reports_too():
    body = _code(_cut('_tak_snapshot'))
    br = body[body.index('_pg_dump_priv(pg_dump_path'):]
    br = br[:br.index('import signal as _signal')]
    assert br.index('except Exception as _pg_e:') < br.index('_cot_stall_report()')


def test_the_guess_is_gone():
    assert 'is the usual cause' not in _cut('_tak_snapshot')


# ── _cot_stall_report: what the panel actually says ─────────────────────────────

class _R:
    def __init__(self, rc=0, out='', err=''):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _report(result=None, raises=None):
    seen = {}

    def fake_pg_exec(args, timeout=30, **kw):
        seen['args'], seen['timeout'] = args, timeout
        if raises:
            raise raises
        return result
    ns = {'_pg_exec': fake_pg_exec, '_COT_STALL_SQL': 'SQL', 're': re}
    exec(compile(_cut('_cot_stall_report'), 'app.py:_cot_stall_report', 'exec'), ns)
    return ns['_cot_stall_report'](), seen


def _row(*f):
    return '\x1f'.join(str(x) for x in f)


def test_report_names_the_session_pg_dump_is_stuck_behind():
    out = '\n'.join([
        _row(4242, 'psql', 'local', 'idle in transaction', 'Client:ClientRead', 93600, '', '',
             'public.cot_router', 'LOCK TABLE cot_router IN ACCESS EXCLUSIVE MODE;'),
        _row(5151, 'pg_dump', 'local', 'active', 'Lock:relation', 598, '4242',
             'AccessShareLock on public.cot_router', '',
             'LOCK TABLE public.cot_router IN ACCESS SHARE MODE'),
    ])
    lines, seen = _report(_R(0, out + '\n'))
    assert seen['args'] == ['psql', '-tAX', '-d', 'cot', '-c', 'SQL'] and seen['timeout'] == 20
    assert lines[0].startswith('cot database — sessions that can stall a dump')
    assert lines[1] == ('  pid 4242 psql (local) idle in transaction, transaction open 26 h, '
                        'wait Client:ClientRead, HOLDS an exclusive lock on public.cot_router'
                        ' — LOCK TABLE cot_router IN ACCESS EXCLUSIVE MODE;')
    assert lines[2] == ('  pid 5151 pg_dump (local) active, transaction open 9 min, '
                        'WAITING for AccessShareLock on public.cot_router, blocked by pid 4242'
                        ' — LOCK TABLE public.cot_router IN ACCESS SHARE MODE')
    assert lines[3].startswith('  → pg_dump is blocked by pid 4242 (psql).')
    assert len(lines) == 4                      # not TAK's connection → no TAK restart advice


def test_report_says_restart_tak_when_the_holder_is_tak_itself():
    out = '\n'.join([
        _row(77, 'PostgreSQL JDBC Driver', '127.0.0.1', 'idle in transaction', '', 4000, '', '',
             'public.mission', 'ALTER TABLE mission ADD COLUMN x int'),
        _row(88, 'pg_dump', 'local', 'active', 'Lock:relation', 30, '77',
             'AccessShareLock on public.mission', '', 'LOCK TABLE public.mission IN ACCESS SHARE MODE'),
    ])
    lines, _ = _report(_R(0, out))
    assert '  → pg_dump is blocked by pid 77 (PostgreSQL JDBC Driver).' in lines[-2]
    assert lines[-1].startswith("  → that is TAK Server's own database connection — restart TAK Server")


def test_report_masks_string_literals_but_keeps_the_statement():
    out = _row(9, 'psql', 'local', 'idle in transaction', '', 60, '', '', 'public.cot_router',
               "ALTER ROLE martiuser PASSWORD 's3cr''et'; UPDATE cot_router SET detail='<x callsign=\"A")
    lines, _ = _report(_R(0, out))
    assert lines[1].endswith("— ALTER ROLE martiuser PASSWORD '…'; UPDATE cot_router SET detail='…'")
    assert 's3cr' not in '\n'.join(lines) and 'callsign' not in '\n'.join(lines)


def test_report_when_nothing_is_stuck_and_when_it_cannot_ask():
    assert _report(_R(0, ''))[0] == ['cot database: nothing is waiting on a lock, holding an '
                                     'exclusive lock, or sitting in a long transaction']
    assert _report(_R(2, '', 'psql: error: connection refused'))[0] == [
        '(could not ask the cot database what it is doing: psql: error: connection refused)']
    assert _report(raises=subprocess.TimeoutExpired('psql', 20))[0][0].startswith(
        '(could not ask the cot database what it is doing: ')
    # a row with a stray separator count is skipped, not mis-parsed
    assert _report(_R(0, 'garbage\x1fline'))[0][0].startswith('cot database: nothing')


def test_stall_sql_is_read_only_and_passes_the_broker_psql_gate(broker):
    sql = re.search(r'^_COT_STALL_SQL = \((.*?)^\)', APP, re.S | re.M).group(1)
    sql = ''.join(re.findall(r'"((?:[^"\\]|\\.)*)"', sql)).replace('\\\\', '\\')
    assert sql.lstrip().upper().startswith('SELECT ')
    bare = re.sub(r"'[^']*'", "''", sql).replace('pg_locks', '')
    assert not re.search(r'\b(LOCK|UPDATE|DELETE|INSERT|ALTER|DROP|TRUNCATE|SET|'
                         r'pg_terminate_backend|pg_cancel_backend)\b', bare, re.I)
    broker._check_runuser(['runuser', '-u', 'postgres', '--', 'psql', '-tAX', '-d', 'cot', '-c', sql])


def test_diagnostics_shows_cot_sessions_only_when_the_database_is_local():
    body = _code(_cut('_diag_section_takserver'))
    assert "if _mode in ('two_server', 'external_db'):" in body
    assert 'out.extend(_cot_stall_report())' in body
