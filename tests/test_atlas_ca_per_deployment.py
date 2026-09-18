"""One certificate authority per deployment (W216 chunk 10).

**Decided, operator 2026-09-18: no shared root.** Every deployment issues its own
device certificates from its own root, and Caddy verifies each agency's devices
against that agency's trust pool alone. That separation is the agency model.

⚠️ **The gap this closes.** Measured on the box the same day: corona's CA was
healthy and split — issuing certificate to 2031, two trust anchors — and the
console reported *"ATLAS is not running"* over it, because `_pki_dir` probed
`/root/atlas/pki` whatever it was asked about and every CA route `_compose_exec`d
into `takmdm-api-1`, which does not exist on a box whose only deployment is an
agency.

⚠️ **And the recovery file was named the same for all of them.** Three agencies
meant three `atlas-recovery-<fqdn>.key` files distinguished only by the browser's
`(1)` and `(2)`. A mislabelled recovery file is indistinguishable from the right
one until the day it is needed — five years out, during an outage, when
`ca-verify-root` rejects it and nothing says which of the three it should have
been.
"""

import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modules.atlas as atlas  # noqa: E402
from modules import atlas_instances as ai  # noqa: E402


# --------------------------------------------------------------------------- #
# Where a deployment's PKI is
# --------------------------------------------------------------------------- #


@pytest.fixture
def box(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas, 'install_base',
                        lambda c=None: str(tmp_path).replace(chr(92), '/'))
    return tmp_path


def test_an_agency_s_pki_is_found_under_its_own_directory(box):
    (box / 'atlas-corona' / 'pki').mkdir(parents=True)

    found = atlas._pki_dir(None, {'slug': 'corona'})

    assert found and found.endswith('atlas-corona/pki'), found


def test_an_agency_s_pki_is_not_the_plain_deployment_s(box):
    """⚠️ The bug. `_pki_dir` probed `/root/atlas/pki` whatever it was asked
    about, so on an agency-only box it answered None and the console reported
    *"ATLAS is not installed here"* over a healthy certificate authority."""
    (box / 'atlas' / 'pki').mkdir(parents=True)

    assert atlas._pki_dir(None, {'slug': 'corona'}) is None


def test_the_plain_deployment_finds_its_own(box):
    (box / 'atlas' / 'pki').mkdir(parents=True)

    found = atlas._pki_dir(None, None)

    assert found and found.endswith('atlas/pki'), found


def test_the_plain_deployment_still_finds_a_legacy_checkout(monkeypatch,
                                                            tmp_path):
    """⚠️ A checkout that predates `install_base` lives at `/root/atlas`, and
    dropping the probe would strand its certificate authority."""
    monkeypatch.setattr(atlas, 'install_base',
                        lambda c=None: str(tmp_path / 'elsewhere'))
    seen = []
    monkeypatch.setattr(atlas.os.path, 'isdir',
                        lambda p: seen.append(p) or p == '/root/atlas/pki')

    assert atlas._pki_dir(None, None) == '/root/atlas/pki'


def test_an_agency_does_not_fall_back_to_the_legacy_locations(monkeypatch,
                                                              tmp_path):
    """⚠️ `/root/atlas` is the *plain* deployment's. An agency finding a CA
    there would renew, export or delete somebody else's root key."""
    monkeypatch.setattr(atlas, 'install_base', lambda c=None: str(tmp_path))
    probed = []
    monkeypatch.setattr(atlas.os.path, 'isdir',
                        lambda p: probed.append(p) or False)

    atlas._pki_dir(None, {'slug': 'corona'})

    assert probed
    assert not any('/root/atlas/pki' == p for p in probed), probed


# --------------------------------------------------------------------------- #
# What the recovery file is called
# --------------------------------------------------------------------------- #


def test_an_agency_s_recovery_file_names_the_agency():
    """The operator's own format, 2026-09-18."""
    assert atlas.recovery_filename({'fqdn': 'leckliter.net'},
                                   {'slug': 'corona'}) == \
        'atlas-corona-leckliter.net.key'


def test_the_plain_deployment_s_names_the_box():
    assert atlas.recovery_filename({'fqdn': 'leckliter.net'}, None) == \
        'atlas-leckliter.net.key'


def test_no_two_deployments_produce_the_same_filename():
    """⚠️ **The whole point.** Identical names differ only by the browser's
    `(1)` and `(2)`, and the day one is needed is the day nobody can tell them
    apart."""
    settings = {'fqdn': 'leckliter.net'}
    names = [atlas.recovery_filename(settings, i) for i in
             (None, {'slug': 'corona'}, {'slug': 'redlands'})]

    assert len(set(names)) == 3, names


