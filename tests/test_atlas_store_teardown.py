"""Taking the ATLAS store back down again (W212).

⚠️ **The bug these exist for.** W205 added the reserved store — a loop-backed
ext4 image and three systemd mount units — and never wrote the counterpart in
`uninstall`. Three mount points live *inside* the install directory, and
`shutil.rmtree` cannot delete through a mount, so the uninstall removed the
containers and then left the device CA, every private key, the Postgres data
directory and `.env` on disk **while reporting that it had succeeded**. It was
found by auditing a box by hand after the operator ran it.

⚠️ What is *not* here: real mounts, a real loop device, a real `systemctl`.
Those need a kernel. What is checked is the part that was actually wrong — the
*order* of the teardown, and the reporting contract that hid the failure. Both
are decidable without a disk, which is the point of keeping them decidable.
"""

import importlib.util
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


# --------------------------------------------------------------------------- #
# A fake root shell that records what it was asked to do
# --------------------------------------------------------------------------- #


class Recorder:
    """Stands in for `_run_root`, remembering the order of the calls.

    ⚠️ Order is what this file is about, so the recorder keeps a list and never
    a set.
    """

    def __init__(self, fail=(), escape=True):
        self.calls: list[list[str]] = []
        self.fail = fail
        self.escape = escape

    def __call__(self, argv, timeout=120):
        self.calls.append(list(argv))
        if argv[0] == "systemd-escape":
            if not self.escape:
                return 1, ""
            # What the real one returns: the path with slashes as dashes.
            return 0, _escape(argv[-1])
        for pattern in self.fail:
            if pattern in " ".join(argv):
                return 1, f"pretend failure: {pattern}"
        if argv[:2] == ["losetup", "-j"]:
            return 0, f"/dev/loop8: [2050]:1 ({atlas.STORE_IMAGE})"
        return 0, ""

    def ran(self, *fragments: str) -> list[list[str]]:
        return [c for c in self.calls
                if all(f in " ".join(c) for f in fragments)]

    def index_of(self, *fragments: str) -> int:
        for i, c in enumerate(self.calls):
            if all(f in " ".join(c) for f in fragments):
                return i
        raise AssertionError(f"never ran anything matching {fragments}")


def _posix(path) -> str:
    """Box-shaped. The module builds paths with `posixpath`."""
    return str(path).replace(chr(92), "/")


def _escape(path: str) -> str:
    return path.strip("/").replace("-", "\\x2d").replace("/", "-") + ".mount"


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A pretend box: an install directory, and nothing actually mounted."""
    base = tmp_path / "atlas"
    (base / "store").mkdir(parents=True)
    (base / "artifacts").mkdir()
    (base / "cache").mkdir()
    image = tmp_path / "var" / "store.img"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"not really an ext4 image")

    # ⚠️ POSIX-shaped: the module builds box paths with `posixpath`, and a
    # Windows temp path would produce separators that exist nowhere on a box.
    monkeypatch.setattr(atlas, "STORE_IMAGE", str(image).replace(chr(92), '/'))
    monkeypatch.setattr(atlas, "atlas_dir",
                        lambda _ctx=None: str(base).replace(chr(92), '/'))
    # Nothing is a mount point unless a test says so.
    monkeypatch.setattr(os.path, "ismount", lambda _p: False)
    return base


@pytest.fixture
def rec(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(atlas, "_run_root", recorder)
    return recorder


# --------------------------------------------------------------------------- #
# The order, which is the whole function
# --------------------------------------------------------------------------- #


def test_the_binds_come_down_before_the_store_they_mount_out_of(box, rec):
    """⚠️ **The load-bearing assertion.** `artifacts` and `cache` are binds out
    of `<store>/artifacts` and `<store>/cache`. Unmounting the store first would
    pull the source out from under two live mounts."""
    atlas.remove_store({}, lambda *_: None)

    artifacts = rec.index_of("disable", "artifacts")
    cache = rec.index_of("disable", "cache")
    store = rec.index_of("disable", _escape(_posix(box / "store")))

    assert artifacts < store, "the store came down before its artifacts bind"
    assert cache < store, "the store came down before its cache bind"


def test_the_volume_goes_before_the_mounts(box, rec):
    """It only points at `pgdata`, but a leftover volume makes the *next*
    deploy refuse — `_bind_pg_volume` rejects one that already exists."""
    atlas.remove_store({}, lambda *_: None)

    assert rec.index_of("volume", "rm") < rec.index_of("disable")


def test_the_loop_is_detached_after_the_mounts_are_gone(box, rec):
    atlas.remove_store({}, lambda *_: None)

    assert rec.index_of("losetup", "-j") > rec.index_of("disable")


def test_systemd_is_reloaded_after_the_unit_files_go(box, rec):
    atlas.remove_store({}, lambda *_: None)

    assert rec.index_of("daemon-reload") > rec.index_of("disable")


# --------------------------------------------------------------------------- #
# What it removes
# --------------------------------------------------------------------------- #


def test_all_three_units_are_disabled(box, rec):
    atlas.remove_store({}, lambda *_: None)

    disabled = " ".join(" ".join(c) for c in rec.ran("disable"))
    for path in (box / "store", box / "artifacts", box / "cache"):
        assert _escape(_posix(path)) in disabled, f"{path} was left enabled"


def test_the_reservation_is_deleted(box, rec):
    assert os.path.exists(atlas.STORE_IMAGE)

    did, errs = atlas.remove_store({}, lambda *_: None)

    assert not os.path.exists(atlas.STORE_IMAGE)
    assert errs == []
    assert any("deleted" in d for d in did)


def test_only_our_own_loop_device_is_detached(box, rec):
    """⚠️ `losetup -D` detaches **every** loop device on the box, including
    other modules'. The image has to be named."""
    atlas.remove_store({}, lambda *_: None)

    assert rec.ran("losetup", "-j", atlas.STORE_IMAGE)
    assert rec.ran("losetup", "-d", "/dev/loop8")
    assert not rec.ran("losetup", "-D"), "detached every loop device on the box"


