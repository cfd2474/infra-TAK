"""Bounding the ATLAS store size (W205).

⚠️ **The first tests in this repository.** The console has none, which is why the
ATLAS module's pure logic is written as pure functions and checked here rather
than discovered on a box. Run them with any pytest:

    python -m pytest tests/test_atlas_store_sizing.py

The module imports only the standard library at the top level, so it loads
without the console around it.

⚠️ What is *not* here: creating the image, making the filesystem, mounting it,
or migrating Postgres into it. Those need a real kernel and a real disk. This
file covers the arithmetic and the refusals — the part that decides whether an
operator is allowed to ask for 200G on a box that has 40.
"""

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    """Imported as `modules.atlas`, not from its file path.

    ⚠️ It uses a relative import (`from . import register_module`), so loading
    it standalone fails. `modules/__init__.py` deliberately keeps its top-level
    imports to the standard library — "third-party imports are lazy so
    `import modules` stays clean" — which is exactly what makes this possible
    without the console around it.
    """
    sys.path.insert(0, str(ROOT))
    import modules.atlas as module

    return module


atlas = _load_module()

GIB = 1024 ** 3


# --------------------------------------------------------------------------- #
# The ceiling
# --------------------------------------------------------------------------- #


def test_the_ceiling_is_85_percent_of_free_space():
    assert atlas.store_ceiling_bytes(100 * GIB) == pytest.approx(85 * GIB, rel=1e-6)


def test_an_existing_reservation_counts_towards_the_ceiling():
    """⚠️ Without this a box that already reserved 40G could never keep it.

    Once the image exists, its space is no longer *free* — so a ceiling computed
    from free space alone would be 85% of what is left over, and an operator
    re-deploying with the size they already have would be told the box cannot
    offer it.
    """
    # 60 free, 40 already reserved: the honest pool is 100.
    assert atlas.store_ceiling_bytes(60 * GIB, already_reserved=40 * GIB) == (
        pytest.approx(85 * GIB, rel=1e-6)
    )


def test_a_full_disk_offers_nothing():
    assert atlas.store_ceiling_bytes(0) == 0


def test_a_negative_reading_does_not_become_a_bonus():
    """`statvfs` failing returns zeros, and arithmetic on a bad reading must not
    produce a ceiling larger than the disk."""
    assert atlas.store_ceiling_bytes(-5 * GIB) == 0
    assert atlas.store_ceiling_bytes(10 * GIB, already_reserved=-5 * GIB) == (
        pytest.approx(8.5 * GIB, rel=1e-6)
    )


# --------------------------------------------------------------------------- #
# The refusals
# --------------------------------------------------------------------------- #


def test_a_size_within_the_ceiling_is_accepted():
    size, error = atlas.validate_store_size(40, free_bytes=100 * GIB)

    assert error is None
    assert size == 40 * GIB


def test_more_than_the_box_has_is_refused():
    """⚠️ The operator's requirement: it must not configure more than the box
    has."""
    size, error = atlas.validate_store_size(200, free_bytes=100 * GIB)

    assert size == 0
    assert "at most" in error


def test_the_ceiling_is_the_85_percent_line_not_the_whole_disk():
    """86 GB of a 100 GB disk is refused even though the disk holds it."""
    _, error = atlas.validate_store_size(86, free_bytes=100 * GIB)
    assert error is not None

    _, ok = atlas.validate_store_size(85, free_bytes=100 * GIB)
    assert ok is None


def test_the_refusal_says_what_the_box_can_offer():
    """⚠️ A refusal that does not name the number leaves the operator guessing,
    and they will guess by trying again."""
    _, error = atlas.validate_store_size(200, free_bytes=100 * GIB)

    assert "85.0 GB" in error


def test_a_size_is_refused_rather_than_clamped():
    """⚠️ Silently handing back a different number than the one typed is how an
    operator comes to believe they reserved 200G on a box that gave them 40."""
    size, error = atlas.validate_store_size(200, free_bytes=100 * GIB)

    assert size == 0
    assert error is not None


def test_nonsense_is_refused_without_raising():
    for bad in ("", None, "forty", "40G", [], float("nan"), float("inf")):
        size, error = atlas.validate_store_size(bad, free_bytes=100 * GIB)
        assert size == 0, bad
        assert error, bad


def test_a_size_too_small_to_hold_what_atlas_ships_is_refused():
    """ATLAS carries ~570 MB of seed applications and indexes before an operator
    uploads anything, so a 1 GB store is a trap rather than a choice."""
    _, error = atlas.validate_store_size(1, free_bytes=100 * GIB)

    assert error is not None
    assert "at least" in error


def test_shrinking_below_what_is_stored_is_refused():
    """⚠️ The alternative is deleting data to fit a number, which no setting
    should do quietly."""
    size, error = atlas.validate_store_size(
        10, free_bytes=100 * GIB, in_use_bytes=30 * GIB
    )

    assert size == 0
    assert "already storing" in error


