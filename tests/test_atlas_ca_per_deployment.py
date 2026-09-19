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
import io
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modules.atlas as atlas  # noqa: E402
from modules import atlas_instances as ai
from atlas_layout import deployment_dir  # noqa: E402


# --------------------------------------------------------------------------- #
# Where a deployment's PKI is
# --------------------------------------------------------------------------- #


@pytest.fixture
def box(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas, 'install_base',
                        lambda c=None: str(tmp_path).replace(chr(92), '/'))
    return tmp_path


def test_an_agency_s_pki_is_found_under_its_own_directory(box):
    (box / 'atlas' / 'corona' / 'pki').mkdir(parents=True)

    found = atlas._pki_dir(None, {'slug': 'corona'})

    assert found and found.endswith('atlas/corona/pki'), found


def test_an_agency_s_pki_is_not_the_plain_deployment_s(box):
    """⚠️ The bug. `_pki_dir` probed `/root/atlas/pki` whatever it was asked
    about, so on an agency-only box it answered None and the console reported
    *"ATLAS is not installed here"* over a healthy certificate authority."""
    (box / 'atlas' / 'default' / 'pki').mkdir(parents=True)

    assert atlas._pki_dir(None, {'slug': 'corona'}) is None


def test_the_plain_deployment_finds_its_own(box):
    (box / 'atlas' / 'default' / 'pki').mkdir(parents=True)

    found = atlas._pki_dir(None, None)

    assert found and found.endswith('atlas/default/pki'), found


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


