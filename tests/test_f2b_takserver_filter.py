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
"""The takserver fail2ban jail does not ban phones for dropped connections (v10.2.2).

Field report 2026-10-01 (pwtak): the jail counted every NioNettyServerHandler
error, so `Connection reset by peer` from two phones behind one carrier NAT
address added up to a ban. Verified with fail2ban-regex on test6 (296 matched →
53 ignored + 243 matched) and test8 (151 → 13 + 138). The lines below are real,
with the address replaced.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')

PFX = ('2026-10-01-01:05:46.864 [epollEventLoopGroup-4-3] ERROR c.b.m.n.n.h.NioNettyHandlerBase - '
       'NioNettyServerHandler error. Cause: ')
SFX = '. Additional info: Remote address: 203.0.113.9; Remote port: 29186; Local port: 8089; Certificate error: peer not verified; '
RESETS = [PFX + 'recvAddress(..) failed with error(-104): Connection reset by peer' + SFX,
          PFX + 'recvAddress(..) failed with error(-103): Software caused connection abort' + SFX,
          PFX + 'recvAddress(..) failed with error(-110): Connection timed out' + SFX]
ATTACKS = [PFX + 'io.netty.handler.ssl.NotSslRecordException: not an SSL/TLS record' + SFX,
           PFX + 'io.netty.handler.ssl.ReferenceCountedOpenSslEngine$OpenSslHandshakeException: '
                 'error:100000c0:SSL routines:OPENSSL_internal:PEER_DID_NOT_RETURN_A_CERTIFICATE' + SFX,
           PFX + 'io.netty.handler.ssl.ReferenceCountedOpenSslEngine$OpenSslHandshakeException: '
                 'error:100000b8:SSL routines:OPENSSL_internal:NO_SHARED_CIPHER' + SFX,
           PFX + 'some.future.Exception: not seen yet' + SFX]


def _table(name):
    i = APP.index(name + ' = {')
    depth, j = 0, APP.index('{', i)
    for k in range(j, len(APP)):
        depth += {'{': 1, '}': -1}.get(APP[k], 0)
        if depth == 0:
            return eval(APP[j:k + 1])
    raise AssertionError(name)


def _regex(conf, key):
    v = re.search(rf'^{key} = (.*)$', conf, re.M).group(1)
    return re.compile(v.replace('<HOST>', r'(?P<host>\S+)'))


def _banned(conf, line):
    return bool(_regex(conf, 'failregex').search(line)) and not (
        re.search(r'^ignoreregex = \S', conf, re.M) and _regex(conf, 'ignoreregex').search(line))


def test_connection_resets_never_count():
    conf = _table('_F2B_OWNED_FILTERS')['takserver']
    for line in RESETS:
        assert not _banned(conf, line), line


def test_handshake_scanner_and_unknown_errors_still_count():
    conf = _table('_F2B_OWNED_FILTERS')['takserver']
    for line in ATTACKS:
        assert _banned(conf, line), line


def test_the_old_shipped_filter_is_upgraded_by_the_startup_self_heal():
    legacy = _table('_F2B_LEGACY_FILTERS')['takserver']
    new = _table('_F2B_OWNED_FILTERS')['takserver']
    assert new not in legacy
    old = legacy[0]
    assert all(_banned(old, l) for l in RESETS)          # the bug the legacy entry records
    assert 'ignoreregex =\n' in old


def test_every_legacy_upgrade_has_an_accurate_reason_for_the_log():
    why = _table('_F2B_UPGRADE_WHY')
    assert set(_table('_F2B_LEGACY_FILTERS')) <= set(why)
    assert 'banning nobody' not in why['takserver']
    assert 'resets' in why['takserver']