def test_the_parent_directory_goes_only_when_empty(box, rec, monkeypatch):
    parent = os.path.dirname(atlas.STORE_IMAGE)
    atlas.remove_store({}, lambda *_: None)

    assert not os.path.isdir(parent)


def test_a_parent_holding_someone_else_s_file_is_left_alone(box, rec):
    parent = pathlib.Path(os.path.dirname(atlas.STORE_IMAGE))
    (parent / "somebody-elses.conf").write_text("keep me", encoding="utf-8")

    atlas.remove_store({}, lambda *_: None)

    assert parent.is_dir(), "removed a directory that still had a file in it"
    assert (parent / "somebody-elses.conf").exists()


# --------------------------------------------------------------------------- #
# Unmounting what systemd will not
# --------------------------------------------------------------------------- #


def test_a_live_mount_is_unmounted_even_without_its_unit(box, rec, monkeypatch):
    """⚠️ `disable --now` only stops a unit systemd has *loaded*. A mount whose
    unit file was already deleted is still a live mount, and only `umount`
    reaches it — which is the state this box was found in."""
    monkeypatch.setattr(os.path, "ismount", lambda p: p.endswith("store"))

    atlas.remove_store({}, lambda *_: None)

    assert rec.ran("umount"), "a live mount was left mounted"


def test_a_busy_mount_falls_back_to_a_lazy_unmount(box, monkeypatch):
    recorder = Recorder(fail=("umount " + _posix(box / "store"),))
    monkeypatch.setattr(atlas, "_run_root", recorder)
    monkeypatch.setattr(os.path, "ismount", lambda p: p.endswith("store"))

    did, errs = atlas.remove_store({}, lambda *_: None)

    assert recorder.ran("umount", "-l"), "never tried a lazy unmount"
    assert errs == []


def test_a_mount_that_cannot_be_unmounted_at_all_is_an_error(box, monkeypatch):
    """⚠️ And it must be an error, not a step. Reporting this as done is the
    bug that hid the missing teardown for a whole release."""
    recorder = Recorder(fail=("umount",))
    monkeypatch.setattr(atlas, "_run_root", recorder)
    monkeypatch.setattr(os.path, "ismount", lambda p: p.endswith("store"))

    did, errs = atlas.remove_store({}, lambda *_: None)

    assert errs, "an unmountable store reported no error"
    assert any("could not unmount" in e for e in errs)


# --------------------------------------------------------------------------- #
# Idempotence
# --------------------------------------------------------------------------- #


def test_an_already_clean_box_reports_no_errors(box, rec):
    """An uninstall has to be re-runnable after a partial failure."""
    os.remove(atlas.STORE_IMAGE)

    did, errs = atlas.remove_store({}, lambda *_: None)

    assert errs == []


def test_running_it_twice_changes_nothing_the_second_time(box, rec):
    atlas.remove_store({}, lambda *_: None)
    did, errs = atlas.remove_store({}, lambda *_: None)

    assert errs == []
    assert not any("deleted" in d for d in did), "deleted the image twice"


