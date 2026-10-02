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
"""Our own Authentik safety nets stop re-arming a storm (v10.2.2 W10).

test6, 2026-10-02 03:19–03:53Z: an autotune UP step recreated server+worker; with
the LDAP outpost's bind cache cold, TAK's normal ~2 service-account binds/s each ran
a full password-hashing flow, the server pinned at 762% CPU and answered 502, so the
cache could not refill. The idle-in-tx watchdog then counted those busy transactions
(idle 0–3 s each) as "abandoned", restarted the server five times in 25 minutes and
emptied the cache each time. The box recovered by itself once the restarts stopped.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')


def _const(name):
    return int(re.search(rf'^{name} = (\d+)', APP, re.M).group(1))


def test_watchdog_counts_only_stale_idle_in_transaction():
    assert _const('_AUTHENTIK_IDLE_IN_TX_STALE_S') == 30
    i = APP.index("COUNT(*) FILTER (WHERE state='idle'), ")
    q = APP[i:i + 400]
    assert "state='idle in transaction'" in q
    assert "now() - state_change > interval '%d seconds'" in q
    assert '% _AUTHENTIK_IDLE_IN_TX_STALE_S' in q


def test_up_steps_wait_six_hours_between_recreates():
    assert _const('_AUTHENTIK_MAX_REQUESTS_TUNE_UP_COOLDOWN_S') == 21600
    assert _const('_AUTHENTIK_MAX_REQUESTS_TUNE_DOWN_COOLDOWN_S') == 120   # fast-down unchanged


def _evaluate(now, current, last_tune, fires):
    src = re.search(r'^def _authentik_max_requests_autotune_evaluate\(.*?(?=^def )', APP, re.S | re.M).group(0)
    settings = {'authentik_max_requests_last_tune_ts': last_tune,
                'authentik_max_requests_fire_history': fires}
    ns = {'load_settings': lambda: settings,
          '_authentik_max_requests_get_current': lambda: (current, max(5, current // 20)),
          '_autotune_log': lambda m: None,
          '_AUTHENTIK_MAX_REQUESTS_FLOOR_DEFAULT': 100, '_AUTHENTIK_MAX_REQUESTS_CEILING_DEFAULT': 1000}
    for n in ('_AUTHENTIK_MAX_REQUESTS_TUNE_DOWN_COOLDOWN_S', '_AUTHENTIK_MAX_REQUESTS_TUNE_UP_COOLDOWN_S',
              '_AUTHENTIK_MAX_REQUESTS_QUIET_WINDOW_S', '_AUTHENTIK_MAX_REQUESTS_FIRE_LOOKBACK_S'):
        ns[n] = _const(n)
    exec(compile(src, 'app.py:evaluate', 'exec'), ns)
    import time
    real = time.time
    time.time = lambda: now
    try:
        return ns['_authentik_max_requests_autotune_evaluate']()
    finally:
        time.time = real


def test_quiet_box_no_longer_recreates_every_30_minutes():
    now = 1_800_000_000
    fire = now - 7 * 3600                                   # quiet for 7 h
    # the test6 night: last UP step 32 min ago -> the old code stepped again here
    assert _evaluate(now, 156, now - 32 * 60, [fire]) == (None, None, None)
    # six hours after the previous step it does ease up
    new, _, reason = _evaluate(now, 156, now - 6 * 3600 - 1, [fire])
    assert new == 195 and reason.startswith('UP:')


def test_a_new_fire_still_halves_fast():
    now = 1_800_000_000
    new, _, reason = _evaluate(now, 400, now - 600, [now - 60])
    assert new == 200 and reason.startswith('DOWN:')
