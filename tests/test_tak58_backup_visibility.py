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
"""TAK 5.8 upgrade: the pre-migration backup says why it failed (v10.2.2).

Field, 2026-10-01 (root console, native 5.7, PostgreSQL 15, 495 MB): Update TAK
Server stopped at "the snapshot completed but captured NO database dump" and the
panel showed nothing else. Two faults:

1. Driven by the Update button, the migration's `_say` writes to the page's panel,
   but the backup (and the package install) were handed the module-level
   tak58_log, which nothing displays — so the pg_dump error never reached anyone.
2. A ROOT console dumped with a shell `sudo -u postgres pg_dump` capped at 300 s,
   unlike the broker path (runuser argv, 600 s). No dev box runs as root.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')


def _func(name):
    return re.search(rf'^def {name}\(.*?(?=^def )', APP, re.S | re.M).group(0)


def _code(body):
    return re.sub(r'#.*', '', body)


def test_native_migration_logs_backup_and_install_to_the_panel_it_was_given():
    body = _code(_func('run_takserver_58_migration'))
    assert '_tak_58_backup(plog=_say)' in body
    assert '_log, timeout_sec=1800)' in body
    assert 'tak58_log, timeout_sec' not in body
    # the only remaining reference is the default for the standalone API route
    assert re.findall(r'\b_?tak58_log\b', body) == ['tak58_log']


def test_root_snapshot_dump_matches_the_broker_path():
    body = _code(_func('_tak_snapshot'))
    # the two-server branch runs sudo on the REMOTE box over SSH; the local shell form is gone
    assert not re.search(r"'sudo -u postgres pg_dump -Fc cot',\s*shell=True", body)
    root = body[body.index("['runuser', '-u', 'postgres', '--', 'pg_dump',"):]
    root = root[:root.index('# 5. Certificates') if '# 5. Certificates' in root else 600]
    assert "cwd='/'" in root and 'timeout=600' in root and 'shell=True' not in root
    assert 'TimeoutExpired' in body                  # a timeout is reported, not swallowed


def test_broker_pg_dump_window_is_the_same_600_seconds():
    broker = (REPO / 'broker' / 'takwerx_broker.py').read_text()
    do = broker[broker.index('def _do_pg_dump('):]
    assert "min(int(req.get('timeout') or DEFAULT_TIMEOUT), DEFAULT_TIMEOUT)" in do
    assert re.search(r'^DEFAULT_TIMEOUT = 600\b', broker, re.M)
