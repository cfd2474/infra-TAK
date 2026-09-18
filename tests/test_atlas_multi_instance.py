"""The store layer, per instance (W216, chunk 3).

Chunks 1 and 2 gave the naming rules and the two sizing modes. This is where the
store functions stop reading module globals and start taking an instance — which
is the change that can reach a running deployment, so most of what follows pins
the *plain* instance to exactly what it does today.

⚠️ **The guarantee under test is compartmentalisation.** An operation on one
agency must never name another instance's image, mount, volume or directory.
Chunk 1 proved the *names* differ; this proves the *code* uses them.
"""

import os
import pathlib
import sys

import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modules.atlas as atlas  # noqa: E402
from modules import atlas_instances as ai  # noqa: E402

GIB = 1024 ** 3


def posix(path):
    """A box-shaped path. ⚠️ The module builds paths with `posixpath` because a
    managed box is always Linux; a Windows temp path in an expectation would
    compare against separators that exist nowhere in production."""
    return str(path).replace(chr(92), '/')
PLAIN = ai.make(None, ai.MODE_FIXED, 100, 8760)
AGENCY = ai.make('agencya', ai.MODE_DYNAMIC, 50, 8761)


# --------------------------------------------------------------------------- #
# Where deployments live
# --------------------------------------------------------------------------- #


def test_the_layout_probe_finds_a_root_install(monkeypatch):
    """What the live box looks like: `/root/atlas/.git` exists."""
    monkeypatch.setattr(atlas, '_glob', lambda pattern: ['/root/atlas/.git'])

    assert atlas.install_base() == '/root'


def test_the_layout_probe_falls_back_to_the_home_directory(monkeypatch):
    """A console born unprivileged keeps its modules under its own home."""
    monkeypatch.setattr(atlas, '_glob', lambda pattern: [])

    assert atlas.install_base() == os.path.expanduser('~')


def test_a_box_with_only_agency_installs_still_resolves_to_root(monkeypatch):
    """⚠️ The reason the probe globs `atlas*` rather than checking `atlas`
    alone. A box whose only deployments are agencies would otherwise fall back
    to a home directory and deploy the next one somewhere else entirely."""
    seen = []
    monkeypatch.setattr(atlas, '_glob',
                        lambda pattern: seen.append(pattern) or ['/root/atlas-a/.git'])

    assert atlas.install_base() == '/root'
    assert 'atlas*' in seen[0]


def test_atlas_dir_still_answers_for_the_plain_instance(monkeypatch):
    """⚠️ One argument, and the same answer as before this chunk. Every existing
    call site and test double passes exactly one; widening the signature broke
    33 of them at once, which was the right signal."""
    monkeypatch.setattr(atlas, 'install_base', lambda ctx=None: '/root')

    assert atlas.atlas_dir(None) == '/root/atlas'
    assert atlas.atlas_dir() == '/root/atlas'


# --------------------------------------------------------------------------- #
# instance_paths
# --------------------------------------------------------------------------- #


@pytest.fixture
def rooted(monkeypatch):
    monkeypatch.setattr(atlas, 'install_base', lambda ctx=None: '/root')
    return None


def test_the_plain_instance_still_resolves_to_the_live_paths(rooted):
    """⚠️ **The test that protects the running deployment.** These are read off
    the live box. If this file ever changes them, an existing install is
    stranded: a deploy would build a new store beside the real one and report
    success."""
    p = atlas.instance_paths(None, None)

    assert p['dir'] == '/root/atlas'
    assert p['store'] == '/root/atlas/store'
    assert p['artifacts'] == '/root/atlas/artifacts'
    assert p['cache'] == '/root/atlas/cache'
    assert p['pg_volume'] == atlas.STORE_PG_VOLUME == 'takmdm_pgdata'


def test_an_agency_resolves_beside_it_on_the_same_layout(rooted):
    p = atlas.instance_paths(None, AGENCY)

    assert p['dir'] == '/root/atlas-agencya'
    assert p['store'] == '/root/atlas-agencya/store'
    assert p['pg_volume'] == 'takmdm-agencya_pgdata'
    assert p['compose_project'] == 'takmdm-agencya'


