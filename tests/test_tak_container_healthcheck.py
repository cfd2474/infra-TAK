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
"""Container TAK 5.8: the hardened images' own HEALTHCHECKs can pass.

ARM64 container install, every day since its 5.8 upgrade:

    takserver      Up 2 hours (unhealthy)   TAK Server listener HTTPS state: 000 (waiting for 200)
    takserver-db   Up 2 hours (unhealthy)   ERROR: /usr/share/crypto-policies/back-ends/LEGACY/openssl.config is not readable.

ADMIN_CERT_NAME / ADMIN_CERT_PASS are never set, the image's OpenSSL 3 cannot read
makeCert's legacy RC2 .p12 anyway, and both integrity lists name files the UBI 10 base
does not ship. _tak_repair_container_healthchecks() fixes the health files on the
shared /opt/tak mount; these tests run its real bash snippets against a fake container.
"""

import ast
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (REPO / 'app.py').read_text(encoding='utf-8')
TREE = ast.parse(APP)

STOCK_HEALTH = '''#!/usr/bin/env bash

LOG_FILE=/opt/tak/health/takserver/health_check.log

takserver_https_response=$(curl https://localhost:8443/Marti/api/missions --cert-type P12 --cert /opt/tak/certs/files/"$ADMIN_CERT_NAME".p12:"$ADMIN_CERT_PASS" -Is -o /dev/null -w "%{http_code}" --head -k)

if [[ $takserver_https_response != 200 ]]; then
    exit 1
fi

/opt/tak/health/takserver/check_file_integrity.sh >> $LOG_FILE
exit $?

'''

SHIPPED = ['/opt/tak/certs/config.cfg',
           '/opt/tak/configure.sh',
           '/usr/share/crypto-policies/back-ends/LEGACY/java.config']
NOT_SHIPPED = ['/usr/share/crypto-policies/back-ends/LEGACY/openssl.config',
               '/usr/share/crypto-policies/back-ends/FIPS/openssl.config']
STOCK_LIST = ''.join(p + '\n' for p in
                     [SHIPPED[0], SHIPPED[1], NOT_SHIPPED[0], SHIPPED[2], NOT_SHIPPED[1]])


def _segment(pred):
    for n in TREE.body:
        if pred(n):
            return ast.get_source_segment(APP, n)
    raise AssertionError('renamed or removed')


def _func(name):
    return _segment(lambda n: isinstance(n, ast.FunctionDef) and n.name == name)


def _const(name):
    return _segment(lambda n: isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name)


class FakeDocker:
    """Each container is a directory standing in for its root filesystem; both share
    one /opt/tak (the bind mount). `docker exec` runs the real argv with absolute
    paths mapped into the container's directory."""

    def __init__(self, tmp, running=('takserver', 'takserver-db')):
        self.tak = tmp / 'opt-tak'
        self.roots = {c: tmp / c for c in ('takserver', 'takserver-db')}
        self.running = set(running)
        self.writes = []
        for root in self.roots.values():
            for p in SHIPPED:
                if p.startswith('/usr/'):
                    f = root / p.lstrip('/')
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text('x')
        for p in SHIPPED:
            if p.startswith('/opt/tak/'):
                f = self.tak / p[len('/opt/tak/'):]
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text('x')

    def put(self, path, body):
        f = self.tak / path[len('/opt/tak/'):]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)

    def get(self, path):
        return (self.tak / path[len('/opt/tak/'):]).read_text()

    def _to_host(self, name, a):
        if a.startswith('/opt/tak/'):
            return str(self.tak / a[len('/opt/tak/'):])
        if a.startswith('/usr/'):
            return str(self.roots[name] / a.lstrip('/'))
        return a

    def _from_host(self, name, text):
        return (text.replace(str(self.tak) + '/', '/opt/tak/')
                    .replace(str(self.roots[name]) + '/', '/'))

    def run(self, argv, **kw):
        if argv[:2] == ['docker', 'inspect']:
            out = 'true' if argv[-1] in self.running else 'false'
            return subprocess.CompletedProcess(argv, 0, out + '\n', '')
        assert argv[:2] == ['docker', 'exec'], argv
        name, rest = argv[2], argv[3:]
        if 'printf' in ' '.join(rest):
            self.writes.append(rest[-1])
        # Map only path ARGUMENTS, never a file body passed through to printf.
        mapped = [self._to_host(name, a) if a.startswith('/') and '\n' not in a else a for a in rest]
        r = subprocess.run(mapped, capture_output=True, text=True)
        return subprocess.CompletedProcess(argv, r.returncode,
                                           self._from_host(name, r.stdout), r.stderr)


