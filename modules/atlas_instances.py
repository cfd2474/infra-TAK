# infra-TAK — the ATLAS instance model (W216)
"""Several ATLAS deployments on one box, one per agency.

⚠️ **Pure by design, and that is the point.** Every name a deployment needs —
directory, image, mount points, unit names, compose project, volume, vhost,
device hostname, Caddy CA directory, Authentik slug, settings prefix, internal
port — is derived in one place, :func:`derive`. W212 was a teardown that missed
*one* path on a single instance; N instances multiply that risk by N, and the
only defence that scales is having one function to be wrong in.

Nothing here touches a disk, a container or the network, so it is all decidable
without a box and testable on the development machine.

⚠️ **The slug is four identifiers at once.** It becomes a DNS label, part of a
systemd unit name, a Docker Compose project name and a Docker volume name, so it
has to satisfy the intersection of all four rather than merely look tidy.
"""
import re

#: The console settings key holding the instance list.
INSTANCES_KEY = 'atlas_instances'

MODE_FIXED = 'fixed'
MODE_DYNAMIC = 'dynamic'
MODES = (MODE_FIXED, MODE_DYNAMIC)

#: 32 keeps `atlas.<slug>.<fqdn>` inside the 63-character DNS label limit with
#: room to spare, and keeps systemd unit names readable.
SLUG_MAX = 32

#: Lowercase letters, digits and hyphens; no leading or trailing hyphen; no
#: dots, because the slug is a single DNS label.
_SLUG_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,%d}[a-z0-9])?$' % (SLUG_MAX - 2))

#: ⚠️ Not decoration. The settings prefix for a slug is `atlas_<slug>_`, so a
#: slug of `pg` would produce `atlas_pg_password` — **the plain instance's own
#: database password key**. Any slug whose prefix can collide with an existing
#: `atlas_*` key is refused; the rest are reserved because they make confusing
#: hostnames.
RESERVED_SLUGS = frozenset({
    # Would collide with the plain instance's settings keys.
    'pg', 'commit', 'access', 'enabled', 'instances',
    # Confusing as `atlas.<slug>.<fqdn>`.
    'atlas', 'admin', 'api', 'www', 'device', 'devices',
})

#: Internal loopback port for the api container. The plain instance already uses
#: 8760 on the live box, so the range starts there and the plain instance keeps
#: it.
BASE_PORT = 8760

#: Ports other things on the box are known to hold. Callers should pass any
#: they observe as well — this is a floor, not an inventory.
KNOWN_TAKEN_PORTS = frozenset({5000, 5080, 5090, 8080, 8443, 8449, 9443, 3100, 8888})


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


def make(slug, mode, size_gb, port):
    """One instance record. `slug` is None for the non-agency deployment."""
    return {'slug': slug, 'mode': mode, 'size_gb': size_gb, 'port': port}


def plain(instances):
    """The slug-less instance, or None."""
    for inst in instances or ():
        if not inst.get('slug'):
            return inst
    return None


def by_slug(instances, slug):
    for inst in instances or ():
        if (inst.get('slug') or None) == (slug or None):
            return inst
    return None


def size_label(mode):
    """What the size field *means*, which differs by mode.

    ⚠️ One field, two meanings, so the label is not cosmetic: under `fixed` the
    number is space taken from the box up front and held; under `dynamic` it is
    only a ceiling, and the space is not held at all.
    """
    return 'reserved' if mode == MODE_FIXED else 'maximum'


# --------------------------------------------------------------------------- #
# The rule the operator stated
# --------------------------------------------------------------------------- #


def may_deploy_plain(instances):
    """Whether a non-agency-specific ATLAS may be deployed.

    ⚠️ **Derived from current state, never latched.** The operator's rule is
    *"this behavior exists for as long as a non-agency atlas (no slug) is
    deployed"* — so uninstalling the plain instance makes it deployable again,
    and the setup question comes back with it. A flag set once at first deploy
    would answer "no" for ever and quietly strand the box.

    The console asks *"is this agency-specific?"* exactly when this is true; when
    it is false the question has only one possible answer, so it is not asked.
    Both behaviours come from this one predicate rather than from two that could
    drift apart.
    """
    return plain(instances) is None


# --------------------------------------------------------------------------- #
# Slugs
# --------------------------------------------------------------------------- #


