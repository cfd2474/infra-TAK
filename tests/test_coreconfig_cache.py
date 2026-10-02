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
"""_read_coreconfig() cache (v10.2.2 W3).

The function is cut out of app.py and run on real files. A mode-000 file is the
non-root box in miniature: stat works (the directory is traversable), open()
raises PermissionError, and the read goes to the broker — stubbed here so every
broker read is counted.
"""

import os
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')


def _src():
    fn = re.search(r'^def _read_coreconfig\(.*?(?=^def )', APP, re.S | re.M).group(0)
    assert '_CORECONFIG_CACHE = {}' in fn, 'cache dict moved away from the function'
    return fn


class Box:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.broker_reads = 0
        self.broker_fails = False
        self.content = {}
        self.ns = {'os': os, 'CORECONFIG_PATH': str(tmp_path / 'CoreConfig.xml'),
                   '_read_priv': self._read_priv}
        exec(compile(_src(), 'app.py:_read_coreconfig', 'exec'), self.ns)

    def _read_priv(self, path):
        # The broker reads as root without touching metadata. A chmod here would
        # bump ctime and defeat the very key under test, so serve a shadow copy.
        self.broker_reads += 1
        if self.broker_fails:
            raise RuntimeError('broker unreachable')
        if path not in self.content:
            raise FileNotFoundError(path)
        return self.content[path]

    def write(self, text, name='CoreConfig.xml', mtime_ns=None, replace=False):
        p = self.dir / name
        if p.exists():
            os.chmod(p, 0o600)
        if replace:
            tmp = self.dir / (name + '.tmp')
            tmp.write_text(text, encoding='utf-8')
            os.replace(tmp, p)
        else:
            p.write_text(text, encoding='utf-8')
        if mtime_ns is not None:
            os.utime(p, ns=(mtime_ns, mtime_ns))
        os.chmod(p, 0o000)                 # 640 tak:tak, as seen by takwerx
        self.content[str(p)] = text
        return str(p)

    def read(self, *a):
        return self.ns['_read_coreconfig'](*a)


@pytest.fixture
def box(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root reads mode-000 files; the non-root path cannot be simulated')
    b = Box(tmp_path)
    yield b
    for p in tmp_path.iterdir():
        os.chmod(p, 0o600)


def test_unchanged_file_is_read_through_the_broker_once(box):
    box.write('<Configuration ldap="yes"/>')
    assert box.read() == '<Configuration ldap="yes"/>'
    assert box.read() == '<Configuration ldap="yes"/>'
    assert box.read() == '<Configuration ldap="yes"/>'
    assert box.broker_reads == 1


def test_a_size_change_is_seen(box):
    box.write('<a/>')
    box.read()
    box.write('<abc/>')
    assert box.read() == '<abc/>'
    assert box.broker_reads == 2


def test_a_same_size_edit_with_a_new_mtime_is_seen(box):
    box.write('<a x="1"/>', mtime_ns=1_700_000_000_000_000_000)
    box.read()
    box.write('<a x="2"/>', mtime_ns=1_700_000_000_000_000_001)
    assert box.read() == '<a x="2"/>'


def test_a_same_size_same_mtime_replace_is_seen_by_inode(box):
    box.write('<a x="1"/>', mtime_ns=1_700_000_000_000_000_000)
    box.read()
    box.write('<a x="2"/>', mtime_ns=1_700_000_000_000_000_000, replace=True)
    assert box.read() == '<a x="2"/>'


def test_explicit_path_and_default_path_are_cached_separately(box):
    other = box.write('<other/>', name='CoreConfig.example.xml')
    box.write('<main/>')
    assert box.read() == '<main/>'
    assert box.read(other) == '<other/>'
    assert box.read() == '<main/>'
    assert box.broker_reads == 2


def test_missing_file_raises_like_before_and_caches_nothing(box):
    with pytest.raises(FileNotFoundError):
        box.read()
    assert box.ns['_CORECONFIG_CACHE'] == {}


def test_unreadable_still_raises_the_original_error_never_no_ldap(box):
    box.write('<a/>')
    box.broker_fails = True
    with pytest.raises(PermissionError):
        box.read()
    assert box.ns['_CORECONFIG_CACHE'] == {}
    box.broker_fails = False
    assert box.read() == '<a/>'


def test_cache_is_bounded(box):
    for i in range(20):
        box.read(box.write(f'<c{i}/>', name=f'c{i}.xml'))
    assert len(box.ns['_CORECONFIG_CACHE']) <= 8