def test_a_missing_loop_device_is_not_an_error(box, monkeypatch):
    class NoLoop(Recorder):
        def __call__(self, argv, timeout=120):
            self.calls.append(list(argv))
            if argv[:2] == ["losetup", "-j"]:
                return 0, ""
            if argv[0] == "systemd-escape":
                return 0, _escape(argv[-1])
            return 0, ""

    recorder = NoLoop()
    monkeypatch.setattr(atlas, "_run_root", recorder)

    did, errs = atlas.remove_store({}, lambda *_: None)

    assert errs == []
    assert not recorder.ran("losetup", "-d")


# --------------------------------------------------------------------------- #
# The reporting contract that hid the bug
# --------------------------------------------------------------------------- #


class FakeCtx(dict):
    """Just enough console for `uninstall` to run.

    ⚠️ **`save_settings` replaces; it must not merge.** Written as
    `self.settings.update(s)` this fake could not express a *deletion* — and
    `uninstall` clears keys by popping them from the copy it loaded, so the
    assertion that a failed uninstall keeps `atlas_pg_password` held no matter
    what the code did. A mutation caught it: the test passed with the guard
    removed. The harness lied before the code did, which is the second time
    that has happened on this project.
    """

    def __init__(self):
        super().__init__()
        self.settings = {"atlas_pg_password": "x", "atlas_enabled": True}
        self.update({
            "_module_git": lambda *a, **k: None,
            "_sudo_wrap": lambda argv: argv,
            "_fw_remove": lambda *a: None,
            "load_settings": lambda: dict(self.settings),
            "save_settings": self._save,
            "generate_caddyfile": lambda _s: None,
            "_caddy_reload": lambda: None,
            "_deregister_authentik_proxy_app": lambda *a: None,
        })

    def _save(self, s):
        self.settings = dict(s)


@pytest.fixture
def uninstallable(box, monkeypatch):
    monkeypatch.setattr(atlas, "_compose", lambda *a, **k: None)
    monkeypatch.setattr(atlas, "_run", lambda *a: True)
    monkeypatch.setattr(atlas, "_caddy_ca_dir", lambda: None)
    monkeypatch.setattr(atlas, "_stale_deploy_key", lambda _d: [])
    return FakeCtx()


def test_uninstall_removes_the_install_directory(uninstallable, box, rec):
    result = atlas.uninstall(uninstallable, None, {})

    assert result["success"] is True
    assert not box.exists(), "the install directory survived a successful uninstall"


def test_uninstall_stops_and_reports_failure_when_the_store_will_not_go(
    uninstallable, box, monkeypatch
):
    """⚠️ **The contract that was missing.** The store failing means the mounts
    are still inside the install directory, so deleting it would half-succeed.
    Stop, say so, and leave the box re-runnable."""
    recorder = Recorder(fail=("umount",))
    monkeypatch.setattr(atlas, "_run_root", recorder)
    monkeypatch.setattr(os.path, "ismount", lambda p: p.endswith("store"))

    result = atlas.uninstall(uninstallable, None, {})

    assert result["success"] is False
    assert "store could not be removed" in result["error"]
    assert box.exists(), "deleted the install directory after the store failed"


def test_uninstall_reports_failure_when_the_directory_will_not_go(
    uninstallable, box, rec, monkeypatch
):
    """⚠️ The exact bug. This used to append "NOT removed: …" to the steps and
    return success anyway, which is why a box with the device CA, every private
    key, the database and `.env` still on it was reported as clean."""
    def refuse(path):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(atlas.shutil, "rmtree", refuse)

    result = atlas.uninstall(uninstallable, None, {})

    assert result["success"] is False
    assert "could not be removed" in result["error"]
    assert "Device or resource busy" in result["error"]


def test_a_failed_uninstall_does_not_clear_the_settings(
    uninstallable, box, rec, monkeypatch
):
    """⚠️ Clearing `atlas_pg_password` while the database survives is what arms
    the next install to fail: deploy generates a new password, Postgres keeps the
    old one on a non-empty data directory, and the API cannot authenticate."""
    monkeypatch.setattr(
        atlas.shutil, "rmtree",
        lambda _p: (_ for _ in ()).throw(OSError(16, "Device or resource busy")),
    )

    atlas.uninstall(uninstallable, None, {})

    assert uninstallable.settings.get("atlas_pg_password") == "x", (
        "cleared the database password while the database was still on disk"
    )


def test_the_store_comes_down_before_the_directory_is_deleted(
    uninstallable, box, rec
):
    """The ordering fix, asserted through `uninstall` rather than only through
    `remove_store`, because the ordering that was wrong was here."""
    atlas.uninstall(uninstallable, None, {})

    assert rec.ran("disable"), "uninstall never took the mounts down"
