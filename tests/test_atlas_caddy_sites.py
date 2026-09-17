"""Caddy sites, one per ATLAS deployment (W216 chunk 6).

⚠️ **The safety property.** `generate_caddyfile` writes the Caddyfile for the
*whole box* — Authentik, TAK server, CloudTAK, NetBird, the console itself. A
file that differs by one character takes all of them down, not just ATLAS's
vhost. So the requirement is not "agencies work", it is:

**on a box with one deployment, the emitted configuration is unchanged.**

That reduces to `caddy_sites` returning exactly one entry carrying the host,
upstream and CA path the generator used before this existed — the values are
pinned below against what was read off the live box.
"""

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modules.atlas as atlas  # noqa: E402
from modules import atlas_instances as ai  # noqa: E402


# --------------------------------------------------------------------------- #
# The hostname an agency gets
# --------------------------------------------------------------------------- #


def test_the_plain_deployment_keeps_the_host_it_has():
    assert ai.agency_host('atlas.leckliter.net', None) == 'atlas.leckliter.net'


def test_an_agency_qualifies_the_first_label():
    """⚠️ Inserted after the service label, not prepended to the whole name —
    `atlas.agency-a.<fqdn>`, not `agency-a.atlas.<fqdn>`. The first label says
    what the service is; the agency qualifies it."""
    assert ai.agency_host('atlas.leckliter.net', 'agency-a') == \
        'atlas.agency-a.leckliter.net'


def test_a_custom_console_domain_is_qualified_the_same_way():
    """A box whose operator renamed the console still gets a sensible agency
    name rather than one built from a constant."""
    assert ai.agency_host('mdm.example.com', 'pd') == 'mdm.pd.example.com'


def test_a_single_label_host_still_works():
    assert ai.agency_host('atlas', 'pd') == 'atlas.pd'


def test_no_host_yields_no_host():
    """Before an FQDN is set there is nothing to qualify, and inventing one
    would put a certificate request in for a name nobody owns."""
    assert ai.agency_host('', 'pd') == ''
    assert ai.agency_host(None, 'pd') is None


# --------------------------------------------------------------------------- #
# The sites themselves
# --------------------------------------------------------------------------- #


@pytest.fixture
def one_deployment(monkeypatch, tmp_path):
    """A box with the single plain deployment, as every box has today."""
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'store.img'))
    monkeypatch.setattr(atlas, 'sync_device_ca_for_caddy',
                        lambda inst=None: '/var/lib/caddy/%s/device-ca.crt'
                        % ai.derive(inst)['name'])
    return {'atlas_enabled': True}


def test_one_deployment_produces_exactly_one_site(one_deployment):
    sites = atlas.caddy_sites(one_deployment, 'atlas.leckliter.net')

    assert len(sites) == 1


def test_that_one_site_is_what_the_generator_used_before(one_deployment):
    """⚠️ **The byte-identical guarantee, in one assertion.** These three values
    are what the hardcoded block carried: the console host, `127.0.0.1:8760`, and
    `/var/lib/caddy/atlas/device-ca.crt`. If any of them moves, the Caddyfile
    changes on a box that has not asked for anything."""
    site = atlas.caddy_sites(one_deployment, 'atlas.leckliter.net')[0]

    assert site['host'] == 'atlas.leckliter.net'
    assert site['upstream'] == '127.0.0.1:8760'
    assert site['ca_path'] == '/var/lib/caddy/atlas/device-ca.crt'
    assert site['slug'] is None


def test_nothing_installed_produces_no_sites(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'store.img'))

    assert atlas.caddy_sites({}, 'atlas.leckliter.net') == []


@pytest.fixture
def two_deployments(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'store.img'))
    monkeypatch.setattr(atlas, 'sync_device_ca_for_caddy',
                        lambda inst=None: '/var/lib/caddy/%s/device-ca.crt'
                        % ai.derive(inst)['name'])
    return {
        'atlas_enabled': True,
        ai.INSTANCES_KEY: [
            ai.make(None, ai.MODE_FIXED, 100, 8760),
            ai.make('agency-a', ai.MODE_DYNAMIC, 50, 8761),
        ],
    }


def test_each_deployment_gets_its_own_site(two_deployments):
    sites = atlas.caddy_sites(two_deployments, 'atlas.leckliter.net')

    assert [s['host'] for s in sites] == [
        'atlas.leckliter.net', 'atlas.agency-a.leckliter.net']


def test_each_deployment_has_its_own_upstream_port(two_deployments):
    """Two agencies on one upstream would send every request to whichever
    container happened to bind first."""
    sites = atlas.caddy_sites(two_deployments, 'atlas.leckliter.net')

    assert [s['upstream'] for s in sites] == ['127.0.0.1:8760', '127.0.0.1:8761']


def test_each_deployment_has_its_own_device_ca(two_deployments):
    """⚠️ **The one that matters for tenancy.** `client_auth` verifies against
    exactly one trust pool. Pointing two agencies at one file would have Caddy
    accept either agency's devices at either agency's hostname — the
    cryptographic separation the whole design rests on, undone by a shared
    path."""
    sites = atlas.caddy_sites(two_deployments, 'atlas.leckliter.net')

    paths = [s['ca_path'] for s in sites]

    assert paths == ['/var/lib/caddy/atlas/device-ca.crt',
                     '/var/lib/caddy/atlas-agency-a/device-ca.crt']
    assert len(set(paths)) == 2


def test_no_two_sites_share_anything(two_deployments):
    a, b = atlas.caddy_sites(two_deployments, 'atlas.leckliter.net')

    for field in ('host', 'upstream', 'ca_path', 'slug'):
        assert a[field] != b[field], f'{field} is shared between deployments'


def test_a_deployment_without_a_ca_reports_none(monkeypatch, tmp_path):
    """Caddy cannot verify devices without one, and the generator skips the
    device site rather than emitting a `pem_file` that does not exist — which
    stops Caddy from starting at all."""
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'store.img'))
    monkeypatch.setattr(atlas, 'sync_device_ca_for_caddy', lambda inst=None: None)

    site = atlas.caddy_sites({'atlas_enabled': True}, 'atlas.leckliter.net')[0]

    assert site['ca_path'] is None


# --------------------------------------------------------------------------- #
# Where the CA copy lands
# --------------------------------------------------------------------------- #


def test_the_ca_copy_is_per_deployment(monkeypatch, tmp_path):
    """⚠️ A shared `atlas/device-ca.crt` would have the last deployment to run
    overwrite every other agency's trust pool."""
    src = tmp_path / 'atlas-agency-a' / 'pki'
    src.mkdir(parents=True)
    (src / 'ca.crt').write_text('-----BEGIN CERTIFICATE-----\nx\n'
                                '-----END CERTIFICATE-----', encoding='utf-8')
    monkeypatch.setattr(atlas, 'install_base',
                        lambda ctx=None: str(tmp_path).replace(chr(92), '/'))
    landed = {}

    class FakePwd:
        @staticmethod
        def getpwnam(_name):
            raise KeyError('no caddy user here')

    monkeypatch.setitem(sys.modules, 'pwd', FakePwd)
    monkeypatch.setattr(atlas.os, 'makedirs',
                        lambda path, **k: landed.setdefault('dir', path))

    try:
        atlas.sync_device_ca_for_caddy(ai.make('agency-a', ai.MODE_FIXED, 50, 8761))
    except Exception:
        pass  # the copy itself needs a real /var/lib/caddy; the path is the point

    assert landed.get('dir', '').endswith('atlas-agency-a')
