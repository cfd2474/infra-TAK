"""Whether the reservation actually reserves (W213).

⚠️ **The bug these exist for, and why the existing tests missed it.** W205 built
the store with `fallocate` — deliberately, over `truncate`, with a comment saying
*"a sparse file reserves nothing"* — and then ran `mkfs.ext4`, which discards its
target. On a regular file on ext4 a discard is `FALLOC_FL_PUNCH_HOLE`, so `mkfs`
handed every reserved block straight back. The reservation reserved nothing for
its whole first release while the console reported that it had.

`test_atlas_store_sizing.py` says in its own docstring that it excludes
"creating the image, making the filesystem, mounting it" — which is precisely
where this lived. So the point of this file is to pull the decidable parts *out*
of that gap: the command that gets run, and the arithmetic that judges the
result. Both are checkable without a disk.

⚠️ What still cannot be checked here: that `-E nodiscard` has the effect claimed.
That needs a real ext4, and it is measured on the box instead — 5 GB fallocated
then `mkfs.ext4` → 67 MiB of real blocks, with `-E nodiscard` → 5121 MiB. The
numbers are recorded in `mkfs_argv`'s docstring so the next person does not have
to rediscover them.

⚠️ `os.stat_result` has no `st_blocks` on Windows, where these run. That is why
the arithmetic takes plain numbers and `allocated_bytes` returns `None` rather
than `0` when it cannot tell — otherwise this file could not exist on the
development machine at all.
"""

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    sys.path.insert(0, str(ROOT))
    import modules.atlas as module

    return module


atlas = _load_module()

GIB = 1024 ** 3


# --------------------------------------------------------------------------- #
# The command that was wrong
# --------------------------------------------------------------------------- #


def test_mkfs_does_not_discard_the_blocks_just_reserved():
    """⚠️ **The fix, and the one assertion that would have caught the bug.**
    Without `-E nodiscard`, `mkfs` punches holes through the whole image."""
    argv = atlas.mkfs_argv('/var/lib/atlas/store.img')

    assert '-E' in argv
    assert argv[argv.index('-E') + 1] == 'nodiscard'


def test_mkfs_keeps_no_root_reserve():
    """`-m 0`: ext4's default 5% is space an operator asked for and cannot use."""
    argv = atlas.mkfs_argv('/var/lib/atlas/store.img')

    assert argv[argv.index('-m') + 1] == '0'


def test_mkfs_targets_the_image_it_was_given():
    argv = atlas.mkfs_argv('/somewhere/else.img')

    assert argv[-1] == '/somewhere/else.img'
    assert argv[0] == 'mkfs.ext4'


def test_mkfs_labels_the_filesystem():
    argv = atlas.mkfs_argv('/var/lib/atlas/store.img')

    assert argv[argv.index('-L') + 1] == 'atlas-store'


def test_mkfs_does_not_ask_a_question_it_cannot_answer():
    """`-F` — there is no terminal on a deploy to confirm at."""
    assert '-F' in atlas.mkfs_argv('/var/lib/atlas/store.img')


# --------------------------------------------------------------------------- #
# Judging the result
# --------------------------------------------------------------------------- #


def test_a_fully_allocated_image_is_reserved():
    assert atlas.is_fully_reserved(100 * GIB, 100 * GIB) is True


def test_an_image_slightly_over_its_apparent_size_is_reserved():
    """⚠️ Measured, not hypothetical: a correctly built 2 GB image reports 2049
    MiB of blocks against 2048 MiB apparent, because ext4's own structures are
    allocated too. A test demanding equality would fail on a working store."""
    assert atlas.is_fully_reserved(2048 * 1024 ** 2, 2049 * 1024 ** 2) is True


def test_a_sparse_image_is_not_reserved():
    """The real numbers off the box: 98 GiB apparent, ~2 GiB backed."""
    assert atlas.is_fully_reserved(98 * GIB, 2 * GIB) is False


def test_the_exact_case_that_shipped():
    """5 GB fallocated then mkfs'd without `-E nodiscard` → 67 MiB."""
    assert atlas.is_fully_reserved(5 * GIB, 67 * 1024 ** 2) is False


def test_a_reservation_that_cannot_be_measured_is_not_claimed():
    """⚠️ **`None` is not zero and neither is "fine".** Reporting a reservation
    that cannot be demonstrated is the bug itself, in a smaller form."""
    assert atlas.is_fully_reserved(100 * GIB, None) is False


