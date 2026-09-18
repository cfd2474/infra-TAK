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
    assert p['mount'] == '/root/atlas/store'
    assert p['artifacts'] == '/root/atlas/artifacts'
    assert p['cache'] == '/root/atlas/cache'
    assert p['image'] == atlas.STORE_IMAGE == '/var/lib/atlas/store.img'
    assert p['pg_volume'] == atlas.STORE_PG_VOLUME == 'takmdm_pgdata'


def test_an_agency_resolves_beside_it_on_the_same_layout(rooted):
    p = atlas.instance_paths(None, AGENCY)

    assert p['dir'] == '/root/atlas-agencya'
    assert p['mount'] == '/root/atlas-agencya/store'
    assert p['image'] == '/var/lib/atlas-agencya/store.img'
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

    for field in ('dir', 'mount', 'artifacts', 'cache', 'image', 'image_dir',
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
        if argv[0] == 'systemd-escape':
            return 0, 'unit.mount'
        if argv[:2] == ['losetup', '-j']:
            return 0, ''
        return 0, ''

    def text(self):
        return '\n'.join(' '.join(c) for c in self.calls)


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A pretend box where both instances could exist."""
    monkeypatch.setattr(atlas, 'install_base', lambda ctx=None: posix(tmp_path))
    monkeypatch.setattr(atlas, 'STORE_IMAGE',
                        posix(tmp_path / 'var' / 'store.img'))
    runner = Runner()
    monkeypatch.setattr(atlas, '_run_root', runner)
    monkeypatch.setattr(atlas, '_bind_unit', lambda *a, **k: None)
    monkeypatch.setattr(atlas, '_bind_pg_volume', lambda *a, **k: None)
    monkeypatch.setattr(atlas.os, 'chown', lambda *a: None, raising=False)
    monkeypatch.setattr(atlas.os, 'chmod', lambda *a: None)
    monkeypatch.setattr(atlas.os.path, 'ismount', lambda _p: True)
    for inst in (None, AGENCY):
        pathlib.Path(atlas.instance_paths(None, inst)['mount']).mkdir(
            parents=True, exist_ok=True)
    return runner


def test_creating_an_agency_store_never_names_the_plain_one(box, tmp_path):
    """⚠️ **Compartmentalisation, asserted against the commands actually run.**
    Chunk 1 proved the names differ; this proves nothing reaches for the wrong
    one."""
    atlas.ensure_store({}, 4 * GIB, lambda *_: None,
                       mode=ai.MODE_DYNAMIC, inst=AGENCY)

    ran = box.text()

    assert 'atlas-agencya' in ran
    assert posix(tmp_path / 'var' / 'store.img') not in ran


def test_creating_the_plain_store_never_names_an_agency(box, tmp_path):
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, inst=None)

    ran = box.text()

    assert posix(tmp_path / 'var' / 'store.img') in ran
    assert 'agencya' not in ran


def test_removing_an_agency_store_leaves_the_plain_one_alone(box, tmp_path):
    """⚠️ The offboarding case, and the one W212 warned about at N instances:
    a teardown that names the wrong image deletes a live agency's database."""
    plain_image = pathlib.Path(atlas.STORE_IMAGE)
    plain_image.parent.mkdir(parents=True, exist_ok=True)
    plain_image.write_bytes(bytes(10))
    agency_image = pathlib.Path(atlas.instance_paths(None, AGENCY)['image'])
    agency_image.parent.mkdir(parents=True, exist_ok=True)
    agency_image.write_bytes(bytes(10))

    did, errs = atlas.remove_store({}, lambda *_: None, inst=AGENCY)

    assert errs == []
    assert not agency_image.exists(), "the agency's own image survived"
    assert plain_image.exists(), "removing an agency deleted the plain store"


def test_removing_an_agency_removes_only_its_own_volume(box):
    atlas.remove_store({}, lambda *_: None, inst=AGENCY)

    ran = box.text()

    assert 'takmdm-agencya_pgdata' in ran
    assert 'takmdm_pgdata ' not in ran + ' '


def test_the_default_instance_is_still_the_plain_one(box, tmp_path):
    """⚠️ Every existing call site omits `inst`. If the default moved, an
    ordinary deploy would build a store for an agency that does not exist."""
    atlas.ensure_store({}, 4 * GIB, lambda *_: None)

    assert posix(tmp_path / 'var' / 'store.img') in box.text()


def test_the_facts_read_the_instance_s_own_image(box, monkeypatch):
    """⚠️ **Asserted on the image actually read, not on a rounded figure.** The
    first version checked `reserved_gb == 0.0`, which is true whether it reads
    the agency's image or the plain one — so it survived a mutation that ignored
    the instance entirely. A number that cannot distinguish the two answers is
    not a test of which one was used."""
    monkeypatch.setattr(atlas, '_disk_free', lambda _p: (500 * GIB, 400 * GIB))
    agency_path = atlas.instance_paths(None, AGENCY)['image']
    agency_image = pathlib.Path(agency_path)
    agency_image.parent.mkdir(parents=True, exist_ok=True)
    agency_image.write_bytes(bytes(2048))
    read = []
    monkeypatch.setattr(atlas, 'allocated_bytes',
                        lambda path: read.append(path) or 0)

    facts = atlas.store_facts({}, mode=ai.MODE_DYNAMIC, inst=AGENCY)

    # ⚠️ Compared against the derived string, not `str(Path(...))` — pathlib
    # hands back the development machine's separators and the module emits the
    # box's.
    assert read == [agency_path], 'store_facts read the wrong image'
    assert facts['mode'] == ai.MODE_DYNAMIC
    assert facts['size_is_reserved'] is False


def test_the_module_constant_stays_authoritative_for_the_plain_image(monkeypatch):
    """⚠️ `derive` would produce the same path, but `STORE_IMAGE` is what a
    deployment configures — so the plain instance must follow the constant, not
    a string rebuilt from a name."""
    monkeypatch.setattr(atlas, 'install_base', lambda ctx=None: '/root')
    monkeypatch.setattr(atlas, 'STORE_IMAGE', '/mnt/elsewhere/store.img')

    assert atlas.instance_paths(None, None)['image'] == '/mnt/elsewhere/store.img'
    assert atlas.instance_paths(None, AGENCY)['image'] == (
        '/var/lib/atlas-agencya/store.img')


def test_reserving_blocks_targets_the_image_it_was_given(monkeypatch):
    """`_reserve_blocks` used to know only one image. With N instances it has to
    be told, or an agency's deploy would fill the plain instance's holes."""
    runner = Runner()
    monkeypatch.setattr(atlas, '_run_root', runner)
    monkeypatch.setattr(atlas, 'allocated_bytes', lambda _p: 0)

    atlas._reserve_blocks(4 * GIB, lambda *_: None, image='/var/lib/atlas-b/store.img')

    assert '/var/lib/atlas-b/store.img' in runner.text()


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
