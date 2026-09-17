"""The instance list and the capacity it is judged against (W216, chunk 4).

⚠️ **The migration is the important part.** Every ATLAS installation that exists
today predates `atlas_instances`, so there is no record of it anywhere — only
`atlas_enabled` and a store on disk. A console that read `[]` there would tell
the operator nothing is installed, offer a *plain* deployment that is already
running, and let a second one be built on top of the first.

The rest is arithmetic for the deploy screen, which has one job beyond adding up:
**naming which constraint bites first.** On the box measured, disk allowed four
or five more instances and memory about twelve, and an operator shown only the
larger figure would plan for twice what fits.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modules.atlas as atlas  # noqa: E402
from modules import atlas_instances as ai  # noqa: E402

GIB = ai.GIB


class Ctx(dict):
    """Just enough console to hold settings."""

    def __init__(self, settings=None):
        super().__init__()
        self.settings = dict(settings or {})
        self['load_settings'] = lambda: dict(self.settings)
        self['save_settings'] = self._save

    def _save(self, s):
        self.settings = dict(s)


# --------------------------------------------------------------------------- #
# The list, and the deployment that predates it
# --------------------------------------------------------------------------- #


def test_a_box_with_nothing_installed_has_no_instances():
    assert atlas.load_instances(Ctx()) == []


def test_a_deployment_made_before_the_list_still_appears(monkeypatch, tmp_path):
    """⚠️ **The migration.** Without this an upgraded console would report an
    installed ATLAS as absent — and then offer to deploy a second plain one over
    the top of it."""
    image = tmp_path / 'store.img'
    image.write_bytes(bytes(1024))
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(image))

    found = atlas.load_instances(Ctx({'atlas_enabled': True}))

    assert len(found) == 1
    assert found[0]['slug'] is None
    assert found[0]['mode'] == ai.MODE_FIXED


def test_the_migrated_instance_blocks_a_second_plain_deployment(monkeypatch, tmp_path):
    """The consequence that matters: the rule from chunk 1 has to see it."""
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'absent.img'))

    found = atlas.load_instances(Ctx({'atlas_enabled': True}))

    assert ai.may_deploy_plain(found) is False


def test_the_migrated_instance_is_sized_from_the_store_on_disk(monkeypatch, tmp_path):
    """⚠️ Byte-scale, because `truncate` allocates for real on NTFS — the
    145 GB lesson. The path matters, not the magnitude."""
    image = tmp_path / 'store.img'
    image.write_bytes(bytes(3 * 1024))
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(image))

    found = atlas.load_instances(Ctx({'atlas_enabled': True}))

    assert found[0]['size_gb'] == 0  # 3 KB rounds to nothing; it was read


def test_the_migration_is_not_written_back(monkeypatch, tmp_path):
    """⚠️ Derived on every read, so it stays true if the store is resized, and
    nothing is persisted about a deployment the operator has not touched."""
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'absent.img'))
    ctx = Ctx({'atlas_enabled': True})

    atlas.load_instances(ctx)

    assert ai.INSTANCES_KEY not in ctx.settings


def test_an_explicit_list_wins_over_the_migration(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'absent.img'))
    recorded = [ai.make('agency-a', ai.MODE_DYNAMIC, 50, 8761)]

    found = atlas.load_instances(
        Ctx({'atlas_enabled': True, ai.INSTANCES_KEY: recorded}))

    assert [i['slug'] for i in found] == ['agency-a']


def test_saving_replaces_rather_than_merges():
    """⚠️ Removing an instance has to be expressible, and a merge cannot say
    'this one is gone'."""
    ctx = Ctx()
    atlas.save_instances(ctx, [ai.make('a', ai.MODE_FIXED, 10, 8760),
                               ai.make('b', ai.MODE_FIXED, 10, 8761)])

    atlas.save_instances(ctx, [ai.make('a', ai.MODE_FIXED, 10, 8760)])

    assert [i['slug'] for i in atlas.load_instances(ctx)] == ['a']


def test_dropping_an_instance_forgets_only_that_one():
    ctx = Ctx()
    atlas.save_instances(ctx, [ai.make(None, ai.MODE_FIXED, 10, 8760),
                               ai.make('a', ai.MODE_DYNAMIC, 10, 8761)])

    atlas.drop_instance(ctx, 'a')

    assert [i['slug'] for i in atlas.load_instances(ctx)] == [None]


# --------------------------------------------------------------------------- #
# Creating one
# --------------------------------------------------------------------------- #


@pytest.fixture
def roomy(monkeypatch, tmp_path):
    """A box with plenty of disk and nothing listening."""
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(tmp_path / 'store.img'))
    monkeypatch.setattr(atlas, '_disk_free', lambda _p: (500 * GIB, 400 * GIB))
    monkeypatch.setattr(atlas, '_run_root', lambda *a, **k: (1, ''))
    monkeypatch.setattr(atlas, '_memory_bytes', lambda: (32 * GIB, 20 * GIB))
    return None


def test_the_first_deployment_may_be_plain(roomy):
    ctx = Ctx()

    inst, err = atlas.add_instance(ctx, False, None, ai.MODE_FIXED, 50)

    assert err is None
    assert inst['slug'] is None
    assert atlas.load_instances(ctx)[0]['slug'] is None


def test_a_second_plain_deployment_is_refused_with_a_reason(roomy):
    ctx = Ctx()
    atlas.add_instance(ctx, False, None, ai.MODE_FIXED, 50)

    inst, err = atlas.add_instance(ctx, False, None, ai.MODE_FIXED, 50)

    assert inst is None
    assert 'agency-specific' in err


def test_an_agency_deployment_records_its_slug_and_mode(roomy):
    ctx = Ctx()

    inst, err = atlas.add_instance(ctx, True, 'Agency-A', ai.MODE_DYNAMIC, 50)

    assert err is None
    assert inst['slug'] == 'agency-a', 'the slug was not normalised'
    assert inst['mode'] == ai.MODE_DYNAMIC


def test_a_fixed_and_a_dynamic_instance_can_coexist(roomy):
    """The operator's requirement, end to end through the registry."""
    ctx = Ctx()
    atlas.add_instance(ctx, False, None, ai.MODE_FIXED, 50)
    atlas.add_instance(ctx, True, 'agency-a', ai.MODE_DYNAMIC, 50)

    modes = {i['mode'] for i in atlas.load_instances(ctx)}

    assert modes == {ai.MODE_FIXED, ai.MODE_DYNAMIC}