def test_nothing_reserved_at_all_is_not_reserved():
    assert atlas.is_fully_reserved(0, 0) is False
    assert atlas.is_fully_reserved(100 * GIB, 0) is False


def test_the_threshold_tolerates_rounding_but_not_a_hole():
    apparent = 100 * GIB

    assert atlas.is_fully_reserved(apparent, int(apparent * 0.96)) is True
    assert atlas.is_fully_reserved(apparent, int(apparent * 0.5)) is False


# --------------------------------------------------------------------------- #
# Measuring it
# --------------------------------------------------------------------------- #


def test_a_missing_image_measures_as_unknown_not_as_empty(tmp_path):
    """⚠️ Unknown, not zero — and the distinction matters, because `0` would
    send the repair path into declaring a real failure."""
    assert atlas.allocated_bytes(str(tmp_path / "absent.img")) is None


def test_an_unreadable_path_measures_as_unknown():
    """`os.stat` raises `ValueError`, not `OSError`, on an embedded NUL — found
    the hard way twice on this project."""
    assert atlas.allocated_bytes("\x00") is None


#: Whether this platform can answer the question at all. Windows cannot.
HAS_ST_BLOCKS = hasattr(os.stat(__file__), "st_blocks")


@pytest.mark.skipif(not HAS_ST_BLOCKS, reason="no st_blocks on this platform")
def test_a_real_file_measures_its_own_blocks(tmp_path):
    """Only where the platform can answer. On Windows this is skipped rather
    than faked, because a faked measurement is what this whole item is about."""
    target = tmp_path / "some.bin"
    target.write_bytes(b"x" * 200_000)

    measured = atlas.allocated_bytes(str(target))

    assert measured is not None
    assert measured >= 200_000


# --------------------------------------------------------------------------- #
# What the console is told
# --------------------------------------------------------------------------- #


class FakeStat:
    """Only the field `store_facts` reads."""

    def __init__(self, size):
        self.st_size = size


@pytest.fixture
def facts(monkeypatch, tmp_path):
    """`store_facts` against an image whose apparent and real sizes we set.

    ⚠️ **No real image is created here, and that is not laziness.** The first
    version did `truncate(100 * GIB)` to fake a sparse file — which is sparse on
    ext4 and *fully allocated* on NTFS, where these tests run. It wrote 145 GB
    into pytest's temp directory before the run was killed. `store_facts` only
    ever reads `os.path.exists` and `st_size`, so those are what get faked;
    inventing a real 100 GB file to test a size calculation was never necessary.

    Both fakes delegate for every other path, because patching `os.stat`
    wholesale would break pytest's own bookkeeping mid-test.
    """
    image = str(tmp_path / "store.img")
    monkeypatch.setattr(atlas, "STORE_IMAGE", image)
    monkeypatch.setattr(atlas, "_disk_free", lambda _p: (500 * GIB, 400 * GIB))
    real_stat, real_exists = os.stat, os.path.exists

    def build(apparent, allocated):
        monkeypatch.setattr(
            atlas.os, "stat",
            lambda p, *a, **k: (FakeStat(apparent) if str(p) == image
                                else real_stat(p, *a, **k)),
        )
        monkeypatch.setattr(
            atlas.os.path, "exists",
            lambda p: True if str(p) == image else real_exists(p),
        )
        monkeypatch.setattr(atlas, "allocated_bytes", lambda _p: allocated)
        return atlas.store_facts(None)

    return build


def test_the_console_is_told_what_is_actually_allocated(facts):
    """⚠️ **The number that lied.** `reserved_gb` came from `st_size`, so a
    sparse 100 GB image reported "100 GB reserved" while holding 2 GB — on a
    panel whose docstring promises every figure is measured."""
    f = facts(100 * GIB, 2 * GIB)

    assert f["reserved_gb"] == 100.0, "the requested size is still worth showing"
    assert f["allocated_gb"] == 2.0
    assert f["fully_reserved"] is False


def test_a_real_reservation_reports_itself_as_one(facts):
    f = facts(100 * GIB, 100 * GIB)

    assert f["allocated_gb"] == 100.0
    assert f["fully_reserved"] is True


def test_no_image_reports_nothing_reserved(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas, "STORE_IMAGE", str(tmp_path / "absent.img"))
    monkeypatch.setattr(atlas, "_disk_free", lambda _p: (500 * GIB, 400 * GIB))

    f = atlas.store_facts(None)

    assert f["reserved_gb"] == 0
    assert f["allocated_gb"] == 0
    assert f["fully_reserved"] is False


