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
"""The /authentik page renders without `docker stats` (v10.2.2 W5).

`docker stats --no-stream` cost every render ~2 s (up to 20 s over SSH). The
cards render a placeholder and the page fills them from the existing
/api/authentik/container-stats route, which the page already polled.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')
TPL = (REPO / 'templates' / 'authentik.html').read_text(encoding='utf-8')


def _func(name):
    return re.search(rf'^def {name}\(.*?(?=^(?:def |@app\.))', APP, re.S | re.M).group(0)


def test_page_render_runs_no_docker_stats_and_no_remote_vcpu_probe():
    body = re.sub(r'#.*', '', _func('authentik_page'))
    assert 'docker stats' not in body
    assert '_get_vcpu_count_remote' not in body


def test_stats_still_served_by_the_json_route():
    body = _func('authentik_container_stats')
    assert 'docker stats --no-stream' in body
    route = APP[:APP.index('def authentik_container_stats(')].rsplit('@app.route', 1)[1]
    assert "'/api/authentik/container-stats'" in route and '@login_required' in route


def test_every_card_has_a_metrics_slot_the_page_fills():
    card = TPL[TPL.index('{% for c in container_info.containers %}'):]
    card = card[:card.index('{% endfor %}')]
    assert '<div id="svc-metrics-{{ c.name }}"' in card
    assert "{% if 'cpu_sys' in c %}" not in card            # no longer conditional on render-time stats
    assert "fetch('/api/authentik/container-stats')" in TPL
    assert re.search(r"querySelector\('\[id\^=\"svc-metrics-\"\]'\)\)\{refreshContainerStats\(\)", TPL)