def test_each_instance_gets_its_own_port(roomy):
    ctx = Ctx()
    atlas.add_instance(ctx, False, None, ai.MODE_FIXED, 50)
    atlas.add_instance(ctx, True, 'agency-a', ai.MODE_DYNAMIC, 50)

    ports = [i['port'] for i in atlas.load_instances(ctx)]

    assert len(set(ports)) == 2


def test_a_duplicate_slug_is_refused(roomy):
    ctx = Ctx()
    atlas.add_instance(ctx, True, 'agency-a', ai.MODE_FIXED, 50)

    inst, err = atlas.add_instance(ctx, True, 'agency-a', ai.MODE_FIXED, 50)

    assert inst is None and 'already' in err


def test_the_instance_is_recorded_before_anything_is_built(roomy):
    """⚠️ So the slug is claimed while the first deploy is still running. A
    half-built instance with no record is the shape of leak W212 was about."""
    ctx = Ctx()

    atlas.add_instance(ctx, True, 'agency-a', ai.MODE_FIXED, 50)

    assert ai.INSTANCES_KEY in ctx.settings


def test_a_bad_mode_is_refused(roomy):
    inst, err = atlas.add_instance(Ctx(), True, 'agency-a', 'elastic', 50)

    assert inst is None and 'sizing mode' in err


# --------------------------------------------------------------------------- #
# Capacity
# --------------------------------------------------------------------------- #


def test_the_budget_leaves_the_floor_alone():
    budget = ai.budget_bytes(473 * GIB, 69 * GIB, 25 * GIB)

    assert round(budget / GIB) == 379