def validate_slug(raw, instances=()):
    """`(slug, error)`. `error` is a sentence for the operator, or None.

    ⚠️ **Case is forced, everything else is refused** (operator decision,
    2026-09-17). `Agency-A` becomes `agency-a` rather than being rejected: the
    slug is a DNS label, a systemd unit name, a Compose project and a volume
    name, none of which agree about case, and there is exactly one sensible
    interpretation of a capital letter. There is no such single interpretation
    of a space or an underscore, so those still refuse.

    **The normalised slug is what gets stored and used**, so the console must
    show the operator what it settled on — `atlas.agency-a.<fqdn>`, not the
    string they typed. Returning it here is what makes that possible.

    Uniqueness therefore becomes case-insensitive for free: `Agency-A` collides
    with an existing `agency-a`, which is the right answer, because they would
    resolve to the same hostname.
    """
    if raw is None or not str(raw).strip():
        return None, 'An agency slug is required for an agency-specific deployment.'

    slug = str(raw).strip().lower()
    if len(slug) > SLUG_MAX:
        return None, f'Keep the slug to {SLUG_MAX} characters or fewer.'
    if '.' in slug:
        return None, ('A slug is one label, not a domain — it sits inside '
                      'atlas.<slug>.<your-domain>, so it cannot contain a dot.')
    if not _SLUG_RE.match(slug):
        return None, ('Use lowercase letters, digits and hyphens, starting and '
                      'ending with a letter or digit.')
    if slug in RESERVED_SLUGS:
        return None, f'{slug!r} is reserved — please pick another.'
    if by_slug(instances, slug) is not None:
        return None, f'There is already an ATLAS deployment for {slug!r}.'
    return slug, None


def validate_mode(mode):
    """`(mode, error)`."""
    if mode in MODES:
        return mode, None
    return None, (f'Choose a sizing mode: {MODE_FIXED} reserves the space up '
                  f'front, {MODE_DYNAMIC} shares it and grows as needed.')


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


def next_port(instances=(), taken=()):
    """The next free loopback port for an api container.

    Deterministic, so the same box produces the same answer twice, and skipping
    anything already spoken for — `taken` is for ports observed on the box,
    because this module cannot see them.
    """
    used = {inst.get('port') for inst in instances or ()}
    used |= set(KNOWN_TAKEN_PORTS)
    used |= set(taken or ())
    port = BASE_PORT
    while port in used:
        port += 1
    return port


# --------------------------------------------------------------------------- #
# Every name in one place
# --------------------------------------------------------------------------- #


def derive(instance, fqdn=None):
    """Every name this instance uses. One function, so there is one place to be
    wrong.

    ⚠️ **The plain instance must derive exactly what is already deployed.** The
    live box runs `/root/atlas`, `/var/lib/atlas/store.img`, compose project
    `takmdm`, volume `takmdm_pgdata`, `/var/lib/caddy/atlas` and settings keyed
    `atlas_*`. A refactor that quietly moved any of those would strand a running
    deployment, so the slug-less branch reproduces them and a test pins it.
    """
    slug = (instance or {}).get('slug') or None
    name = 'atlas' if slug is None else f'atlas-{slug}'
    directory = f'/root/{name}'
    host = 'atlas' if slug is None else f'atlas.{slug}'

    return {
        'slug': slug,
        'name': name,
        # The job slot and lock. ⚠️ `[a-z0-9_-]` only — the descriptor validator
        # rejects anything else, and a colon here would fail at import.
        'job_key': name,
        'dir': directory,
        'image': f'/var/lib/{name}/store.img',
        'image_dir': f'/var/lib/{name}',
        'mount': f'{directory}/store',
        'artifacts': f'{directory}/artifacts',
        'cache': f'{directory}/cache',
        # ⚠️ Compose takes the project name from `-p`, which outranks the
        # `name:` in the released compose file, so ATLAS itself needs no change.
        'compose_project': 'takmdm' if slug is None else f'takmdm-{slug}',
        'pg_volume': 'takmdm_pgdata' if slug is None else f'takmdm-{slug}_pgdata',
        'port': (instance or {}).get('port') or BASE_PORT,
        'caddy_ca_dir': f'/var/lib/caddy/{name}',
        'authentik_slug': name,
        'admin_group': f'{name}-admins',
        # ⚠️ `atlas_` for the plain instance, because those keys already exist on
        # every deployed box. See RESERVED_SLUGS for why a slug cannot collide.
        'settings_prefix': 'atlas_' if slug is None else f'atlas_{slug}_',
        'vhost': f'{host}.{fqdn}' if fqdn else None,
        'device_host': f'{host}.{fqdn}' if fqdn else None,
        'mode': (instance or {}).get('mode') or MODE_FIXED,
        'size_gb': (instance or {}).get('size_gb'),
    }


