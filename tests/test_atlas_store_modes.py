"""Fixed and dynamic sizing (W216, chunk 2).

Two modes with one code path and a handful of differences, each of which is the
difference between a reservation and a ceiling:

* `fallocate` vs `truncate` when the image is created;
* `-E nodiscard` present or absent, so `mkfs` either keeps the blocks or hands
  them back;
* `discard` in the mount options, without which a dynamic store can only grow;
* `_reserve_blocks` running or not, because running it on a dynamic store would
  silently convert it into a fixed one;
* and **the second gauge**, without which a dynamic store reports free space the
  host may be unable to deliver.

⚠️ The last is the one that must not ship missing. A filesystem that is only a
ceiling will say "3.3 GB available" with an empty box underneath it, which is
exactly the class of authoritative-looking wrong number W213 removed.

Pure: the argv, the options and the arithmetic. The behaviour they produce was
measured on a real box and the numbers live in the docstrings.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modules.atlas as atlas  # noqa: E402
from modules.atlas_instances import MODE_DYNAMIC, MODE_FIXED  # noqa: E402

GIB = 1024 ** 3


# --------------------------------------------------------------------------- #
# mkfs: keep the blocks, or hand them back
# --------------------------------------------------------------------------- #


def test_fixed_keeps_the_blocks_mkfs_would_discard():
    """W213's fix, still in force for the mode it was written for."""
    argv = atlas.mkfs_argv('/x.img', mode=MODE_FIXED)

    assert '-E' in argv and argv[argv.index('-E') + 1] == 'nodiscard'


def test_dynamic_lets_mkfs_discard_on_purpose():
    """⚠️ The inverse of W213, and correct here. A dynamic image is sparse by
    design: the filesystem is a ceiling and the blocks belong to the box until
    this agency uses them."""
    argv = atlas.mkfs_argv('/x.img', mode=MODE_DYNAMIC)

    assert 'nodiscard' not in argv


def test_the_default_mode_is_fixed():
    """⚠️ Every existing call site omits the mode, and must keep reserving.
    Defaulting to dynamic would silently un-reserve every deployed store."""
    assert atlas.mkfs_argv('/x.img') == atlas.mkfs_argv('/x.img', mode=MODE_FIXED)
    assert 'nodiscard' in atlas.mkfs_argv('/x.img')


@pytest.mark.parametrize("mode", [MODE_FIXED, MODE_DYNAMIC])
def test_both_modes_keep_the_root_reserve_off_and_ask_no_questions(mode):
    argv = atlas.mkfs_argv('/x.img', mode=mode)

    assert argv[argv.index('-m') + 1] == '0'
    assert '-F' in argv
    assert argv[-1] == '/x.img'


# --------------------------------------------------------------------------- #
# Mount options: the half that makes shrink work
# --------------------------------------------------------------------------- #


def test_dynamic_mounts_with_discard():
    """⚠️ Without `discard` a dynamic store only ever grows: deleting 40 GB of
    imagery would free it inside the agency's filesystem and hand nothing back
    to the box, defeating the whole mode."""
    assert 'discard' in atlas.mount_options(MODE_DYNAMIC)


def test_dynamic_stops_loudly_on_an_io_error():
    """⚠️ A dynamic store can take an I/O error rather than a clean ENOSPC,
    because its filesystem believes in space the host cannot deliver. The live
    fixed store measures `Errors behavior: Continue` — carrying on, which for a
    database is the worst option."""
    assert 'errors=remount-ro' in atlas.mount_options(MODE_DYNAMIC)


def test_fixed_mounts_as_it_always_has():
    """⚠️ Pinned: the deployed box mounts `loop` and nothing else. Adding
    options here would rewrite a live deployment's mount unit on next deploy."""
    assert atlas.mount_options(MODE_FIXED) == 'loop'
    assert atlas.mount_options() == 'loop'


def test_fixed_does_not_discard():
    """It has nothing to hand back — its blocks are the reservation."""
    assert 'discard' not in atlas.mount_options(MODE_FIXED)


# --------------------------------------------------------------------------- #
# The second gauge
# --------------------------------------------------------------------------- #


def test_a_fixed_store_trusts_its_own_filesystem():
    """The blocks are already allocated, so the filesystem's answer *is* the
    truth and the host's free space cannot reduce it."""
    assert atlas.deliverable_free(50 * GIB, 1 * GIB, MODE_FIXED) == 50 * GIB


