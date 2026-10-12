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
"""The snapshot's cot dump gets a window sized to the database (infra-TAK #126).

Field, v10.2.11 (root console, native 5.7, PostgreSQL 15, 14 GB cot): the 5.8 pre-migration
pg_dump was killed at the fixed 600 s while still in COPY public.cot_router with 1.2 GB
written, so the snapshot had no dump and the upgrade refused to start. The stall report
then listed that working pg_dump as if it were the thing stalling the backup.
"""

import importlib.util
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')
BROKER_PY = REPO / 'broker' / 'takwerx_broker.py'
GB = 1 << 30


def _cut(name):
    return re.search(rf'^def {name}\(.*?(?=^\S)', APP, re.S | re.M).group(0)


def _code(body):
    return re.sub(r'#.*', '', body)


def _const(name):
    return eval(re.search(rf'^{name} = (.+)$', APP, re.M).group(1))


@pytest.fixture(scope='module')
def window():
    ns = {k: _const(k) for k in ('_COT_DUMP_MIN_S', '_COT_DUMP_S_PER_GB', '_COT_DUMP_MAX_S')}
    exec(compile(_cut('_cot_dump_timeout') + _cut('_cot_dump_window_text'), 'app.py', 'exec'), ns)
    return ns


def test_window_scales_with_size_between_floor_and_cap(window):
    t = window['_cot_dump_timeout']
    assert t(0) == 600                              # small boxes: the old 10 min
    assert t(495 * (1 << 20)) == 600 + 495 * 900 // 1024   # lutak.net-sized: ~17 min
    assert t(14 * GB) == 600 + 14 * 900             # the #126 box: 3.7 h, not 10 min
    assert t(14 * GB) > 3 * 3600                    # reporter's own estimate tops out at ~3 h
    assert t(100 * GB) == 6 * 3600                  # never past the migration's own 6 h
    assert t(None) == 6 * 3600                      # unknown size must not fail a good dump


def test_window_text(window):
    txt = window['_cot_dump_window_text']
    assert txt(600) == '10 min' and txt(13200) == '3.7 h' and txt(6 * 3600) == '6.0 h'


def test_every_dump_branch_uses_the_sized_window():
    body = _code(_cut('_tak_snapshot'))
    assert '_cot_db_size_for_dump(_snap_tak_cfg, _snap_edb)' in body
    assert body.index('_dump_to = _cot_dump_timeout(') < body.index('if _snap_edb.get(\'host\'):')
    assert "stderr=subprocess.PIPE, timeout=_dump_to, env=_pg_env)" in body      # external DB
    assert "r2 = subprocess.run(_ssh_parts, stdout=_f, stderr=subprocess.PIPE, timeout=_dump_to)" in body
    assert "_pg_dump_priv(pg_dump_path, db='cot', timeout=_dump_to," in body     # broker
    assert 'communicate(timeout=_dump_to)' in body                                # root
    assert 'did not finish within {_dump_win}' in body
    assert 'within 10 min' not in body and '30–90s' not in body


def test_remote_dumps_give_up_on_locks_like_the_local_ones():
    # A multi-hour window is only safe if a dump stuck behind a lock still fails in 120 s.
    body = _code(_cut('_tak_snapshot'))
    assert "'sudo -u postgres pg_dump --lock-wait-timeout=%s -Fc cot' % _COT_DUMP_LOCK_WAIT" in body
    assert "[_pgdump, '--lock-wait-timeout=' + _COT_DUMP_LOCK_WAIT, '-Fc'," in body


def test_size_probe_asks_the_server_the_dump_reads():
    seen = []

    def fake_cotdb_psql(cfg, sql, timeout=25):
        seen.append(('cotdb', cfg.get('mode'), sql))
        return True, '15032385536'

    class _R:
        returncode, stdout = 0, '7516192768\n'

    def fake_run(argv, **kw):
        seen.append(('tcp', argv, kw['env']['PGPASSWORD']))
        return _R()

    import os
    ns = {'_cotdb_psql': fake_cotdb_psql, 'os': os,
          'subprocess': type('S', (), {'run': staticmethod(fake_run)})}
    exec(compile(_cut('_cot_db_size_for_dump'), 'app.py', 'exec'), ns)
    size = ns['_cot_db_size_for_dump']
    assert size({'mode': 'two_server'}, {}) == 15032385536
    assert seen[-1] == ('cotdb', 'two_server', "SELECT pg_database_size('cot');")
    assert size({'mode': 'external_db'}, {'host': 'db.example', 'password': 'pw'}) == 7516192768
    kind, argv, pw = seen[-1]
    assert kind == 'tcp' and argv[:3] == ['psql', '-h', 'db.example'] and pw == 'pw'
    assert '--no-password' in argv
    # a failed or odd answer is "unknown", never an exception
    ns['_cotdb_psql'] = lambda *a, **k: (False, 'psql: error')
    assert size({'mode': 'local'}) is None
    ns['_cotdb_psql'] = lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('psql', 20))
    assert size({'mode': 'local'}) is None


# ── broker ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def broker():
    spec = importlib.util.spec_from_file_location('takwerx_broker_window_test', BROKER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_broker_honours_a_window_past_ten_minutes(broker, tmp_path, monkeypatch):
    # The old clamp turned any requested window into 600 s. Prove the requested value is
    # what reaches communicate() — and that the cap still holds above 6 h.
    seen = []

    class _P:
        pid, returncode = 0, 0

        def __init__(self, *a, **k):
            pass

        def communicate(self, timeout=None):
            seen.append(timeout)
            return None, b''
    monkeypatch.setattr(broker.subprocess, 'Popen', _P)
    monkeypatch.setattr(broker.shutil, 'which', lambda n, path=None: '/bin/' + n)
    monkeypatch.setattr(broker, '_dump_hmac', lambda p: 'x')
    dump = tmp_path / 'snap' / 'cot.pgdump'
    for asked in (13200, 99999, None):
        assert broker._do_pg_dump({'path': str(dump), 'db': 'cot', 'timeout': asked})['ok']
    assert seen == [13200, 6 * 3600, 600]


def test_console_and_broker_share_the_cap(broker):
    assert broker._PG_DUMP_MAX_TIMEOUT == _const('_COT_DUMP_MAX_S') == 6 * 3600


# ── stall report: a working pg_dump is not a stall ─────────────────────────────

def _stall_sql():
    sql = re.search(r'^_COT_STALL_SQL = \((.*?)^\)', APP, re.S | re.M).group(1)
    return ''.join(re.findall(r'"((?:[^"\\]|\\.)*)"', sql)).replace('\\\\', '\\')


def test_stall_sql_no_longer_lists_every_pg_dump():
    where = _stall_sql().split(' WHERE ', 1)[1]
    assert "application_name = 'pg_dump' OR" not in where
    # long-running / idle-in-transaction only count for sessions that are NOT pg_dump
    guard = "a.application_name IS DISTINCT FROM 'pg_dump' AND ("
    assert guard in where
    tail = where[where.index(guard):]
    assert tail.index('idle in transaction') > 0 and tail.index("a.state = 'active'") > 0
    # a pg_dump that IS waiting on a lock still shows up (and names its blocker)
    assert where.index("a.wait_event_type = 'Lock'") < where.index(guard)
    assert 'pg_blocking_pids' in where and 'AccessExclusiveLock' in where


def test_stall_sql_still_passes_the_broker_psql_gate(broker):
    broker._check_runuser(['runuser', '-u', 'postgres', '--', 'psql', '-tAX', '-d', 'cot',
                           '-c', _stall_sql()])