def test_agencies_follow_a_home_layout_too(monkeypatch):
    """⚠️ One box, one layout. An agency must not land under `/root` because a
    constant said so while the plain instance lives in a home directory."""
    monkeypatch.setattr(atlas, 'install_base', lambda ctx=None: '/home/console')

    assert atlas.instance_paths(None, AGENCY)['dir'] == '/home/console/atlas-agencya'
    assert atlas.instance_paths(None, None)['dir'] == '/home/console/atlas'


def test_no_instance_shares_a_path_with_another(rooted):
    a = atlas.instance_paths(None, None)
    b = atlas.instance_paths(None, AGENCY)

    for field in ('dir', 'store', 'artifacts', 'cache',
                  'pg_volume', 'compose_project'):
        assert a[field] != b[field], f'{field} is shared between instances'


# --------------------------------------------------------------------------- #
# The store functions act on the instance they are given
# --------------------------------------------------------------------------- #


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=120):
        self.calls.append(list(argv))
        # ⚠️ **`rm -rf` actually removes.** `remove_store` checks that the
        # directory is gone rather than trusting the exit code — the failure
        # it replaces reported success over a database still on disk — so a
        # fake that records the call and deletes nothing makes the real check
        # look like a bug. Faithful beats convenient.
        if argv[:2] == ['rm', '-rf'] and os.path.isdir(argv[2]):
            shutil.rmtree(argv[2])
        return 0, ''

    def text(self):
        return chr(10).join(' '.join(c) for c in self.calls)


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A pretend box where both deployments could exist.

    ⚠️ Far smaller than it was (W230). It used to patch `STORE_IMAGE`,
    `_run_root`, `_bind_unit`, `_bind_pg_volume`, `os.chown`, `os.chmod` and
    `os.path.ismount`, because creating a store meant a loop image, a
    filesystem and three systemd units. It is now directories plus one
    brokered `chown`, and the fixture shrank to match.
    """
    monkeypatch.setattr(atlas, 'install_base', lambda ctx=None: posix(tmp_path))
    runner = Runner()
    monkeypatch.setattr(atlas, '_run_root', runner)
    monkeypatch.setattr(atlas, '_bind_pg_volume', lambda *a, **k: None)
    return runner


def test_creating_an_agency_store_makes_only_its_own_directories(box, tmp_path):
    """⚠️ Compartmentalisation, asserted against the filesystem."""
    err = atlas.ensure_store({}, 4 * GIB, lambda *_: None,
                             mode=ai.MODE_DYNAMIC, inst=AGENCY)

    assert err is None, err
    assert (tmp_path / 'atlas-agencya' / 'store' / 'pgdata').is_dir()
    assert (tmp_path / 'atlas-agencya' / 'artifacts').is_dir()
    assert not (tmp_path / 'atlas').exists(), 'the plain deployment was touched'


def test_creating_the_plain_store_never_names_an_agency(box, tmp_path):
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, inst=None)

    assert (tmp_path / 'atlas' / 'store' / 'pgdata').is_dir()
    assert 'agencya' not in box.text()
    assert not (tmp_path / 'atlas-agencya').exists()


def test_the_database_directory_is_not_chowned_at_all(box, tmp_path):
    """⚠️ **The fix that the first fix needed.**

    `postgres:16-alpine` is uid 70 and will not start unless it owns its data
    directory, so this used to chown `pgdata` to 70 through the shimmed
    binary. Measured on the converted box: the `chown` shim only routes paths
    under `/etc /opt /usr /var /run /boot /swapfile`, so a path in the
    console's own home falls through to `/usr/bin/chown` and fails with
    *"Operation not permitted"*. The deploy stopped there.

    Widening the shim to broker chowns anywhere under `/home` is a large
    grant for a small need; running the database as the console's own uid
    needs no privilege at all.
    """
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, inst=AGENCY)

    assert 'chown' not in box.text(), box.text()


def test_the_override_does_not_override_either_container_user(tmp_path):
    """⚠️ **Tried, and both images refused.**

    Running the containers as the console's own uid would have removed the
    host-side chown entirely. Measured on the box: Postgres could not chmod
    its data directory (*"initdb: could not change permissions"*) and ATLAS's
    image is `USER takmdm`, so its init step died with
    `PermissionError: /pki/ca.crt`. Each image expects to own what it writes,
    so the ownership is fixed on the host instead, through the broker.
    """
    body = atlas._COMPOSE_OVERRIDE.format(app_port=8760)

    # ⚠️ A *key*, not the word. The comment above it in the template
    # explains why there is no `user:` -- matching the prose would have this
    # fail on the explanation for its own absence.
    keys = [line.strip() for line in body.split(chr(10))
            if not line.strip().startswith('#')]
    assert not [k for k in keys if k.startswith('user:')], body


def test_nothing_reaches_for_a_loop_device_or_a_mount(box):
    """⚠️ The whole point of W230: none of these binaries is shimmed, and the
    console cannot run them. A regression here is a deploy that fails on a
    real box and passes here."""
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, inst=AGENCY)

    ran = box.text()
    for forbidden in ('losetup', 'mount', 'mkfs', 'resize2fs', 'e2fsck',
                      'fallocate', 'truncate', 'systemd-escape', 'systemctl'):
        assert forbidden not in ran, '%s is not available to the console: %s' % (
            forbidden, ran)


def test_removing_an_agency_store_leaves_the_plain_one_alone(box, tmp_path):
    """⚠️ The offboarding case: a teardown naming the wrong path deletes a
    live agency's database."""
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, inst=None)
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, inst=AGENCY)

    did, errs = atlas.remove_store({}, lambda *_: None, inst=AGENCY)

    assert errs == [], errs
    assert (tmp_path / 'atlas' / 'store').is_dir(), 'the plain store was removed'
    assert posix(tmp_path / 'atlas-agencya' / 'store') in chr(10).join(did)