def test_a_dynamic_store_reports_the_smaller_of_the_two():
    """⚠️ **The number that would otherwise lie.** The agency's filesystem says
    50 GB free; the box has 8 GB to give. 8 is the honest answer."""
    assert atlas.deliverable_free(50 * GIB, 8 * GIB, MODE_DYNAMIC) == 8 * GIB


def test_a_dynamic_store_is_still_capped_by_its_own_ceiling():
    """The other direction: the box is roomy, the agency is nearly full."""
    assert atlas.deliverable_free(2 * GIB, 200 * GIB, MODE_DYNAMIC) == 2 * GIB


def test_neither_gauge_goes_negative():
    """A negative remainder would draw a bar pointing the wrong way — the same
    guard the ATLAS-side meter already carries."""
    assert atlas.deliverable_free(-5, 10, MODE_DYNAMIC) == 0
    assert atlas.deliverable_free(10, -5, MODE_DYNAMIC) == 0
    assert atlas.deliverable_free(-5, -5, MODE_FIXED) == 0


def test_the_facts_carry_the_mode_and_say_whether_space_is_held():
    """⚠️ `size_is_reserved` exists so no caller can render a ceiling as though
    it were held space. One field, two meanings, was the W213 shape of bug."""
    facts = atlas.store_facts(None, mode=MODE_DYNAMIC)

    assert facts['mode'] == MODE_DYNAMIC
    assert facts['size_is_reserved'] is False


def test_the_facts_default_to_fixed():
    facts = atlas.store_facts(None)

    assert facts['mode'] == MODE_FIXED
    assert facts['size_is_reserved'] is True


def test_the_facts_always_offer_the_deliverable_figure(monkeypatch):
    """Present in both modes, so the page never has to ask which one it is
    looking at before it can draw a bar."""
    monkeypatch.setattr(atlas, '_disk_free', lambda _p: (500 * GIB, 400 * GIB))

    for mode in (MODE_FIXED, MODE_DYNAMIC):
        assert 'deliverable_gb' in atlas.store_facts(None, mode=mode)


# --------------------------------------------------------------------------- #
# ensure_store: what each mode actually runs
# --------------------------------------------------------------------------- #