def _holder_box(monkeypatch, tmp_path, deployments, with_key=(), built=None,
                silent=()):
    """A box where each deployment answers `ca-status` for itself.

    ⚠️ **It stubs the container, not the filesystem, and that change is
    the whole of W235.** This used to create `pki/ca.key` on disk and let
    `root_key_holders` stat it. That made every test here pass while the
    function was wrong on every real box: `pki/` is `drwx------` owned by
    the container's uid, the console cannot traverse it, and the stat came
    back False with three root keys sitting in it. A double that can answer
    a question the real console cannot ask is not a double, it is a
    different program.

    `silent` names deployments whose container does not answer — "we could
    not look", which must never be reported as "there is no key here".
    """
    monkeypatch.setattr(atlas, 'install_base',
                        lambda c=None: str(tmp_path).replace(chr(92), '/'))
    names = {ai.derive(i)['name']: i for i in deployments}
    projects = ({ai.derive(i)['compose_project'] for i in deployments}
                if built is None else set(built))
    monkeypatch.setattr(atlas, 'compose_projects_present',
                        lambda c=None: projects)
    # The checkout, because `instance_is_built` wants a directory as well as
    # a container. Nothing is put *inside* `pki/` -- that is the point.
    for name in names:
        (deployment_dir(name) / 'pki').mkdir(parents=True, exist_ok=True)

    def status(c, inst=None, use_cache=True):
        name = ai.derive(inst)['name']
        if name in silent:
            return None
        return {'root_certificate': True,
                'root_key_on_server': name in with_key,
                'is_split': False}
    monkeypatch.setattr(atlas, '_ca_status', status)
    atlas._forget_ca_status()

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
    root is still here (SEC_AUDIT S-2 / W185). Measured on the box
    2026-09-19: `is_split: True` *and* `root_key_on_server: True`, together,
    on all three deployments."""
    monkeypatch.setattr(atlas, 'install_base',
                        lambda c=None: str(tmp_path).replace(chr(92), '/'))
    monkeypatch.setattr(atlas, 'compose_projects_present',
                        lambda c=None: {'takmdm-corona'})
    deployment_dir('atlas-corona').mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(atlas, '_ca_status', lambda c, inst=None, use_cache=True: {
        'root_certificate': True, 'root_key_on_server': True, 'is_split': True})
    ctx = {'load_settings': lambda: {
        'atlas_enabled': True,
        ai.INSTANCES_KEY: [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)]}}

    assert atlas.root_key_holders(ctx)['holders'] == ['atlas-corona']


def test_an_unfinished_deployment_is_not_asked(monkeypatch, tmp_path):
    """It has no certificate authority yet — the deploy makes one — so listing
    it would send an operator looking for a file that does not exist."""
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)],
                      with_key=['atlas-corona'], built=[])

    assert atlas.root_key_holders(ctx)['holders'] == []


def test_a_deployment_that_cannot_be_asked_is_unknown_not_safe(monkeypatch,
                                                               tmp_path):
    """⚠️ **Not safe.** "We could not look" and "there is no key here" are
    different answers, and reporting the first as the second would tell an
    operator the one job they have is already done."""
    ctx = _holder_box(monkeypatch, tmp_path,
                      [ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)],
                      silent=['atlas-corona'])

    answer = atlas.root_key_holders(ctx)

    assert answer['holders'] == []
    assert answer['unknown'] == ['atlas-corona']


def test_a_key_the_console_cannot_stat_is_still_reported(monkeypatch, tmp_path):
    """⚠️ **The inversion, reproduced.** The filesystem says there is no
    root key -- because the console cannot read the directory -- and the
    container says there is. The container wins, every time. On the box this
    was three root keys reported as none.
    """
    inst = ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)
    monkeypatch.setattr(atlas, 'install_base',
                        lambda c=None: str(tmp_path).replace(chr(92), '/'))
    monkeypatch.setattr(atlas, 'compose_projects_present',
                        lambda c=None: {'takmdm-corona'})
    deployment_dir('atlas-corona').mkdir(parents=True, exist_ok=True)
    # Nothing on disk at all: exactly what the console sees through a
    # directory it cannot traverse.
    monkeypatch.setattr(atlas, '_compose_exec',
                        lambda c, argv, timeout=60, stdin=None, inst=None:
                        '{"root_key_on_server": true, "is_split": true}')
    atlas._forget_ca_status()
    ctx = {'load_settings': lambda: {'atlas_enabled': True,
                                     ai.INSTANCES_KEY: [inst]}}

    assert atlas.root_key_holders(ctx)['holders'] == ['atlas-corona']


def test_the_answer_is_cached_but_a_ceremony_clears_it(monkeypatch, tmp_path):
    """⚠️ A `docker compose exec` per deployment on every poll is far
    dearer than the stat it replaced, so it is cached -- but a cached "the
    root key is still here" surviving a delete would keep warning about a
    key that has gone, and a cached "it is gone" surviving a renewal is
    worse."""
    inst = ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)
    calls = []
    monkeypatch.setattr(atlas, '_compose_exec',
                        lambda c, argv, timeout=60, stdin=None, inst=None:
                        calls.append(1) or '{"root_key_on_server": true}')
    atlas._forget_ca_status()

    atlas._ca_status({}, inst=inst)
    atlas._ca_status({}, inst=inst)

    assert len(calls) == 1, 'the second read should have been cached'

    atlas._forget_ca_status(inst)
    atlas._ca_status({}, inst=inst)

    assert len(calls) == 2, 'clearing the entry must force a fresh read'


def test_an_uncached_read_always_asks(monkeypatch):
    """The panel an operator is looking at, right after they changed
    something, must not be answered from a cache."""
    inst = ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)
    calls = []
    monkeypatch.setattr(atlas, '_compose_exec',
                        lambda c, argv, timeout=60, stdin=None, inst=None:
                        calls.append(1) or '{"root_key_on_server": false}')
    atlas._forget_ca_status()

    atlas._ca_status({}, inst=inst)
    atlas._ca_status({}, inst=inst, use_cache=False)

    assert len(calls) == 2


def test_a_container_that_cannot_be_asked_is_not_cached(monkeypatch):
    """⚠️ Caching None would turn a momentary restart into half a minute
    of "unknown" -- and, worse, would keep answering None after the
    deployment came back."""
    inst = ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)
    answers = [None, '{"root_key_on_server": true}']
    monkeypatch.setattr(atlas, '_compose_exec',
                        lambda c, argv, timeout=60, stdin=None, inst=None:
                        answers.pop(0))
    atlas._forget_ca_status()

    assert atlas._ca_status({}, inst=inst) is None
    assert atlas._ca_status({}, inst=inst) == {'root_key_on_server': True}


def test_unparseable_output_is_unknown_not_safe(monkeypatch):
    """⚠️ A container that answers something that is not JSON has not
    said "no key"."""
    inst = ai.make('corona', ai.MODE_DYNAMIC, 50, 8761)
    monkeypatch.setattr(atlas, '_compose_exec',
                        lambda c, argv, timeout=60, stdin=None, inst=None: 'boom')
    atlas._forget_ca_status()

    assert atlas._ca_status({}, inst=inst) is None


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


def _view_code(name):
    """A view's source with comments and docstrings removed.

    ⚠️ **Match executable code, never prose.** The guard below forbids
    `os.path.exists` in the renewal, and the comment explaining *why* it is
    forbidden contains that very phrase -- so the first version of the guard
    failed on the fixed code. It is the mirror of the mistake
    `test_atlas_non_root` already warns about: match comments and a guard
    becomes unfailable by editing a sentence; match them here and it becomes
    unpassable by explaining yourself.
    """
    source = _view_source(name)
    source = re.sub(TRIPLE + r'[\s\S]*?' + TRIPLE, '', source)
    return re.sub(r'#[^\n]*', '', source)


def _view_source(name):
    start = MODULE.index('    def %s(' % name)
    end = MODULE.index(chr(10) + '    def ', start + 1)
    return MODULE[start:end]


TRIPLE = chr(34) * 3

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
    # ⚠️ `_ca_status` is a container call too (W235). It wraps
    # `_compose_exec` so the pattern above stopped matching `ca_view`
    # entirely, and a guard that matches nothing passes forever -- which is
    # how this one nearly stopped watching the view it was written for.
    calls = re.findall(
        r'(?<![A-Za-z0-9_])(?:_compose(?:_exec|_exec_rc)?|_ca_status)\((?:[^()]|\([^()]*\))*\)',
        source)

    assert calls, '%s no longer reaches a container' % view
    for call in calls:
        flat = ' '.join(call.split())
        assert 'inst=' in flat, '%s: %s' % (view, flat)


def test_the_renewal_asks_the_container_whether_a_root_key_is_there():
    """⚠️ **A source guard, because a view needs Flask and the suite has
    none.** Weaker than exercising it, and it is what killed the mutant that
    put `os.path.exists` back: on the box that probe returns False through a
    directory the console cannot traverse, and the renewal then refuses with
    "no root key on the server and none supplied" while the key is sitting
    there. Reported by the operator; unreachable by any test that stubs the
    filesystem.
    """
    source = _view_code('ca_renew_view')

    assert '_ca_status(' in source
    assert 'os.path.exists' not in source, (
        'the console cannot stat inside pki/ -- the answer is always False')


def test_the_renewal_says_not_running_rather_than_no_key():
    """⚠️ Two different answers, and conflating them sends the operator
    hunting for a key that is present on a deployment that is merely
    stopped."""
    source = _view_source('ca_renew_view')
    head = source[:source.index('on_server =')]

    assert 'ATLAS is not running' in head, (
        'an unreachable container must not be read as "no root key"')


def test_the_ceremony_clears_the_cached_status_before_restaging():
    """⚠️ The CA has just changed. A cached answer surviving it means the
    banner reports the pre-ceremony state for up to the TTL -- including
    "the root key is still here" after it has gone."""
    source = _view_source('ca_renew_view')

    assert '_forget_ca_status(inst)' in source
    assert source.index('_forget_ca_status(inst)') <         source.index('sync_device_ca_for_caddy(')


def test_the_ca_panel_is_never_answered_from_the_cache():
    """It is opened to see the effect of something just done."""
    source = _view_source('ca_view')

    assert 'use_cache=False' in source


# --------------------------------------------------------------------------- #
# Staging a supplied root key (W235)
# --------------------------------------------------------------------------- #


def test_a_supplied_root_key_is_written_through_the_broker(tmp_path,
                                                           monkeypatch):
    """⚠️ `pki/` is not the console's to write. The plain `os.open` this
    used raised PermissionError, so supplying the key by hand could not work
    on a non-root box either -- both routes through the renewal were dead."""
    wrote = {}
    monkeypatch.setattr(atlas, '_chown_priv', lambda p, u, g: None)
    ctx = {'_write_priv': lambda path, body, **k: wrote.update(
        {'path': path, 'body': body, 'mode': k.get('mode')})}

    err = atlas._write_root_key(str(tmp_path / 'pki' / 'ca.key'), 'KEYPEM', ctx)

    assert err is None
    assert wrote['path'].endswith('ca.key')
    assert wrote['body'] == 'KEYPEM' + chr(10)
    assert wrote['mode'] == 0o600


def test_the_staged_key_is_handed_to_the_container(tmp_path, monkeypatch):
    """⚠️ The ceremony reads it *inside* the container, which runs as
    uid 1000 while the console is 997. The docstring here used to claim the
    two were the same and the code rested on that; measured on the box, they
    are not."""
    chowned = []
    monkeypatch.setattr(atlas, '_chown_priv',
                        lambda p, u, g: chowned.append((p, u, g)))
    ctx = {'_write_priv': lambda path, body, **k: None}

    atlas._write_root_key('/x/pki/ca.key', 'KEYPEM', ctx)

    assert chowned == [('/x/pki/ca.key', atlas.APP_UID, atlas.APP_GID)]


def test_a_key_that_cannot_be_handed_over_is_not_left_lying_there(monkeypatch):
    """⚠️ **The worst secret in the system.** Staged but unreadable by the
    only process that needs it is the exposure with none of the benefit, and
    the caller's `finally` only shreds what it believes it staged."""
    shredded = []
    monkeypatch.setattr(atlas, '_chown_priv', lambda p, u, g: 'EPERM')
    monkeypatch.setattr(atlas, '_shred', lambda p: shredded.append(p) or True)
    ctx = {'_write_priv': lambda path, body, **k: None}

    err = atlas._write_root_key('/x/pki/ca.key', 'KEYPEM', ctx)

    assert err and 'hand the staged key' in err
    assert shredded == ['/x/pki/ca.key']