def test_removing_an_agency_removes_only_its_own_volume(box):
    atlas.remove_store({}, lambda *_: None, inst=AGENCY)

    ran = box.text()
    assert 'takmdm-agencya_pgdata' in ran
    assert 'takmdm_pgdata ' not in ran + ' '


def test_removing_a_clean_box_reports_no_errors(box):
    """An uninstall has to be re-runnable after a partial failure."""
    did, errs = atlas.remove_store({}, lambda *_: None, inst=AGENCY)

    assert errs == []


def test_the_volume_goes_before_the_directories(box, tmp_path):
    """⚠️ Leaving it behind makes the next deploy refuse -- `_bind_pg_volume`
    rejects a volume already pointing somewhere unexpected."""
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, inst=AGENCY)

    atlas.remove_store({}, lambda *_: None, inst=AGENCY)

    calls = [' '.join(c) for c in box.calls]
    volume = next(i for i, c in enumerate(calls) if 'volume rm' in c)
    removal = next(i for i, c in enumerate(calls) if c.startswith('rm -rf'))
    assert volume < removal, calls


def test_the_default_instance_is_still_the_plain_one(box, tmp_path):
    atlas.ensure_store({}, 4 * GIB, lambda *_: None)

    assert (tmp_path / 'atlas' / 'store').is_dir()


def test_no_instance_path_escapes_the_redirected_base(monkeypatch, tmp_path):
    """The guard that would have caught item 9 on any platform.

    ⚠️ On Windows `/var/lib/...` resolves to the drive root, so the tests
    that created it passed here for weeks while creating them under the drive
    root. Asserting containment does not care which platform it runs on.
    """
    base = posix(tmp_path)
    monkeypatch.setattr(atlas, 'install_base', lambda ctx=None: base + '/atlas')

    for inst in (None, AGENCY):
        paths = atlas.instance_paths(None, inst)
        for key in ('dir', 'store', 'artifacts', 'cache'):
            assert paths[key].startswith(base), (inst, key, paths[key])