# --------------------------------------------------------------------------- #
# The repair path
# --------------------------------------------------------------------------- #


class Fallocate:
    """Records `fallocate` calls and can pretend the image fills up."""

    def __init__(self, after=None, rc=0):
        self.calls = []
        self.after = after
        self.rc = rc

    def __call__(self, argv, timeout=120):
        self.calls.append(list(argv))
        return self.rc, "" if self.rc == 0 else "pretend fallocate failure"


@pytest.fixture
def repair(monkeypatch):
    """`_reserve_blocks` with a controllable allocation reading."""
    def run(size, before, after, rc=0):
        runner = Fallocate(rc=rc)
        readings = iter([before, after])
        monkeypatch.setattr(atlas, "_run_root", runner)
        monkeypatch.setattr(
            atlas, "allocated_bytes",
            lambda _p: next(readings, after),
        )
        err = atlas._reserve_blocks(size, lambda *_: None)
        return err, runner

    return run


def test_a_sparse_store_is_repaired(repair):
    """⚠️ Every store created before this fix is sparse, so the repair is the
    part that matters for boxes that already exist."""
    err, runner = repair(100 * GIB, 2 * GIB, 100 * GIB)

    assert err is None
    assert runner.calls, "a sparse store was not repaired"
    assert runner.calls[0][0] == "fallocate"
    assert str(100 * GIB) in runner.calls[0]


def test_a_store_already_holding_its_blocks_is_left_alone(repair):
    """Idempotent: a deploy at the size already reserved must not rewrite it."""
    err, runner = repair(100 * GIB, 100 * GIB, 100 * GIB)

    assert err is None
    assert runner.calls == [], "re-reserved a store that was already reserved"


def test_a_failed_fallocate_is_an_error(repair):
    err, _ = repair(100 * GIB, 2 * GIB, 2 * GIB, rc=1)

    assert err is not None
    assert "fallocate" in err


def test_a_repair_that_did_not_take_is_an_error(repair):
    """⚠️ Verify *after* the call. `fallocate` returning 0 while the blocks are
    still missing is exactly the shape of the original bug: a command that
    succeeded and an outcome that did not."""
    err, _ = repair(100 * GIB, 2 * GIB, 3 * GIB)

    assert err is not None
    assert "still holds only" in err


def test_ensure_store_reserves_the_blocks(monkeypatch, tmp_path):
    """⚠️ **That the call exists at all.** W212's bug was a function that was
    written and never called, and W213's was a command that undid the one before
    it — both invisible to unit tests of the pieces. So this asserts the wiring:
    a deploy over an existing store must ask for the blocks, because every store
    made before this fix is sparse and nothing else would ever repair it.
    """
    base = tmp_path / "atlas"
    (base / "store").mkdir(parents=True)
    image = tmp_path / "store.img"
    image.write_bytes(b"x" * 64)

    asked = []
    monkeypatch.setattr(atlas, "STORE_IMAGE", str(image))
    monkeypatch.setattr(atlas, "atlas_dir", lambda _c: str(base))
    monkeypatch.setattr(atlas, "_run_root", lambda *a, **k: (0, ""))
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    monkeypatch.setattr(atlas.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setattr(atlas.os, "chmod", lambda *a: None)
    monkeypatch.setattr(atlas, "_bind_unit", lambda *a: None)
    monkeypatch.setattr(atlas, "_bind_pg_volume", lambda *a: None)
    monkeypatch.setattr(
        atlas, "_reserve_blocks",
        lambda size, plog: asked.append(size) or None,
    )

    err = atlas.ensure_store({}, 64, lambda *_: None)

    assert err is None
    assert asked == [64], "ensure_store did not ask for the blocks"


def test_an_unmeasurable_platform_does_not_fail_the_deploy(monkeypatch):
    """⚠️ Windows and anything else without `st_blocks`. The fallocate ran and
    succeeded; claiming failure because the result cannot be read would block a
    deploy over a measurement problem."""
    runner = Fallocate()
    monkeypatch.setattr(atlas, "_run_root", runner)
    monkeypatch.setattr(atlas, "allocated_bytes", lambda _p: None)

    err = atlas._reserve_blocks(100 * GIB, lambda *_: None)

    assert err is None
    assert runner.calls, "never attempted the reservation"