def test_a_root_era_console_still_writes_it_directly(tmp_path, monkeypatch):
    """No broker on a root-era box, and none needed: it owns the directory."""
    monkeypatch.setattr(atlas, '_chown_priv', lambda p, u, g: None)
    path = tmp_path / 'ca.key'

    err = atlas._write_root_key(str(path), 'KEYPEM', None)

    assert err is None
    assert path.read_text(encoding='utf-8') == 'KEYPEM' + chr(10)


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


# --------------------------------------------------------------------------- #
# The trust bundle carries the intermediate (2026-09-19)
# --------------------------------------------------------------------------- #


def test_the_bundle_includes_the_issuing_certificate(monkeypatch, tmp_path):
    """⚠️ **Root plus every intermediate, which is the whole point of a
    bundle.** Caddy's `trust_pool file` verifies a client certificate against
    what is in that file; once the root is taken offline devices are issued by
    the intermediate, and the root alone cannot verify them."""
    pki = tmp_path / 'atlas' / 'default' / 'pki'
    pki.mkdir(parents=True)
    (pki / 'ca.crt').write_text('ROOT-CERT', encoding='utf-8')
    (pki / 'issuing.crt').write_text('ISSUING-CERT', encoding='utf-8')
    monkeypatch.setattr(atlas, 'install_base',
                        lambda ctx=None: str(tmp_path).replace(chr(92), '/'))
    monkeypatch.setattr(atlas, 'caddy_base',
                        lambda: str(tmp_path / 'caddy').replace(chr(92), '/'))
    monkeypatch.setattr(atlas, '_run_root', lambda argv, timeout=120: (0, ''))
    written = {}

    atlas.sync_device_ca_for_caddy(
        None, {'_write_priv': lambda p, body, **k: written.update({'body': body})})

    assert 'ROOT-CERT' in written['body']
    assert 'ISSUING-CERT' in written['body'], written['body']