def test_keeping_the_size_you_already_have_is_allowed():
    """The re-deploy case, and the reason `already_reserved` exists at all."""
    size, error = atlas.validate_store_size(
        40, free_bytes=2 * GIB, already_reserved=40 * GIB
    )

    assert error is None
    assert size == 40 * GIB


def test_a_reservation_survives_the_disk_filling_up_around_it():
    """⚠️ **The bug this test found, and the reason the rule is what it is.**

    A box reserves 40 GB when the disk is roomy. Other modules grow. Now only
    2 GB is free, so 85% of the honest 42 GB pool is 35.7 GB — less than the
    40 GB already reserved. A ceiling check alone refuses every re-deploy, and
    the operator cannot escape by shrinking either, because shrinking below what
    is stored is also refused.

    Keeping what is already reserved takes nothing further from the box, so it is
    always allowed. The 85% rule governs *new* claims.
    """
    size, error = atlas.validate_store_size(
        40, free_bytes=2 * GIB, already_reserved=40 * GIB
    )
    assert error is None and size == 40 * GIB

    # Asking for *more* than is reserved is still bounded by the 85% line.
    _, refused = atlas.validate_store_size(
        41, free_bytes=2 * GIB, already_reserved=40 * GIB
    )
    assert refused is not None


def test_asking_for_less_than_is_reserved_is_refused():
    """⚠️ Refused, because the module will not shrink the image.

    Growing the store is `fallocate` plus an online `resize2fs`; shrinking it
    safely needs the stack down and the image unmounted, and that does not exist
    yet. Accepting 20 GB and keeping 40 would be the "handed back a different
    number" failure in its purest form — so it says so instead.
    """
    size, error = atlas.validate_store_size(
        20, free_bytes=0, already_reserved=40 * GIB
    )

    assert size == 0
    assert 'shrinking it is not supported' in error
    assert '40.0 GB' in error


def test_growing_an_existing_reservation_is_still_bounded():
    """Growth is a new claim on the disk, so the 85% line applies to it."""
    size, error = atlas.validate_store_size(
        50, free_bytes=20 * GIB, already_reserved=40 * GIB
    )
    assert error is None and size == 50 * GIB

    _, refused = atlas.validate_store_size(
        59, free_bytes=20 * GIB, already_reserved=40 * GIB
    )
    assert refused is not None


# --------------------------------------------------------------------------- #
# Reading the box
# --------------------------------------------------------------------------- #


def test_free_space_is_read_from_the_nearest_existing_directory(tmp_path):
    """⚠️ `statvfs` raises on a path that does not exist, and the image's parent
    is created by the very deploy that needs this number — so asking before it
    exists is the normal case, not an error."""
    missing = tmp_path / "not" / "here" / "yet"

    total, free = atlas._disk_free(str(missing))

    assert total > 0
    assert free > 0


def test_free_space_excludes_the_filesystem_root_reserve():
    """⚠️ **A source check, and labelled as one because this platform cannot
    tell the difference.**

    `usage.free` is what a non-root process may use; `usage.total - usage.used`
    includes ext4's 5% root reserve. On the box that is ~18 GB of a 473 GB disk
    promised away and not actually available. On NTFS the two are identical, so a
    behavioural test here would pass against either and prove nothing — a
    mutation sweep confirmed exactly that.

    Asserting the source is weak. Asserting nothing would be weaker.
    """
    source = (ROOT / "modules" / "atlas.py").read_text(encoding="utf-8")
    body = source[source.index("def _disk_free"): source.index("def store_ceiling_bytes")]

    assert "usage.free" in body
    assert "usage.total - usage.used" not in body


def test_an_unreadable_path_reports_nothing_rather_than_raising():
    total, free = atlas._disk_free("\x00")

    assert (total, free) == (0, 0)


def test_the_facts_the_console_draws_are_all_present():
    facts = atlas.store_facts()

    for key in (
        "disk_total_gb", "disk_free_gb", "reserved_gb",
        "usable_gb", "used_gb", "max_gb", "min_gb", "mounted",
    ):
        assert key in facts, key


def test_the_reported_maximum_is_never_more_than_the_disk():
    """⚠️ The whole point of showing a number: it has to be one the box can
    actually honour."""
    facts = atlas.store_facts()

    assert facts["max_gb"] <= facts["disk_total_gb"]


# --------------------------------------------------------------------------- #
# The systemd units
# --------------------------------------------------------------------------- #