def test_binding_the_database_volume_targets_the_named_volume(monkeypatch):
    runner = Runner()
    monkeypatch.setattr(atlas, '_run_root', runner)

    atlas._bind_pg_volume('/somewhere/pgdata', lambda *_: None,
                          volume='takmdm-b_pgdata')

    assert 'takmdm-b_pgdata' in runner.text()


# --------------------------------------------------------------------------- #
# Threading a parameter half-way
# --------------------------------------------------------------------------- #


def _function_source(name):
    """The source of one top-level function in the module."""
    text = (ROOT / 'modules' / 'atlas.py').read_text(encoding='utf-8')
    start = text.index(chr(10) + 'def %s(' % name) + 1
    end = text.index(chr(10) + 'def ', start + 1)
    return text[start:end]


#: ⚠️ **Both jobs, not just `deploy`.** `_run_update` is `deploy` minus the
#: destructive parts and had every one of the same constants — the plain
#: checkout, the plain compose project, the plain network, the plain version
#: key. Three mutations survived the chunk-8 sweep until these were
#: parametrised, so the generalisation is not tidiness: it is the only thing
#: standing between an agency update and a rebuild of somebody else's
#: deployment.
JOBS = ('deploy', '_run_update')


@pytest.mark.parametrize('job', JOBS)
def test_no_job_resolves_a_path_outside_its_identity(job):
    """⚠️ `atlas_dir(ctx)` is the *plain* install directory. Calling it inside
    a job that has already resolved its own deployment is how an agency update
    came to `git fetch` in `/root/atlas` — which on a box with no plain
    deployment does not exist, and on a box with one updates the wrong tree."""
    source = _function_source(job)

    assert 'atlas_dir(' not in source,         '%s resolves the plain install directory rather than its own' % job


#: Helpers whose *default* is the plain deployment, so a call that omits the
#: instance silently acts on somebody else's. ⚠️ Listed explicitly rather than
#: matched by shape: `_installed_version(ctx, inst)` takes it positionally and
#: a shape rule would either miss these or flag that.
SCOPED_HELPERS = ('_compose', '_compose_exec', '_verify_access_control',
                  'ensure_authentik_app')


@pytest.mark.parametrize('job', JOBS)
def test_every_scoped_call_in_a_job_names_its_deployment(job):
    """⚠️ `_verify_access_control` was the last one found, by a mutation that
    survived: an agency update re-checked the *plain* deployment's Authentik
    binding, recorded the answer against the agency, and reported it in the
    agency's log. The tile would then have gone green for a console nobody had
    checked."""
    import re

    source = _function_source(job)
    seen = 0
    for fn in SCOPED_HELPERS:
        for call in re.findall(r'%s\((?:[^()]|\([^()]*\))*\)' % fn, source):
            seen += 1
            flat = ' '.join(call.split())
            assert 'inst=' in flat,                 '%s: this call still targets the plain deployment: %s'                 % (job, flat)

    assert seen, '%s no longer calls any of %s' % (job, ', '.join(SCOPED_HELPERS))


@pytest.mark.parametrize('job', JOBS)
def test_every_plain_defaulted_call_in_a_job_names_its_deployment(job):
    import re

    source = _function_source(job)

    for fn in ('_bridge_gateway', '_set_trusted_proxies'):
        for call in re.findall(r'%s\((?:[^()]|\([^()]*\))*\)' % fn, source):
            flat = ' '.join(call.split())
            assert '_me[' in flat,                 '%s: this call still takes the plain default: %s' % (job, flat)


@pytest.mark.parametrize('job', JOBS)
def test_no_job_writes_a_setting_under_a_hardcoded_plain_key(job):
    import re

    source = _function_source(job)
    writes = re.findall(r"s(?:_early)?\[f?'\{(\w+)\}(\w+)'\]", source)

    for var, key in writes:
        if var == 'KEY':
            assert key == '_enabled',                 "%s writes the plain 'atlas%s' for every deployment" % (job, key)
        else:
            assert var == '_prefix',                 '%s: settings written under %r, not the deployment prefix'                 % (job, var)