def _staged(monkeypatch, tmp_path, files, said=None):
    """Run the staging with `files` in the plain deployment's `pki/`.

    Returns what was written to Caddy's trust pool, or None.
    """
    pki = tmp_path / 'atlas' / 'default' / 'pki'
    pki.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (pki / name).write_text(body, encoding='utf-8')
    monkeypatch.setattr(atlas, 'install_base',
                        lambda ctx=None: str(tmp_path).replace(chr(92), '/'))
    monkeypatch.setattr(atlas, 'caddy_base',
                        lambda: str(tmp_path / 'caddy').replace(chr(92), '/'))
    monkeypatch.setattr(atlas, '_run_root', lambda argv, timeout=120: (0, ''))
    if said is not None:
        monkeypatch.setattr(atlas, 'print',
                            lambda *a, **k: said.append(' '.join(map(str, a))),
                            raising=False)
    written = {}
    atlas.sync_device_ca_for_caddy(
        None, {'_write_priv': lambda p, body, **k: written.update({'body': body})})
    return written.get('body')


# --------------------------------------------------------------------------- #
# ATLAS publishes the bundle; this reads it (W234)
# --------------------------------------------------------------------------- #


def test_the_published_bundle_is_used_when_it_is_there(monkeypatch, tmp_path):
    """⚠️ **The retired intermediates are in it and cannot be found any
    other way.** The glob this replaced ran as the console against a
    `drwx------` directory owned by the application's uid: it returned
    nothing, silently, so a renewed CA staged a pool missing every retired
    intermediate and the devices they issued stopped being trusted at the
    edge."""
    body = _staged(monkeypatch, tmp_path, {
        'ca.crt': 'ROOT-CERT',
        'issuing.crt': 'ISSUING-CERT',
        atlas.BUNDLE_CERT: 'ROOT-CERT' + chr(10) + 'RETIRED-ONE' + chr(10) + 'ISSUING-CERT' + chr(10) + '',
    })

    assert 'RETIRED-ONE' in body, body