def test_both_modes_count_against_the_budget():
    """⚠️ A dynamic ceiling is a promise even though the space is not held. The
    operator's rule is that a request exceeding the budget is refused, which
    needs a figure including what has already been promised."""
    instances = [ai.make(None, ai.MODE_FIXED, 100, 8760),
                 ai.make('a', ai.MODE_DYNAMIC, 50, 8761)]

    assert round(ai.committed_bytes(instances) / GIB) == 150


def test_only_fixed_instances_actually_hold_disk():
    """The difference that makes dynamic worth having: space inside a dynamic
    ceiling is still available to the rest of the box."""
    instances = [ai.make(None, ai.MODE_FIXED, 100, 8760),
                 ai.make('a', ai.MODE_DYNAMIC, 50, 8761)]

    assert round(ai.reserved_bytes(instances) / GIB) == 100


def test_a_request_past_the_budget_is_refused_with_the_spare_named():
    instances = [ai.make(None, ai.MODE_FIXED, 100, 8760)]

    ok, err = ai.fits(400, instances, 379 * GIB)

    assert ok is False
    assert '279' in err


def test_a_request_inside_the_budget_is_allowed():
    ok, err = ai.fits(50, [ai.make(None, ai.MODE_FIXED, 100, 8760)], 379 * GIB)

    assert ok is True and err is None


def test_a_sizeless_request_is_refused():
    ok, err = ai.fits(0, [], 379 * GIB)

    assert ok is False and 'size' in err


def test_the_screen_says_how_many_more_fit():
    instances = [ai.make(None, ai.MODE_FIXED, 100, 8760)]

    assert ai.instances_that_fit(50, instances, 379 * GIB) == 5


def test_memory_headroom_uses_the_measured_peak():
    """⚠️ The peak, not the steady state: N instances restarting together spike
    together, and the steady figure promises room a restart would not find."""
    allowed = ai.instances_that_ram_allows(20 * GIB, 4 * GIB)

    assert allowed == int((16 * GIB) // ai.INSTANCE_RAM_PEAK_BYTES)


def test_the_binding_constraint_is_named_not_just_computed():
    """⚠️ The whole point of the panel. Disk 4, memory 12 — an operator shown
    only 12 would plan for three times what fits."""
    assert ai.binding_constraint(4, 12) == ('disk', 4)
    assert ai.binding_constraint(30, 12) == ('memory', 12)


def test_a_tie_reports_disk_because_it_is_the_harder_one_to_add():
    assert ai.binding_constraint(5, 5)[0] == 'disk'


def test_the_facts_carry_everything_the_screen_shows(roomy):
    ctx = Ctx()
    atlas.add_instance(ctx, False, None, ai.MODE_FIXED, 100)

    facts = atlas.capacity_facts(ctx, size_gb=50)

    assert facts['instances'] == 1
    assert facts['committed_gb'] == 100.0
    assert facts['floor_gb'] == 25
    assert facts['binding'] in ('disk', 'memory')
    assert facts['may_deploy_plain'] is False
    assert facts['per_instance_peak_gb'] > 1


def test_the_facts_say_a_plain_deployment_is_still_available(roomy):
    facts = atlas.capacity_facts(Ctx(), size_gb=50)

    assert facts['may_deploy_plain'] is True


def test_memory_is_read_as_available_not_free(tmp_path):
    """⚠️ **`MemAvailable`, not `MemFree`.** On a box running eight stacks free
    memory is near zero because the page cache holds the rest — planning against
    it would refuse every instance the box could comfortably run. These are the
    real proportions from the measured box: 32 GB total, 19.7 GB free, 20.5 GB
    available."""
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text(chr(10).join([
        'MemTotal:       32862208 kB',
        'MemFree:        20193280 kB',
        'MemAvailable:   21027840 kB',
        'Buffers:          102400 kB',
        '',
    ]), encoding='utf-8')

    total, available = atlas._memory_bytes(str(meminfo))

    assert total == 32862208 * 1024
    assert available == 21027840 * 1024, 'planning read MemFree, not MemAvailable'


def test_unreadable_memory_is_zero_rather_than_a_guess(tmp_path):
    """A fabricated figure here would offer instances the box cannot run."""
    assert atlas._memory_bytes(str(tmp_path / 'absent')) == (0, 0)