def test_every_compose_call_in_deploy_names_its_instance():
    """⚠️ **The bug this is here for.** `deploy` resolved the instance for its
    directory and its store and then left every `_compose` call on the default,
    which is the plain deployment. On a box whose plain ATLAS had been
    uninstalled, compose ran against `/root/atlas` — a directory that does not
    exist — and answered *"no configuration file provided: not found"* after
    three steps had already reported success.

    Threading half a parameter is worse than not threading it at all: the half
    that works hides the half that does not.
    """
    import re

    source = _function_source('deploy')
    # ⚠️ Whole call expressions, not lines: one of these spans four lines, and a
    # line-based check reports the opening parenthesis as the offender — which
    # is true but unhelpful, and would pass the moment someone reflowed it.
    calls = re.findall(r'_compose(?:_exec)?\((?:[^()]|\([^()]*\))*\)', source)

    assert calls, 'deploy no longer runs compose at all'
    for call in calls:
        flat = ' '.join(call.split())
        assert 'inst=' in flat, f'this call still targets the plain deployment: {flat}'


def test_deploy_resolves_the_instance_from_the_recorded_list():
    """So a deploy cannot build a deployment nobody registered.

    ⚠️ The paths now arrive through `deployment_identity`, which is where the
    hostname, port and settings prefix are decided too. Asking for
    `instance_paths` by name here is what this test used to do, and it failed
    the moment that indirection appeared — a test of the spelling, not of the
    behaviour.
    """
    source = _function_source('deploy')

    assert 'load_instances(ctx)' in source
    assert 'deployment_identity(ctx, _inst' in source


def test_every_plain_defaulted_call_in_deploy_names_its_deployment():
    """⚠️ **The same half-threading, one layer down.** `_bridge_gateway` and
    `_set_trusted_proxies` both default to the plain deployment's Docker
    network, so an agency silently took the RFC1918 fallback and never narrowed
    — the S-1 mitigation not applying to exactly the deployments a box gains
    from here on.

    ⚠️ **Not a lockout, which is why nothing reported it.** The fallback still
    refuses a request from a public address, so the deploy succeeded, the
    console worked, and the control was simply wider than it should be. A
    behavioural test would have to run `deploy` end to end against Docker; this
    asks the one question that distinguishes the two cases.
    """
    import re

    source = _function_source('deploy')

    for fn in ('_bridge_gateway', '_set_trusted_proxies'):
        calls = re.findall(r'%s\((?:[^()]|\([^()]*\))*\)' % fn, source)
        assert calls, 'deploy no longer calls %s' % fn
        for call in calls:
            flat = ' '.join(call.split())
            assert '_me[' in flat, \
                'this call still takes the plain default: %s' % flat


def test_deploy_writes_no_setting_under_a_hardcoded_plain_key():
    """⚠️ Every generated value a deploy records belongs to *that* deployment.
    `atlas_pg_password` shared between two of them makes the second database
    permanently unopenable, and the failure appears as
    `FATAL: password authentication failed` long after the deploy said it
    succeeded.

    `atlas_enabled` is the one exception and is asserted rather than excluded:
    it means "the ATLAS module is installed on this box", which is what
    `detect`, the tile and `caddy_sites` ask. Prefixing it would have made an
    agency-only box report ATLAS absent and emit no vhost at all.
    """
    import re

    source = _function_source('deploy')
    writes = re.findall(r"s(?:_early)?\[f?'\{(\w+)\}(\w+)'\]", source)

    assert writes, 'deploy no longer records anything'
    # ⚠️ Asserted positively as well as negatively. Prefixing `atlas_enabled`
    # satisfies every "not the plain key" rule below and is still wrong: an
    # agency-only box would report ATLAS absent, `caddy_sites` would emit no
    # vhost, and the console would offer to install what is already running.
    assert ('KEY', '_enabled') in writes,         'deploy no longer records that ATLAS is installed on this box'
    for var, key in writes:
        if var == 'KEY':
            assert key == '_enabled', \
                "deploy writes the plain 'atlas%s' for every deployment" % key
        else:
            assert var == '_prefix', \
                'settings are written under %r, not the deployment prefix' % var