def test_a_box_with_no_domain_still_produces_a_usable_name():
    """An unnamed file is worse than an imprecise one — it would download as
    `.key` and land in a downloads folder indistinguishable from anything."""
    name = atlas.recovery_filename({}, {'slug': 'corona'})

    assert name == 'atlas-corona-server.key'


# --------------------------------------------------------------------------- #
# Which deployments still hold a root key
# --------------------------------------------------------------------------- #


def _holder_box(monkeypatch, tmp_path, deployments, with_key=(), built=None):
    monkeypatch.setattr(atlas, 'install_base',
                        lambda c=None: str(tmp_path).replace(chr(92), '/'))
    names = {ai.derive(i)['name']: i for i in deployments}
    projects = ({ai.derive(i)['compose_project'] for i in deployments}
                if built is None else set(built))
    monkeypatch.setattr(atlas, 'compose_projects_present',
                        lambda c=None: projects)
    for name in names:
        pki = tmp_path / name / 'pki'
        pki.mkdir(parents=True, exist_ok=True)
        (pki / 'ca.crt').write_text('cert', encoding='utf-8')
        if name in with_key:
            (pki / 'ca.key').write_text('key', encoding='utf-8')
    settings = {'atlas_enabled': True, ai.INSTANCES_KEY: list(deployments)}
    return {'load_settings': lambda: dict(settings)}


def test_a_deployment_that_still_has_its_root_key_is_named(monkeypatch,
                                                           tmp_path):
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)],
                      with_key=['atlas-corona'])

    assert atlas.root_key_holders(ctx)['holders'] == ['atlas-corona']


def test_a_deployment_that_has_finished_the_ceremony_is_not(monkeypatch,
                                                            tmp_path):
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)])

    assert atlas.root_key_holders(ctx)['holders'] == []


def test_only_the_deployments_that_hold_one_are_named(monkeypatch, tmp_path):
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make(None, ai.MODE_FIXED, 100, 8760),
                       ai.make('corona', ai.MODE_DYNAMIC, 50, 8761),
                       ai.make('redlands', ai.MODE_DYNAMIC, 50, 8762)],
                      with_key=['atlas-corona', 'atlas-redlands'])

    assert atlas.root_key_holders(ctx)['holders'] == ['atlas-corona',
                                                      'atlas-redlands']


def test_the_key_file_decides_not_whether_an_intermediate_exists(monkeypatch,
                                                                 tmp_path):
    """⚠️ `is_split` answers a different question, and those two come apart in
    exactly the state this is for: an issuing certificate exists **and** the
    root is still here (SEC_AUDIT S-2 / W185)."""
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)],
                      with_key=['atlas-corona'])
    (tmp_path / 'atlas-corona' / 'pki' / 'issuing.crt').write_text(
        'intermediate', encoding='utf-8')

    assert atlas.root_key_holders(ctx)['holders'] == ['atlas-corona']


def test_an_unfinished_deployment_is_not_asked(monkeypatch, tmp_path):
    """It has no certificate authority yet — the deploy makes one — so listing
    it would send an operator looking for a file that does not exist."""
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)],
                      with_key=['atlas-corona'], built=[])

    assert atlas.root_key_holders(ctx)['holders'] == []


def test_a_deployment_with_no_pki_directory_is_unknown_not_safe(monkeypatch,
                                                                tmp_path):
    """⚠️ **Not safe.** "We could not look" and "there is no key here" are
    different answers, and reporting the first as the second would tell an
    operator the one job they have is already done."""
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)])
    import shutil
    shutil.rmtree(tmp_path / 'atlas-corona' / 'pki')

    answer = atlas.root_key_holders(ctx)

    assert answer['holders'] == []
    assert answer['unknown'] == ['atlas-corona']


def test_the_holders_travel_with_the_deployments(monkeypatch, tmp_path):
    """The page polls one route; a second one for this would be a second poll
    and a second thing to keep in step."""
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)],
                      with_key=['atlas-corona'])
    ctx['probe_run'] = lambda argv, **k: type(
        'R', (), {'stdout': 'true', 'stderr': '', 'returncode': 0})()
    monkeypatch.setattr(atlas, 'capacity_facts', lambda c, size_gb=None: {})

    payload = atlas.instances_payload(ctx)

    assert payload['root_keys']['holders'] == ['atlas-corona']