def _load(fake):
    ns = {'os': os, '_sudo_wrap': lambda cmd: list(cmd),
          'subprocess': type('sp', (), {'run': staticmethod(fake.run)})}
    for name in ('TAK_CONTAINER', 'TAK_DB_CONTAINER', '_TAK_HEALTH_DIRS', '_TAK_HEALTH_P12_ARGS',
                 '_TAK_HEALTH_PEM_ARGS', '_TAK_HEALTH_CERT_DEFAULTS'):
        exec(_const(name), ns)
    exec(_func('_tak_container_running'), ns)
    exec(_func('_tak_repair_container_healthchecks'), ns)
    return ns['_tak_repair_container_healthchecks']


@pytest.fixture
def stock(tmp_path):
    fake = FakeDocker(tmp_path)
    fake.put('/opt/tak/health/takserver/health_check.sh', STOCK_HEALTH)
    fake.put('/opt/tak/health/takserver/critical_file_list.txt', STOCK_LIST)
    fake.put('/opt/tak/health/takserver-db/critical_file_list.txt', STOCK_LIST)
    return fake


def test_health_check_uses_admin_pem_with_defaults(stock):
    logs = []
    assert _load(stock)(logs.append) is True
    body = stock.get('/opt/tak/health/takserver/health_check.sh')
    assert '.p12' not in body
    assert ('--cert /opt/tak/certs/files/"$ADMIN_CERT_NAME".pem '
            '--key /opt/tak/certs/files/"$ADMIN_CERT_NAME".key --pass "$ADMIN_CERT_PASS"') in body
    # The defaults land BEFORE the curl that reads them.
    assert body.index('ADMIN_CERT_NAME=${ADMIN_CERT_NAME:-admin}') < body.index('takserver_https_response=')
    assert '. /opt/tak/certs/cert-metadata.sh' in body
    assert body.endswith('exit $?\n\n')  # the rest of the stock script is untouched
    assert subprocess.run(['bash', '-n'], input=body, text=True).returncode == 0


def test_cert_password_comes_from_cert_metadata(stock, tmp_path):
    _load(stock)(None)
    body = stock.get('/opt/tak/health/takserver/health_check.sh')
    defaults = body[body.index('ADMIN_CERT_NAME='):body.index('takserver_https_response=')]
    meta = tmp_path / 'cert-metadata.sh'
    meta.write_text('CAPASS=${CAPASS:-atakatak}\nPASS="s3cret"\nDIR=files\n')
    probe = defaults.replace('/opt/tak/certs/cert-metadata.sh', str(meta)) + \
        'echo "$ADMIN_CERT_NAME:$ADMIN_CERT_PASS"\n'
    env = {k: v for k, v in os.environ.items() if not k.startswith('ADMIN_CERT_')}
    out = subprocess.run(['bash', '-c', probe], capture_output=True, text=True, env=env).stdout
    assert out.strip() == 'admin:s3cret'
    # An empty ADMIN_CERT_NAME (what the container really has) still means admin.
    env['ADMIN_CERT_NAME'] = ''
    out = subprocess.run(['bash', '-c', probe], capture_output=True, text=True, env=env).stdout
    assert out.strip() == 'admin:s3cret'


def test_integrity_list_skips_only_files_the_image_lacks(stock):
    _load(stock)(None)
    for c in ('takserver', 'takserver-db'):
        lines = stock.get(f'/opt/tak/health/{c}/critical_file_list.txt').splitlines()
        assert len(lines) == len(SHIPPED) + len(NOT_SHIPPED)
        live = [l for l in lines if not l.replace(' ', '').startswith('#')]
        assert live == SHIPPED  # every shipped file is still checked, in order
        for p in NOT_SHIPPED:
            assert f'# not in this image, skipped by infra-TAK: {p}' in lines


def test_second_run_is_a_no_op(stock):
    repair = _load(stock)
    assert repair(None) is True
    before = {p: stock.get(p) for p in ('/opt/tak/health/takserver/health_check.sh',
                                        '/opt/tak/health/takserver/critical_file_list.txt',
                                        '/opt/tak/health/takserver-db/critical_file_list.txt')}
    stock.writes.clear()
    assert repair(None) is False
    assert stock.writes == []
    assert before == {p: stock.get(p) for p in before}


def test_stopped_container_is_skipped(tmp_path):
    fake = FakeDocker(tmp_path, running=('takserver',))
    fake.put('/opt/tak/health/takserver-db/critical_file_list.txt', STOCK_LIST)
    logs = []
    _load(fake)(logs.append)
    assert fake.get('/opt/tak/health/takserver-db/critical_file_list.txt') == STOCK_LIST
    assert any('takserver-db not running' in l for l in logs)


def test_pre_hardened_bundle_without_health_dir_is_untouched(tmp_path):
    fake = FakeDocker(tmp_path)
    assert _load(fake)(None) is False
    assert fake.writes == []


def test_runs_after_deploy_upgrade_and_console_update():
    assert '_tak_repair_container_healthchecks(log_step)' in _func('_deploy_takserver_container')
    assert '_tak_repair_container_healthchecks(ulog)' in _func('run_takserver_upgrade_container')
    assert APP.count('_tak_repair_container_healthchecks(') >= 4  # def + 3 call sites