class Runner:
    """Records the root commands, and pretends they all succeed."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=120):
        self.calls.append(list(argv))
        if argv[0] == 'systemd-escape':
            return 0, 'root-atlas-store.mount'
        return 0, ''

    def ran(self, *fragments):
        return [c for c in self.calls
                if all(f in ' '.join(c) for f in fragments)]


@pytest.fixture
def store(monkeypatch, tmp_path):
    """`ensure_store` against a temporary image, with root commands recorded."""
    base = tmp_path / 'atlas'
    (base / 'store').mkdir(parents=True)
    image = tmp_path / 'var' / 'store.img'
    image.parent.mkdir(parents=True)

    runner = Runner()
    monkeypatch.setattr(atlas, 'STORE_IMAGE', str(image))
    monkeypatch.setattr(atlas, 'atlas_dir', lambda _c: str(base))
    monkeypatch.setattr(atlas, '_run_root', runner)
    monkeypatch.setattr(atlas, '_bind_unit', lambda *a: None)
    monkeypatch.setattr(atlas, '_bind_pg_volume', lambda *a: None)
    monkeypatch.setattr(atlas.os, 'chown', lambda *a: None, raising=False)
    monkeypatch.setattr(atlas.os, 'chmod', lambda *a: None)
    monkeypatch.setattr(atlas.os.path, 'ismount', lambda _p: True)
    return runner


def test_fixed_allocates_the_blocks_up_front(store):
    err = atlas.ensure_store({}, 4 * GIB, lambda *_: None, mode=MODE_FIXED)

    assert err is None
    assert store.ran('fallocate'), 'a fixed store did not allocate its blocks'
    assert not store.ran('truncate')


def test_dynamic_creates_a_sparse_ceiling(store):
    """⚠️ `truncate`, not `fallocate` — the space stays with the box until this
    agency uses it, which is the entire mode."""
    err = atlas.ensure_store({}, 4 * GIB, lambda *_: None, mode=MODE_DYNAMIC)

    assert err is None
    assert store.ran('truncate'), 'a dynamic store pre-allocated its space'
    assert not store.ran('fallocate')


def test_dynamic_never_reserves_its_blocks_afterwards(monkeypatch, store):
    """⚠️ **The silent-conversion guard.** `_reserve_blocks` fills every hole in
    the image. Running it on a dynamic store would turn it into a fixed one
    while the operator believed they had chosen dynamic."""
    called = []
    monkeypatch.setattr(atlas, '_reserve_blocks',
                        lambda size, plog: called.append(size) or None)

    atlas.ensure_store({}, 4 * GIB, lambda *_: None, mode=MODE_DYNAMIC)

    assert called == [], 'a dynamic store had its blocks reserved'


def test_fixed_still_reserves_its_blocks(monkeypatch, store):
    called = []
    monkeypatch.setattr(atlas, '_reserve_blocks',
                        lambda size, plog: called.append(size) or None)

    atlas.ensure_store({}, 4 * GIB, lambda *_: None, mode=MODE_FIXED)

    assert called == [4 * GIB]


def test_the_default_keeps_existing_deployments_reserving(monkeypatch, store):
    """⚠️ Omitting the mode must behave exactly as before this chunk."""
    called = []
    monkeypatch.setattr(atlas, '_reserve_blocks',
                        lambda size, plog: called.append(size) or None)

    atlas.ensure_store({}, 4 * GIB, lambda *_: None)

    assert called == [4 * GIB]
    assert store.ran('fallocate')


@pytest.mark.parametrize(
    "mode,expected",
    [(MODE_FIXED, "loop"), (MODE_DYNAMIC, "loop,discard,errors=remount-ro")],
)
def test_the_mount_unit_is_written_with_the_mode_s_options(
    monkeypatch, store, mode, expected
):
    """⚠️ The options only take effect if they reach the *unit file* — the
    store is mounted by systemd at boot, not by this code. Writing `loop` into a
    dynamic store's unit would leave it unable to shrink after every reboot,
    which is the kind of failure that looks like it worked on the day."""
    written = []
    monkeypatch.setattr(atlas.os.path, "ismount", lambda _p: False)
    monkeypatch.setattr(
        atlas, "_write_mount_unit",
        lambda what, where, plog, options=None: written.append(options) or None,
    )

    atlas.ensure_store({}, 4 * GIB, lambda *_: None, mode=mode)

    assert written == [expected]


# ⚠️ **Byte-scale sizes on purpose.** An earlier test in this project faked a
# large image with `truncate(100 * GIB)` — sparse on ext4, fully allocated on the
# NTFS these tests run on — and wrote 145 GB before it was killed. Nothing here
# needs the numbers to be realistic; the code path is the same at 1000 bytes.


@pytest.mark.parametrize(
    "mode,grows_with,not_with",
    [(MODE_FIXED, "fallocate", "truncate"), (MODE_DYNAMIC, "truncate", "fallocate")],
)
def test_growth_follows_the_mode(monkeypatch, store, tmp_path, mode,
                                 grows_with, not_with):
    """⚠️ **The silent-conversion bug on the resize path.** Growing a dynamic
    store with `fallocate` would allocate the whole new ceiling at once, turning
    it into a fixed store while the operator believed it was still dynamic —
    and the space would vanish from the pool the moment someone raised a cap.
    """
    image = pathlib.Path(atlas.STORE_IMAGE)
    image.write_bytes(bytes(1000))

    err = atlas.ensure_store({}, 2000, lambda *_: None, mode=mode)

    assert err is None
    assert store.ran(grows_with, "2000"), f"{mode} did not grow with {grows_with}"
    assert not store.ran(not_with, "2000")


def test_growth_resizes_the_filesystem_in_both_modes(store):
    """Raising the ceiling is only half of it — ext4 has to be told."""
    pathlib.Path(atlas.STORE_IMAGE).write_bytes(bytes(1000))

    atlas.ensure_store({}, 2000, lambda *_: None, mode=MODE_DYNAMIC)

    assert store.ran("resize2fs")


def test_shrinking_is_still_refused_in_both_modes(store):
    """⚠️ `resize2fs` cannot shrink a mounted filesystem, so neither mode
    supports it. Dynamic shrinks its *disk usage* through `discard`; its ceiling
    does not move."""
    pathlib.Path(atlas.STORE_IMAGE).write_bytes(bytes(2000))

    for mode in (MODE_FIXED, MODE_DYNAMIC):
        err = atlas.ensure_store({}, 1000, lambda *_: None, mode=mode)
        assert err and "shrink" in err


def test_each_mode_makes_its_filesystem_the_matching_way(store):
    atlas.ensure_store({}, 4 * GIB, lambda *_: None, mode=MODE_DYNAMIC)

    made = store.ran('mkfs.ext4')

    assert made, 'no filesystem was created'
    assert 'nodiscard' not in ' '.join(made[0])