def test_the_holder_check_reuses_the_caller_s_process_listing(monkeypatch,
                                                              tmp_path):
    """⚠️ Read on every poll of the deployments route. A second `docker ps` per
    poll would make the page slower the more agencies a box has."""
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)])
    calls = []
    monkeypatch.setattr(atlas, 'compose_projects_present',
                        lambda c=None: calls.append(1) or set())

    atlas.root_key_holders(ctx, projects={'takmdm-corona'})

    assert calls == []


# --------------------------------------------------------------------------- #
# Every CA route names its deployment
# --------------------------------------------------------------------------- #


MODULE = (ROOT / 'modules' / 'atlas.py').read_text(encoding='utf-8')
PAGE = (ROOT / 'templates' / 'atlas.html').read_text(encoding='utf-8')


def _view_source(name):
    start = MODULE.index('    def %s(' % name)
    end = MODULE.index(chr(10) + '    def ', start + 1)
    return MODULE[start:end]


CA_VIEWS = ('ca_view', 'ca_renew_view', 'ca_recovery_view',
            'ca_recovery_confirm_view')


@pytest.mark.parametrize('view', CA_VIEWS)
def test_every_ca_view_resolves_the_deployment_it_acts_on(view):
    source = _view_source(view)

    assert '_requested_instance(' in source, \
        '%s still acts on whichever deployment is plain' % view


@pytest.mark.parametrize('view', CA_VIEWS)
def test_every_container_call_in_a_ca_view_names_its_deployment(view):
    """⚠️ These run `ca-export-root`, `ca-delete-root` and
    `ca-issue-intermediate`. Aimed at the wrong container they would export one
    agency's root key to another's operator, or delete a root that was never
    saved."""
    source = _view_source(view)
    calls = re.findall(
        r'_compose(?:_exec|_exec_rc)?\((?:[^()]|\([^()]*\))*\)', source)

    assert calls, '%s no longer reaches a container' % view
    for call in calls:
        flat = ' '.join(call.split())
        assert 'inst=' in flat, '%s: %s' % (view, flat)


def test_the_renewed_trust_bundle_is_staged_for_its_own_deployment():
    """⚠️ Staging the plain bundle would leave the renewed agency verifying
    devices against the certificate it had just replaced — and Caddy would
    reject every tablet on that agency at the edge."""
    source = _view_source('ca_renew_view')
    calls = re.findall(r'sync_device_ca_for_caddy\([^)]*\)', source)

    assert calls == ['sync_device_ca_for_caddy(inst, ctx)'], calls


def test_the_pki_directory_is_asked_for_by_deployment():
    source = _view_source('ca_renew_view')

    assert '_pki_dir(ctx, inst)' in source


# --------------------------------------------------------------------------- #
# And so does every request the page makes
# --------------------------------------------------------------------------- #


def test_every_ca_request_the_page_makes_carries_a_slug():
    """⚠️ Without it the server falls back to the plain deployment, so a
    renewal aimed at an agency would issue a new intermediate for somebody
    else's fleet."""
    posts = re.findall(
        r"fetch\('/api/atlas/ca[^']*',\s*\{[^;]*?\}\);", PAGE, re.S)

    assert len(posts) == 3, len(posts)
    for call in posts:
        flat = ' '.join(call.split())
        assert 'slug: caSlug()' in flat, flat


def test_the_ca_status_request_asks_about_one_deployment():
    assert "'/api/atlas/ca' + q" in PAGE


def test_the_card_no_longer_claims_there_is_one_certificate_authority():
    assert 'Certificate authorities' in PAGE
    assert '<div class="card-title">Certificate authority</div>' not in PAGE


def test_the_recovery_download_names_the_file_for_its_deployment():
    """⚠️ The route is where the filename is chosen; the helper being correct
    does not help if the caller drops the argument. Both halves, because a
    mutation proved the route could lose it silently."""
    source = _view_source('ca_recovery_view')

    assert 'recovery_filename(ctx[' in source
    # ⚠️ Nested parentheses: `ctx['load_settings']()` closes before the call
    # does, so a lazy `[^)]*` matched half the expression and reported a missing
    # argument that was there.
    for call in re.findall(r'recovery_filename\((?:[^()]|\([^()]*\))*\)', source):
        assert 'inst' in call, call


def test_the_ca_status_says_which_deployment_it_describes():
    """⚠️ The page draws one block per deployment from these replies. Without a
    name they are three identical panels, and the operator cannot tell which
    certificate authority they are about to renew."""
    source = _view_source('ca_view')

    assert "body['name'] = names['name']" in source
    assert "body['slug'] = names['slug']" in source