def test_the_published_bundle_is_taken_whole_not_added_to(monkeypatch, tmp_path):
    """⚠️ ATLAS decides what this deployment trusts, and it is the only
    thing that can see the full set. Concatenating the loose files on top
    would put a certificate ATLAS had deliberately dropped -- an intermediate
    removed because it was compromised -- back into the pool."""
    body = _staged(monkeypatch, tmp_path, {
        'ca.crt': 'ROOT-CERT',
        'issuing.crt': 'REVOKED-AND-REMOVED',
        atlas.BUNDLE_CERT: 'ROOT-CERT' + chr(10) + 'CURRENT-ISSUING' + chr(10) + '',
    })

    assert 'REVOKED-AND-REMOVED' not in body, body
    assert body.strip() == 'ROOT-CERT' + chr(10) + 'CURRENT-ISSUING'


def test_an_atlas_without_a_bundle_still_works(monkeypatch, tmp_path):
    """⚠️ **The fallback is not optional.** A deployment on a release
    older than W234 publishes no bundle, and refusing to stage would strand
    it on the upgrade that would have fixed it."""
    body = _staged(monkeypatch, tmp_path, {
        'ca.crt': 'ROOT-CERT', 'issuing.crt': 'ISSUING-CERT'})

    assert 'ROOT-CERT' in body
    assert 'ISSUING-CERT' in body


def test_falling_back_says_what_it_cannot_include(monkeypatch, tmp_path):
    """⚠️ **And it is not safe, so it must not be quiet.** What the
    fallback assembles is correct until that deployment's first CA renewal
    and wrong, invisibly, ever after: nothing errors, devices issued by a
    retired intermediate simply stop being trusted."""
    said = []
    _staged(monkeypatch, tmp_path,
            {'ca.crt': 'ROOT-CERT', 'issuing.crt': 'ISSUING-CERT'}, said=said)

    joined = chr(10).join(said)

    assert 'retired' in joined.lower(), joined
    assert atlas.BUNDLE_CERT in joined, joined


def test_a_published_bundle_is_not_announced_as_a_problem(monkeypatch, tmp_path):
    """⚠️ A warning on the healthy path is a warning nobody reads."""
    said = []
    _staged(monkeypatch, tmp_path, {
        'ca.crt': 'ROOT-CERT',
        atlas.BUNDLE_CERT: 'ROOT-CERT' + chr(10) + 'ISSUING-CERT' + chr(10) + ''}, said=said)

    assert not [line for line in said if 'retired' in line.lower()], said


def test_the_console_no_longer_globs_a_directory_it_cannot_read():
    """⚠️ The bug was the glob, and a glob that returns nothing looks
    exactly like a deployment that has never rotated. Asserted on the source
    so that reintroducing it fails here rather than at a tablet."""
    import inspect

    source = inspect.getsource(atlas.sync_device_ca_for_caddy)
    code = chr(10).join(line for line in source.split(chr(10))
                        if not line.lstrip().startswith('#'))

    assert '_glob(' not in code, (
        'the console cannot list pki/retired -- it is drwx------ and owned by '
        'the application. Read the published bundle instead.')


def test_the_bundle_name_matches_the_one_atlas_publishes():
    """⚠️ **A contract across two repositories, checked where both are
    checked out.** Skipped elsewhere -- a box has only this one -- but this is
    the machine where either side gets changed, so it is the machine where
    drift has to fail.
    """
    import os
    import re

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ca_py = os.path.join(os.path.dirname(here), 'TAK-MDM',
                         'app', 'security', 'ca.py')
    if not os.path.isfile(ca_py):
        pytest.skip('the ATLAS checkout is not beside this one')

    with io.open(ca_py, encoding='utf-8') as handle:
        found = re.search(r'^BUNDLE_CERT = "([^"]+)"', handle.read(), re.M)

    assert found, 'ATLAS no longer defines BUNDLE_CERT'
    assert found.group(1) == atlas.BUNDLE_CERT, (
        'ATLAS publishes %r and this reads %r' % (found.group(1),
                                                  atlas.BUNDLE_CERT))


def test_deploy_restages_the_bundle_after_issuing(monkeypatch):
    """⚠️ **The ordering bug, found on the box.** Step 6 stages the trust pool;
    the CA ceremony that creates the intermediate runs *after* it. A fresh
    deployment therefore had a pool holding only the root — one certificate,
    `CN = TAK-MDM Device CA`, with `issuing.crt` sitting unused beside it.

    The failure would never appear in a deploy log: it arrives later, at a
    tablet, as a TLS handshake that refuses.
    """
    import inspect

    source = inspect.getsource(atlas.deploy)
    staged = source.index('Device CA staged for Caddy')
    issued = source.index('Issuing certificate created')
    restaged = source.index('Trust bundle re-staged')

    assert staged < issued < restaged, (
        're-staging must come after the intermediate is created')