def test_the_mount_unit_starts_before_docker():
    """⚠️ **The load-bearing line in the whole feature.**

    Docker starting first would let it create the Postgres volume, and a
    container write into it, against a directory that is not yet the
    reservation — so ATLAS would fill the root disk while the reserved image sat
    empty. Boot order is the only thing between that and a reboot.
    """
    unit = atlas.mount_unit_text(
        '/var/lib/atlas/store.img', '/root/atlas/store', 'ext4', 'loop', 'store'
    )

    assert 'Before=docker.service' in unit


def test_the_mount_unit_survives_a_reboot():
    """⚠️ A bare `mount` would be worse than nothing: after a reboot
    `<atlas>/store` becomes an ordinary directory on the root disk, ATLAS writes
    to it happily, and the reservation sits unused in a file."""
    unit = atlas.mount_unit_text('/img', '/mnt/x', 'ext4', 'loop', 'store')

    assert 'WantedBy=multi-user.target' in unit


def test_the_unit_names_the_image_and_the_mount_point():
    unit = atlas.mount_unit_text(
        '/var/lib/atlas/store.img', '/root/atlas/store', 'ext4', 'loop', 'store'
    )

    assert 'What=/var/lib/atlas/store.img' in unit
    assert 'Where=/root/atlas/store' in unit
    assert 'Type=ext4' in unit
    assert 'Options=loop' in unit


def test_a_bind_unit_waits_for_the_filesystem_it_binds_out_of():
    """A bind into the store cannot mount before the store does."""
    unit = atlas.mount_unit_text(
        '/root/atlas/store/artifacts', '/root/atlas/artifacts', 'none', 'bind',
        'bind', requires_mounts_for='/root/atlas/store',
    )

    assert 'RequiresMountsFor=/root/atlas/store' in unit
    assert 'Type=none' in unit
    assert 'Options=bind' in unit


def test_a_plain_mount_unit_has_no_stray_dependency():
    unit = atlas.mount_unit_text('/img', '/mnt/x', 'ext4', 'loop', 'store')

    assert 'RequiresMountsFor' not in unit


def test_the_unit_says_it_is_generated():
    """An operator who finds this file has to know editing it is pointless."""
    unit = atlas.mount_unit_text('/img', '/mnt/x', 'ext4', 'loop', 'store')

    assert unit.startswith('# Managed by the InfraTAK ATLAS module')


def test_the_unit_filename_encodes_its_mount_point():
    """⚠️ systemd refuses a mount unit whose filename does not match its
    `Where=` — "Where= setting doesn't match unit name". `systemd-escape` is
    asked when present; this is the fallback, and it has to agree."""
    assert atlas._systemd_unit_name('/root/atlas/store') == 'root-atlas-store.mount'
    assert atlas._systemd_unit_name('/root/atlas/cache') == 'root-atlas-cache.mount'


# --------------------------------------------------------------------------- #
# What the deploy form is allowed to send
# --------------------------------------------------------------------------- #


def test_no_size_supplied_means_no_reservation():
    """⚠️ Absent is not zero. An existing box keeps what it has, and a new
    one gets no reservation rather than a size nobody chose."""
    params, error = atlas.deploy_validate({})
    assert error is None and params == {}

    params, error = atlas.deploy_validate({'store_gb': ''})
    assert error is None and params == {}


def test_a_bad_size_is_refused_before_the_deploy_starts():
    """⚠️ Before, not during. A refusal halfway through leaves a half-built
    box, and this number is knowable from the form alone."""
    params, error = atlas.deploy_validate({'store_gb': 'lots'})

    assert params == {}
    assert error


# --------------------------------------------------------------------------- #
# The console field
# --------------------------------------------------------------------------- #


def test_the_deploy_card_carries_the_store_field():
    page = (ROOT / "templates" / "atlas.html").read_text(encoding="utf-8")

    assert 'id="storeGb"' in page
    assert "store_gb" in page, "the deploy POST does not send the size"


def test_the_page_warns_that_free_disk_will_drop():
    """⚠️ The one thing an operator will otherwise report as a bug. The
    space disappears from `df` the moment it is reserved, which is the feature
    working — so the page says it before the button, not after."""
    page = (ROOT / "templates" / "atlas.html").read_text(encoding="utf-8")

    assert "Free disk will drop by this much immediately" in page


def test_the_store_field_behaves():
    """⚠️ Driven, not matched.

    The field reports free space, sets its own ceiling to the 85% line, prefills
    an existing reservation, and says so when the disk cannot be read. None of
    that is visible in a string search, which is what
    `scripts/check_store_field.js` exists for — plain node with a hand-rolled DOM
    and a stubbed fetch, no jsdom and no dependencies.

    Skipped only when there is no `node`, and loudly: nothing else exercises this.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "no node on PATH, so the reserved-storage field is unexercised. The "
            "rest of this module only checks the markup is present."
        )

    result = subprocess.run(
        [node, str(ROOT / "scripts" / "check_store_field.js")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "all checks passed" in output, output
