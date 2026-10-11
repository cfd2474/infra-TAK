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
"""The daily console restart waits for module operations (#113).

Field report (2026-10-11, v10.2.10-alpha): the idle-gated 04:00 restart bounced
the console while an ATLAS update was at Step 2/3, killed its `docker compose
up` and left the deployment half-recreated. The gate only scanned app.py's own
`*_status` globals; ATLAS keeps its update state in modules/atlas.py.

app.py cannot be imported in a test, so the gate is cut out of app.py by its
markers and run with the REAL module files loaded — the slots it is checked
against are the ones a real update sets.
"""

import os
import pathlib
import sys
import time
import types
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import modules as mod_registry  # noqa: E402
import modules.atlas as atlas  # noqa: E402
import modules.simulator as simulator  # noqa: E402
import modules.tvr as tvr  # noqa: E402

APP = (ROOT / 'app.py').read_text(encoding='utf-8')
START = "# Issue #113: the daily restart's idle gate"
END = "@app.route('/api/console/restart-safe')"
BLOCK = APP[APP.index(START):APP.index(END)]
FMT = '%Y-%m-%dT%H:%M:%SZ'


@pytest.fixture
def h():
    results = {}
    ns = {'os': os, 'time': time, 'datetime': datetime, '_sys': sys,
          'mod_registry': mod_registry, '_mod_results_load': lambda: results}
    exec(compile(BLOCK, 'app.py:restart-gate', 'exec'), ns)
    ns['_results'] = results
    yield ns
    # leave the real module state as we found it
    for slots in (atlas._update_slots, atlas._removal_slots):
        for slot in slots.values():
            slot['running'] = False
    for d in (atlas._update_all_status, tvr._update_status, simulator._rebuild_status):
        d['running'] = False
    for j in mod_registry._JOBS.values():
        j['running'] = False


def _record(h, module, outcome='running', started=None, kind='update'):
    started = started or datetime.utcnow()
    h['_results'][module] = {'last': {'module': module, 'kind': kind, 'outcome': outcome,
                                      'started_at': started.strftime(FMT)}, 'history': []}


def test_idle_console_is_safe_to_restart(h):
    assert h['_console_busy_reason']() is None


def test_atlas_update_in_flight_defers_the_restart(h):
    # The #113 case: an ATLAS update, which only ever touched modules/atlas.py.
    atlas._update_slot(None)['running'] = True
    reason = h['_console_busy_reason']()
    assert reason and 'atlas' in reason


def test_atlas_removal_in_flight_defers_the_restart(h):
    atlas._removal_slot(None)['running'] = True
    assert h['_console_busy_reason']()


def test_atlas_update_all_defers_the_restart(h):
    atlas._update_all_status['running'] = True
    assert 'atlas' in h['_console_busy_reason']()


@pytest.mark.parametrize('status', [lambda: tvr._update_status,
                                    lambda: simulator._rebuild_status])
def test_other_module_files_status_dicts_count(h, status):
    status()['running'] = True
    assert h['_console_busy_reason']()


def test_registry_deploy_or_uninstall_defers_the_restart(h):
    mod_registry._job('atlas')['running'] = True
    assert h['_console_busy_reason']() == 'module atlas running'


def test_app_status_globals_still_count(h):
    h['cloudtak_deploy_status'] = {'running': True}
    assert h['_console_busy_reason']() == 'cloudtak_deploy_status running'


def test_a_running_module_result_defers_the_restart(h):
    _record(h, 'takserver')
    assert h['_console_busy_reason']() == 'takserver update running'


def test_finished_or_interrupted_results_do_not_hold_it_off(h):
    _record(h, 'atlas-sample', outcome='failed')
    _record(h, 'authentik', outcome='interrupted')
    _record(h, 'tvr', outcome='ok')
    assert h['_console_busy_reason']() is None


def test_a_stale_running_record_does_not_block_the_restart_forever(h):
    # A thread that died without recording its end must not stop the
    # wedged-worker recovery the restart exists for.
    _record(h, 'atlas', started=datetime.utcnow() - timedelta(hours=4))
    assert h['_console_busy_reason']() is None


def test_a_malformed_record_does_not_block_the_restart(h):
    h['_results']['atlas'] = {'last': {'outcome': 'running', 'started_at': 'garbage'}}
    h['_results']['tvr'] = {'last': {'outcome': 'running'}}
    assert h['_console_busy_reason']() is None


def test_the_route_uses_the_gate():
    i = APP.index(END)
    route = APP[i:APP.index('\n@app.route', i + 1)]
    assert 'busy = _console_busy_reason()' in route
    assert "'safe': busy is None" in route


def test_module_files_keep_their_job_state_where_the_gate_looks():
    # A rename here (say `_update_slots` -> `_updates`) would quietly reopen #113.
    assert isinstance(atlas._update_slots, dict) and isinstance(atlas._removal_slots, dict)
    for mod in (atlas, tvr, simulator):
        for name, val in vars(mod).items():
            if isinstance(val, dict) and 'running' in val and not callable(val):
                assert name.endswith('_status'), f'{mod.__name__}.{name} is invisible to the gate'
