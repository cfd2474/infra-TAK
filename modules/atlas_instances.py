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

    ⚠️ Refuses rather than repairs. Silently lowercasing or trimming an
    operator's slug would put a hostname on the box that is not the one they
    typed, and they would find out from DNS.
    """
    if raw is None or not str(raw).strip():
        return None, 'An agency slug is required for an agency-specific deployment.'

    slug = str(raw).strip()
    if slug != slug.lower():
        return None, (f'Use lowercase: {slug!r} would become part of a hostname, '
                      f'a systemd unit name and a Docker volume name, and those '
                      f'do not agree about case.')
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