# --------------------------------------------------------------------------- #
# Capacity: what this box can still take
# --------------------------------------------------------------------------- #

GIB = 1024 ** 3

#: Measured on the development box (2026-09-17): an idle instance with no devices
#: enrolled held 846 MiB and **peaked at 1.27 GiB** while the repository indexes
#: were parsed.
#:
#: ⚠️ Planning uses the peak, not the steady state. N instances restarting
#: together spike together, and the steady figure would promise room that a
#: simultaneous restart would not find. Devices add on top of both, so this is a
#: floor.
INSTANCE_RAM_PEAK_BYTES = int(1.27 * GIB)

#: The protected floor: disk that never belongs to ATLAS, for the other InfraTAK
#: modules to grow into. Operator decision, 2026-09-17.
#:
#: ⚠️ This sizes **module data growth**, which measured ~3.5 GB in total on the
#: box. It cannot bound image and build-cache accumulation — 42 GB in
#: `/var/lib/containerd`, which grows with every release build — because a
#: reservation cannot bound something unbounded. That needs a prune policy, not
#: a bigger floor.
DEFAULT_FLOOR_GB = 25


def budget_bytes(disk_total, non_atlas_used, floor_bytes):
    """The most ATLAS may ever hold on this box, all instances together."""
    return max(0, disk_total - non_atlas_used - floor_bytes)


def committed_bytes(instances):
    """What the existing instances already account for.

    ⚠️ **Both modes count.** A fixed instance has taken its space; a dynamic one
    has only a ceiling. Counting only the fixed ones would let the budget be
    exhausted by ceilings nobody had allowed for — and the operator's rule is
    that a request exceeding the budget is *refused*, which needs a figure that
    includes what has already been promised.
    """
    return sum(int((i.get('size_gb') or 0) * GIB) for i in instances or ())


def reserved_bytes(instances):
    """Only what is actually held on disk — the fixed instances.

    The difference from :func:`committed_bytes` is the whole point of dynamic
    mode: space inside a dynamic instance's ceiling is still available to the
    rest of the box until that agency writes to it.
    """
    return sum(int((i.get('size_gb') or 0) * GIB)
               for i in instances or () if i.get('mode') == MODE_FIXED)


def fits(requested_gb, instances, budget):
    """`(ok, error)` — whether another instance of this size may be created."""
    requested = int((requested_gb or 0) * GIB)
    if requested <= 0:
        return False, 'Choose a size for this deployment.'
    used = committed_bytes(instances)
    if used + requested > budget:
        spare = max(0, budget - used)
        return False, (
            f'That would take ATLAS past its budget. '
            f'{spare / GIB:.0f} GB of {budget / GIB:.0f} GB is still uncommitted.'
        )
    return True, None


def instances_that_fit(size_gb, instances, budget):
    """How many more of this size the budget allows. For the deploy screen."""
    each = int((size_gb or 0) * GIB)
    if each <= 0:
        return 0
    return max(0, (budget - committed_bytes(instances)) // each)


def instances_that_ram_allows(ram_available, reserve_bytes, per_instance=None):
    """How many more instances memory allows, at the measured peak."""
    each = per_instance or INSTANCE_RAM_PEAK_BYTES
    return max(0, (max(0, ram_available - reserve_bytes)) // each)


def binding_constraint(by_disk, by_ram):
    """Which limit bites first, and how many more instances that allows.

    ⚠️ Returned as a pair so the page can *name* it. On the box measured, disk
    allowed four or five more and memory about twelve — and an operator reading
    only the larger number would plan for twice what fits. Naming the binding
    constraint is the difference between a dashboard and a number.
    """
    if by_disk <= by_ram:
        return 'disk', by_disk
    return 'memory', by_ram
