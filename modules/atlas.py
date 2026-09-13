# infra-TAK module — ATLAS MDM
"""ATLAS: Android device management for ATAK tablets.

A Device Owner MDM. The console is an ordinary admin web UI; the interesting
half is the **device channel**, where enrolled tablets authenticate with client
certificates issued by ATLAS's own CA and receive a desired state to converge
on.

Three things about that shape the whole module:

* **The device channel is mutual TLS and it is not optional.** Caddy terminates
  it on its own port, verifies the client certificate against the device CA this
  deployment generated, and forwards it. Devices cannot do an interactive login,
  so Authentik is not in that path — the same split EUD Remote Assist uses.
* **The console is admin tooling**, so it sits behind Authentik on
  `atlas.<fqdn>` like every other admin surface, and is never published directly.
* **The agent package is fetched by a tablet in out-of-box setup**, before it
  has been told anything. It reaches Caddy on the well-known port, and its
  integrity comes from a signature checksum carried in the provisioning QR.

⚠️ **ATLAS never speaks ACME.** Caddy is the single ACME client on the box and
holds the certificate for `atlas.<fqdn>`; two clients contending for :80 does not
resolve in a config file, it means one of them quietly stops renewing.
"""
import os
import secrets
import shutil
import subprocess

from . import register_module, job_log

KEY = 'atlas'

# Source, pinned. Rule 8: a tag *and* the commit it resolved to, verified after
# fetch — a moving branch is not a pin, and a tag can be moved by whoever owns
# the repo.
# The repository is public, so there is no credential in this file and none
# needed on the box.
ATLAS_REPO_HTTPS = 'https://github.com/cfd2474/TAK-MDM.git'
# Where the update check asks what the newest release is. Unauthenticated,
# which the public repository allows and which keeps any credential out of a
# world-readable module file.
ATLAS_REPO_API = 'https://api.github.com/repos/cfd2474/TAK-MDM'
ATLAS_TAG = 'v1.0.1'
# ⚠️ The **commit**, not the tag object. `v0.1.0` is an annotated tag, so
# `git rev-parse v0.1.0` returns the tag object's own SHA while a clone's HEAD
# is the commit it points at — two different hashes, and comparing them made
# every deploy refuse itself. `git rev-parse 'v0.1.0^{}'` is the one to record.
ATLAS_SHA = '76f00dd8fe0bee80e731488cd58aeafcd6cd7efd'

# The device channel. One public port, justified: enrolled tablets cannot reach
# the console's vhost (Authentik would bounce a device that cannot log in), and
# mutual TLS needs a listener of its own.
DEVICE_PORT = 8449

# The application, on loopback. Caddy is the only thing that talks to it.
APP_PORT = 8760

# ⚠️ The container the API runs as. ATLAS's compose file pins its project name
# to `takmdm`, so the containers are `takmdm-*` no matter what directory the
# module installs into — `--project-directory` does not override an explicit
# `name:`. Guessing `atlas-api-1` from the module key made detect() report the
# module as never running, forever.
API_CONTAINER = 'takmdm-api-1'

# ⚠️ The uid the application runs as inside its image, and the host directories
# it must be able to write.
#
# ATLAS's Dockerfile drops to an unprivileged `takmdm` user pinned at uid 1000.
# These three paths are bind-mounted from the install directory, which the
# console creates as root — and Docker creates any that are still missing as
# root too. Either way the container cannot write them, and the first thing it
# tries is to generate the device CA:
#
#     PermissionError: [Errno 13] Permission denied: '/pki/ca.crt'
#
# The one-shot `init` service then exits 1, `api` never starts, and the deploy
# fails at the build step with nothing in the compose output naming a
# permission problem. So: create them, and hand them to uid 1000 first.
APP_UID = 1000
APP_GID = 1000

# Writable bind mounts, from ATLAS's docker-compose.yml. `pki` holds the device
# CA, `artifacts` the agent APK and generated packages, `cache` the APK
# inspector's scratch space. The read-only mounts (nginx config, acme) belong
# to ATLAS's own proxy service, which this module never starts — Caddy fronts
# the deployment instead.
WRITABLE_DIRS = ('pki', 'artifacts', 'cache')


def _plog(msg):
    job_log(KEY, msg)


def atlas_dir(ctx):
    """Where ATLAS is installed.

    ⚠️ Two layouts, and the difference is not cosmetic. A console born
    unprivileged keeps modules under its own home; a box flipped from root keeps
    them at `/root`. Guessing wrong means a deploy that "succeeds" against an
    empty directory while the real install sits elsewhere.
    """
    root_path = os.path.join('/root', KEY)
    if os.path.isdir(os.path.join(root_path, '.git')):
        return root_path
    return os.path.expanduser(os.path.join('~', KEY))


def _compose_argv(ctx, *action):
    return ['docker', 'compose', '--project-directory', atlas_dir(ctx)] + list(action)


def _compose(ctx, action, timeout=180):
    """Run `docker compose <action>` in the install directory, via the broker.

    ⚠️ **`action` is a string, not argv.** `_broker_compose` does
    `shlex.split(action)` on it, so a list arrives as a file-like object and
    dies with `'list' object has no attribute 'read'` — an error that names
    neither compose nor the argument that was wrong. `control_map` is the
    opposite: the registry sudo-wraps and runs that one itself, so it takes a
    real argv list. Two neighbouring seams, two conventions.
    """
    return ctx['_broker_compose'](atlas_dir(ctx), action, timeout=timeout)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def detect(ctx):
    """Whether ATLAS is installed here, and whether it is up.

    ⚠️ **Installed is a settings flag, not a directory.** The console may be
    running unprivileged and cannot read a root-owned install dir; a filesystem
    probe would report a working deployment as absent.

    ⚠️ **Running probes the API container only.** There was once a loopback port
    to curl; it is gone on purpose (it bypassed mutual TLS), and a probe against
    it would make this module report itself down forever.
    """
    s = ctx['load_settings']()
    enabled = bool(s.get(f'{KEY}_enabled'))
    running = False
    try:
        r = ctx['probe_run'](
            ['docker', 'inspect', '--format', '{{.State.Running}}', API_CONTAINER],
            text=True, timeout=3,
        )
        running = (r.stdout or '').strip() == 'true'
    except Exception:
        running = False

    # Self-heal: the container is up but the flag was lost (a settings file
    # restored from before the install, most often). Trust the container.
    if running and not enabled:
        s = ctx['load_settings']()
        s[f'{KEY}_enabled'] = True
        ctx['save_settings'](s)
        enabled = True

    # The version comes from the checkout, so the tile cannot disagree with the
    # footer of the console it is describing.
    return {'installed': enabled, 'running': running,
            'version': _installed_version(ctx) if enabled else None}


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #


def deploy_validate(data):
    """Nothing to validate: the repository is public and takes no credential."""
    return {}, None


def _stale_deploy_key(dirpath):
    """Paths of the key a private-repo install left behind, if still present.

    The repository is public now, so this key opens nothing. It is removed on
    deploy rather than left to rot: a credential kept past its purpose is only
    ever a liability, and nobody audits a file they have forgotten exists.
    """
    base = os.path.dirname(dirpath)
    return [p for p in (os.path.join(base, f'.{KEY}_deploy_key'),
                        os.path.join(base, f'.{KEY}_deploy_key.pub'))
            if os.path.exists(p)]



def _write_build_file(dirpath, plog=None):
    """Record the checked-out revision where the container can still read it.

    ⚠️ `.dockerignore` excludes `.git`, and it should — but that leaves the
    running console unable to answer "is this exactly the code I think it is".
    It reported `revision unknown` on every InfraTAK deployment while the footer
    happily showed a version, which is the worse half of both worlds: a number
    to trust and no way to check it.

    ATLAS reads this file at `app/version.py:_from_file`, which exists for
    precisely this case. Written before the image is built, so it is copied in.
    """
    try:
        r = subprocess.run(
            ['git', '-C', dirpath, 'log', '-1', '--format=%h %cs'],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        revision, _, committed = r.stdout.strip().partition(' ')
        with open(os.path.join(dirpath, 'BUILD'), 'w', encoding='utf-8') as handle:
            handle.write('revision=%s\ncommitted=%s\ndirty=false\n'
                         % (revision, committed))
        if plog:
            plog('  \u2713 Build stamp written (revision %s)' % revision)
        return revision
    except (OSError, subprocess.SubprocessError):
        return None


def _verify_pin(ctx, dirpath, plog):
    """Check the checkout is the commit we meant to install.

    ⚠️ A tag is a pointer and whoever owns the repository can move it. Rule 8
    wants the SHA checked *after* the fetch, which is the only moment the
    difference is observable.

    ⚠️ `ATLAS_SHA` must be the **commit** the tag dereferences to. An annotated
    tag is itself an object with its own hash, so `git rev-parse <tag>` and the
    HEAD of a clone made from it are different strings — and comparing them
    fails every time, which reads exactly like a tag that has been tampered
    with. Record `git rev-parse '<tag>^{}'`.
    """
    r = ctx['_module_git'](dirpath, 'rev-parse', 'HEAD', timeout=10)
    head = (r.stdout or '').strip()
    if not ATLAS_SHA:
        plog(f'  ⚠ No pinned SHA recorded — installed {head[:12]} from {ATLAS_TAG}')
        return head
    if not head.startswith(ATLAS_SHA) and not ATLAS_SHA.startswith(head):
        raise RuntimeError(
            f'{ATLAS_TAG} resolved to {head[:12]}, expected {ATLAS_SHA[:12]} — '
            'refusing to install a tag that has moved'
        )
    plog(f'  ✓ Pin verified: {ATLAS_TAG} = {head[:12]}')
    return head


def deploy(ctx, job, params):
    plog = _plog
    dirpath = atlas_dir(ctx)
    params = params or {}
    try:
        settings = ctx['load_settings']()

        # ── 1/7 Docker ────────────────────────────────────────────────────────
        plog('━━━ Step 1/7: Checking Docker ━━━')
        rc, version = ctx['_docker_probe']()
        if rc != 0:
            plog('  Docker not found — installing...')
            if not ctx['_install_docker_engine'](plog):
                raise RuntimeError('Docker install failed — see log above')
            plog('✓ Docker installed')
        else:
            plog(f'✓ Docker present: {version}')

        # ── 2/7 Source ────────────────────────────────────────────────────────
        plog('')
        plog('━━━ Step 2/7: Fetching ATLAS ━━━')
        # Three ways to have a key, in order of how deliberate they are:
        # pasted into the deploy form, kept from a previous deploy, or already
        # placed on the box by an operator who would rather not paste a private
        # key into a web form at all. The last one is the reason this checks the
        # filesystem — the key never has to travel.
        # A key left over from when this repository was private opens nothing
        # now. Removed here rather than left to rot.
        for stale in _stale_deploy_key(dirpath):
            try:
                os.remove(stale)
                plog(f'  Removed the obsolete deploy key ({stale})')
            except OSError:
                pass
        repo = ATLAS_REPO_HTTPS

        if os.path.isdir(os.path.join(dirpath, '.git')):
            plog(f'  Already cloned at {dirpath} — fetching {ATLAS_TAG}')
            ctx['_module_git'](dirpath, 'checkout', '--', '.', timeout=60)
            r = subprocess.run(
                ['git', '-C', dirpath, 'fetch', '--tags', '--depth=1', 'origin', ATLAS_TAG],
                capture_output=True, text=True, timeout=300, env=None,
            )
            if r.returncode != 0:
                raise RuntimeError(f'git fetch failed: {r.stderr[-300:]}')
            ctx['_module_git'](dirpath, 'checkout', '-f', ATLAS_TAG, timeout=60)
        else:
            os.makedirs(dirpath, exist_ok=True)
            plog(f'  Cloning {repo} @ {ATLAS_TAG}')
            r = subprocess.run(
                ['git', 'clone', '--depth=1', '--branch', ATLAS_TAG, repo, dirpath],
                capture_output=True, text=True, timeout=600, env=None,
            )
            if r.returncode != 0:
                hint = ''
                if 'Permission denied' in r.stderr or 'not read from remote' in r.stderr:
                    hint = ' — the repository is private; supply a read-only deploy key'
                raise RuntimeError(f'git clone failed{hint}: {r.stderr[-300:]}')
        commit = _verify_pin(ctx, dirpath, plog)
        _write_build_file(dirpath, plog)
        plog('✓ Source in place')

        # ── 3/7 Configuration ─────────────────────────────────────────────────
        plog('')
        plog('━━━ Step 3/7: Writing configuration ━━━')
        # Generated once and kept. Regenerating on a re-deploy would leave the
        # existing database unopenable by the application that owns it.
        #
        # ⚠️ Persisted *here*, before anything uses it — not with the rest of the
        # settings in step 6. Postgres applies POSTGRES_PASSWORD only when it
        # initialises an empty volume and ignores it ever after, so the moment
        # step 4 starts the database this value is baked in. A deploy that then
        # failed anywhere before step 6 left no record of it, and the next
        # attempt generated a fresh password against a volume that still held
        # the old one:
        #
        #     FATAL: password authentication failed for user "takmdm"
        #
        # — with the API sitting on "waiting for database..." forever and the
        # database permanently unopenable. Writing it first costs nothing: an
        # abandoned deploy leaves a password for a database that may not exist,
        # which is harmless, and a resumed one reuses it, which is the point.
        pg_password = settings.get(f'{KEY}_pg_password')
        if not pg_password:
            pg_password = secrets.token_urlsafe(24)
            s_early = ctx['load_settings']()
            s_early[f'{KEY}_pg_password'] = pg_password
            ctx['save_settings'](s_early)
        fqdn = ctx['_get_service_domain'](ctx['load_settings'](), KEY)

        if not fqdn:
            plog('  ⚠ No domain resolved for this box.')
            plog('    The console will still work through Caddy, but devices')
            plog('    cannot be enrolled without a hostname to put in the QR.')

        device_url = f'https://{fqdn}:{DEVICE_PORT}' if fqdn else ''
        console_url = f'https://{fqdn}' if fqdn else ''
        apk_url = f'http://{fqdn}/api/v1/provisioning/agent.apk' if fqdn else ''
        ctx['_write_priv'](os.path.join(dirpath, '.env'), _ENV_TEMPLATE.format(
            pg_password=pg_password,
            device_url=device_url,
            apk_url=apk_url,
            console_url=console_url,
        ), perm=0o600)
        ctx['_write_priv'](
            os.path.join(dirpath, 'docker-compose.override.yml'),
            _COMPOSE_OVERRIDE.format(app_port=APP_PORT),
        )
        plog(f'✓ .env and docker-compose.override.yml written (app on 127.0.0.1:{APP_PORT})')

        # The container runs unprivileged; the directories it writes are ours.
        # See APP_UID. Done here rather than after `up` because the very first
        # thing the stack does is write the device CA into pki/.
        for name in WRITABLE_DIRS:
            path = os.path.join(dirpath, name)
            os.makedirs(path, exist_ok=True)
            os.chown(path, APP_UID, APP_GID)
            # Anything already inside — a re-deploy over an existing install,
            # or files the repository ships — needs the same owner, or the
            # application can read its own CA but not renew it.
            for root, dirs, files in os.walk(path):
                for entry in dirs + files:
                    os.chown(os.path.join(root, entry), APP_UID, APP_GID)
        plog(f'✓ {", ".join(WRITABLE_DIRS)} owned by uid {APP_UID} (the container is not root)')

        # ── 4/7 Start ─────────────────────────────────────────────────────────
        plog('')
        plog('━━━ Step 4/7: Starting containers ━━━')
        # `api` alone: it depends_on db and the one-shot migration step, and
        # naming it keeps ATLAS's own nginx out of a deployment where Caddy is
        # the only thing that should be terminating TLS.
        r = _compose(ctx, 'up -d --build api', timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f'docker compose up failed:\n{(r.stderr or "")[-500:]}')
        plog('✓ Containers built and started')

        # ── 5/7 Firewall ──────────────────────────────────────────────────────
        plog('')
        plog('━━━ Step 5/7: Opening the device port ━━━')
        ok, detail = ctx['_fw_allow'](DEVICE_PORT, 'tcp')
        plog(f'  {"✓" if ok else "⚠"} allow {DEVICE_PORT}/tcp — {detail}')
        if not ok:
            # Not fatal: a box with no firewall installed answers "no firewall
            # present", and the port is reachable anyway. A box that *has* one
            # and refused is a problem the operator needs to see, not a reason
            # to unwind a working deployment.
            plog('    The device port may be unreachable until this is resolved.')

        # ── 6/7 Register ──────────────────────────────────────────────────────
        plog('')
        plog('━━━ Step 6/7: Registering the module ━━━')
        s = ctx['load_settings']()
        s[f'{KEY}_enabled'] = True
        s[f'{KEY}_pg_password'] = pg_password
        s[f'{KEY}_commit_sha'] = commit
        ctx['save_settings'](s)
        ctx['generate_caddyfile'](s)
        if ctx['_caddy_reload'](plog):
            plog('✓ Caddy reloaded')

        # ── 7/7 Authentik ─────────────────────────────────────────────────────
        plog('')
        plog('━━━ Step 7/7: Administrator sign-in ━━━')
        token = (ctx['_get_authentik_env_value'](s, 'AUTHENTIK_TOKEN') or
                 ctx['_get_authentik_env_value'](s, 'AUTHENTIK_BOOTSTRAP_TOKEN'))
        if fqdn and token:
            plog('  Registering the console with Authentik...')
            # ⚠️ This is what makes the console reachable *and* restricted.
            # Without an application the outpost has nothing to authorise, so
            # Caddy's forward_auth never sets the identity headers ATLAS reads
            # and the console answers 401 to everyone, permanently.
            ctx['_ensure_authentik_atlas_app'](fqdn, token, plog=plog, settings=s)
            # Re-emit now that the application exists: the console vhost only
            # grows its forward_auth block once Authentik is in the picture.
            ctx['generate_caddyfile'](ctx['load_settings']())
            ctx['_caddy_reload'](plog)
            plog('  ✓ Sign in with an Authentik administrator account.')
        else:
            # ⚠️ Degrading here does NOT mean an open console. ATLAS has two auth
            # modes and no middle one; without Authentik the honest answer is
            # that the console is not published, and is reached over an SSH
            # tunnel exactly as the console-by-IP row was resolved.
            plog('  ⚠ Authentik not configured — the console vhost is NOT published.')
            plog('    Reach it over an SSH tunnel:')
            plog(f'      ssh -L 8760:127.0.0.1:{APP_PORT} <this host>')

        plog('')
        plog('✓ ATLAS deployed.')
        if fqdn:
            plog(f'  Console:  https://{fqdn}/')
            plog(f'  Devices:  https://{fqdn}:{DEVICE_PORT}/  (mutual TLS)')
        plog('')
        # The agent and the launcher ship with the source and load into the
        # library on first start, so enrolment works with no upload at all.
        # Said out loud because an operator has no other way to know the
        # library is not empty.
        plog('  Bundled and ready: ATLAS Agent (device policy controller)')
        plog('  and ATLAS Launcher. Enrollment tokens can be minted now.')
        job.update({'running': False, 'complete': True, 'error': False})
    except Exception as exc:
        plog(f'ERROR: {exc}')
        job.update({'running': False, 'complete': False, 'error': True})


# --------------------------------------------------------------------------- #
# Versions, and updating to a newer one
# --------------------------------------------------------------------------- #

#: ⚠️ Slot-local, deliberately NOT the registry's deploy job slot. An update
#: and a deploy must never share a lock or a log: they can be started from
#: different pages seconds apart, and interleaving their output would make both
#: unreadable at exactly the moment somebody needs to read one.
_update_status = {'running': False, 'complete': False, 'error': False, 'log': []}

#: Cheap cache for the upstream tag check. GitHub allows 60 unauthenticated
#: requests an hour per IP and the console polls this for a badge; without a
#: cache a busy box spends its whole allowance and the badge silently blanks.
_latest_cache = {'value': None, 'at': 0.0}
_LATEST_TTL = 900


def _parse_version(text):
    """``v1.2.3`` -> ``(1, 2, 3)``. None for anything that is not three numbers.

    ⚠️ Comparison is on the tuple, never the string: ``0.10.0`` sorts before
    ``0.9.0`` alphabetically, which would quietly stop offering updates at the
    tenth release of any series.
    """
    if not text:
        return None
    parts = str(text).strip().lstrip('vV').split('.')
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _latest_version(use_cache=True):
    """The newest release tag upstream, or None when it cannot be established.

    ⚠️ None means *unknown*, not *up to date*. A rate-limited or offline box
    must never be told it is current; it is told nothing, and the page says so.
    """
    import json
    import time
    import urllib.request

    now = time.time()
    if use_cache and _latest_cache['value'] and now - _latest_cache['at'] < _LATEST_TTL:
        return _latest_cache['value']
    try:
        request = urllib.request.Request(
            ATLAS_REPO_API + '/tags?per_page=50',
            headers={'Accept': 'application/vnd.github+json',
                     'User-Agent': 'infra-TAK-atlas-module'},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            tags = json.loads(response.read().decode())
    except Exception:
        return _latest_cache['value']

    versions = [v for v in (_parse_version(t.get('name')) for t in tags) if v]
    if not versions:
        return _latest_cache['value']
    newest = '.'.join(str(p) for p in max(versions))
    _latest_cache.update({'value': newest, 'at': now})
    return newest


def _installed_version(ctx):
    """The version on disk, read from the checkout rather than from settings.

    ⚠️ This is the same VERSION file the running console shows in its footer,
    so the two cannot disagree. A settings value could, if a deploy half-finished
    — and an update badge that contradicts the footer is worse than no badge.
    """
    try:
        with open(os.path.join(atlas_dir(ctx), 'VERSION'), encoding='utf-8') as handle:
            return handle.read().strip().lstrip('vV') or None
    except OSError:
        return None


def _run_update(ctx):
    """Fetch the newest release and rebuild in place. Data is never touched.

    ⚠️ This is `deploy` minus everything that would destroy state: no volume
    is removed and the install directory survives, so the database, the device CA
    and every enrolled tablet come through it intact. That is the whole
    difference between updating and reinstalling.

    The applications shipped with the new release load on start, and the agent
    among them is offered to the fleet — an update that left every device on
    the previous agent would be a fleet running a build this server no longer is.
    """
    from datetime import datetime

    global _update_status
    log = []

    def plog(msg):
        log.append('[' + datetime.now().strftime('%H:%M:%S') + '] ' + msg)
        _update_status['log'] = list(log)
        print('[' + KEY + '] update: ' + msg, flush=True)

    _update_status.update({'running': True, 'complete': False, 'error': False, 'log': []})
    try:
        dirpath = atlas_dir(ctx)
        if not os.path.isdir(os.path.join(dirpath, '.git')):
            raise RuntimeError('ATLAS is not installed from a git checkout')

        target = _latest_version(use_cache=False)
        if not target:
            raise RuntimeError('Could not reach GitHub to find the newest release')
        current = _installed_version(ctx)
        plog('Installed ' + (current or 'unknown') + ' → available ' + target)

        here, there = _parse_version(current), _parse_version(target)
        if here and there and there <= here:
            plog('✓ Already on the newest release — nothing to do')
            _update_status.update({'running': False, 'complete': True, 'error': False})
            return

        tag = 'v' + target
        plog('━━━ Step 1/3: Fetching ' + tag + ' ━━━')
        # The module rewrites .env and the compose override on every deploy, so
        # the working tree is dirty on tracked files and a plain pull aborts with
        # "local changes would be overwritten".
        ctx['_module_git'](dirpath, 'checkout', '--', '.', timeout=60)
        r = subprocess.run(
            ['git', '-C', dirpath, 'fetch', '--tags', '--depth=1', 'origin', tag],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            raise RuntimeError('git fetch failed: ' + (r.stderr or '')[-300:])
        ctx['_module_git'](dirpath, 'checkout', '-f', tag, timeout=60)
        _write_build_file(dirpath, plog)
        plog('✓ Source now at ' + tag)

        plog('━━━ Step 2/3: Rebuilding ━━━')
        # ⚠️ No `-v` anywhere here. `down -v` would take the database and the
        # device CA with it, and every enrolled tablet would need a factory reset.
        r = _compose(ctx, 'up -d --build api', timeout=1800)
        if r.returncode != 0:
            raise RuntimeError('docker compose up failed: ' + (r.stderr or '')[-500:])
        plog('✓ Containers rebuilt — database and device CA untouched')
        plog('  The agent and launcher from this release load on start, and the')
        plog('  new agent is offered to the fleet on each device\'s next check-in.')

        plog('━━━ Step 3/3: Recording ━━━')
        s = ctx['load_settings']()
        s[KEY + '_version'] = target
        ctx['save_settings'](s)
        plog('✓ ATLAS updated to v' + target)
        _update_status.update({'running': False, 'complete': True, 'error': False})
    except Exception as exc:
        plog('ERROR: ' + str(exc))
        _update_status.update({'running': False, 'complete': False, 'error': True})


# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #


def uninstall(ctx, job, params):
    """Remove ATLAS completely. Nothing of it survives this call.

    ⚠️ **This destroys the device CA and the database, and that is deliberate.**
    Every enrolled tablet's identity is signed by that CA; once it is gone they
    cannot be re-adopted, only factory reset in person. The console asks for a
    password and says so before calling this.

    The alternative — keeping the data "just in case" — was worse in practice:
    an operator who uninstalls expects the box to be as it was, and a leftover
    database silently decided the *next* install's fate, because Postgres only
    honours POSTGRES_PASSWORD on an empty volume. Install regenerates all of it.

    ⚠️ The deploy key is *not* removed. It lives beside the install directory,
    not inside it, and it is the credential for fetching ATLAS rather than any
    part of ATLAS — taking it would make the next install fail at `git clone`
    with nothing on the page to explain why.
    """
    steps = []
    dirpath = atlas_dir(ctx)

    # Volumes go with the containers: `down -v` is the only step that removes
    # the database, and it needs the compose file, so it runs before the
    # directory does.
    if os.path.isdir(dirpath):
        _compose(ctx, 'down -v --remove-orphans', timeout=300)
        steps.append('Containers, network and database volume removed')
    else:
        steps.append('No install directory — nothing to stop')

    r = _run(ctx, ['docker', 'image', 'rm', '-f', 'takmdm-api', 'takmdm-init'])
    steps.append('Images removed' if r else 'Images already absent')

    # The install directory carries the device CA, the bundle signing key, the
    # uploaded artifacts and the generated .env.
    # The obsolete deploy key goes with everything else now. It was kept while
    # the repository was private, because removing it would have failed the next
    # install at `git clone`. A public repository takes no credential, so a
    # private key left on the box is pure liability.
    for stale in _stale_deploy_key(dirpath):
        try:
            os.remove(stale)
            steps.append(f'Obsolete deploy key removed ({os.path.basename(stale)})')
        except OSError:
            pass

    for path, label in ((dirpath, 'Install directory (device CA, artifacts, .env)'),
                        (_caddy_ca_dir(), "Caddy's copy of the device CA")):
        try:
            if path and os.path.isdir(path):
                shutil.rmtree(path)
                steps.append(f'{label} removed')
        except OSError as exc:
            steps.append(f'{label} NOT removed: {exc}')

    ctx['_fw_remove'](DEVICE_PORT, 'tcp')
    steps.append(f'Firewall rule for {DEVICE_PORT}/tcp removed')

    # ⚠️ Every generated value goes. Leaving
    # atlas_pg_password behind would hand the next install a password for a
    # database that no longer exists — harmless only by luck, since the volume
    # it belonged to is gone.
    s = ctx['load_settings']()
    for key in [k for k in list(s) if k.startswith(f'{KEY}_')]:
        s.pop(key, None)
    s[f'{KEY}_enabled'] = False
    ctx['save_settings'](s)
    ctx['generate_caddyfile'](s)
    ctx['_caddy_reload']()
    steps.append('Generated settings cleared and Caddy vhosts removed')

    try:
        ctx['_deregister_authentik_proxy_app'](s, KEY, 'ATLAS MDM Proxy')
        steps.append('ATLAS application removed from Authentik')
    except Exception:
        steps.append('ATLAS application not in Authentik (not configured)')

    return {'success': True, 'steps': steps}


def _caddy_ca_dir():
    """Where app.py stages a Caddy-readable copy of the device CA, or None.

    Mirrors _sync_atlas_device_ca_for_caddy: Caddy runs unprivileged and cannot
    read the install directory, so the certificate is copied into its own home.
    A stale copy left behind would have Caddy verifying client certificates
    against a CA that no longer exists.
    """
    for base in ('/var/lib/caddy', os.path.expanduser('~caddy')):
        candidate = os.path.join(base, KEY)
        if os.path.isdir(candidate):
            return candidate
    return None


def _run(ctx, argv):
    """Best-effort root command; True when it succeeded."""
    try:
        p = subprocess.run(ctx['_sudo_wrap'](list(argv)), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=120)
        return p.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Templates written into the install directory
# --------------------------------------------------------------------------- #

_ENV_TEMPLATE = """# Written by the infra-TAK ATLAS module. Edited by hand at your own risk:
# a re-deploy rewrites this file.
#
# ⚠️ Every name here must appear as ${{...}} in ATLAS's docker-compose.yml.
# There is no `env_file`, so a variable written here and not referenced there
# reaches nothing and fails silently.

# Generated once and kept. Regenerating on a re-deploy would leave the existing
# database unopenable by the application that owns it.
TAKMDM_DB_PASSWORD={pg_password}

# Caddy terminates TLS with a publicly-issued certificate, so provisioning must
# NOT tell devices to pin this deployment's own CA — they would fail the
# handshake at enrolment with nothing on the tablet to explain it.
TAKMDM_INCLUDE_SERVER_CA=0

# What a device is told to talk to: Caddy's device vhost, not the app.
TAKMDM_SERVER_URL={device_url}

# Where a tablet in out-of-box setup fetches the agent. Plain HTTP on the
# well-known port, through Caddy — an Android setup wizard follows no redirect
# and its integrity check is the signature checksum in the QR.
TAKMDM_AGENT_APK_URL={apk_url}

# The console's public origin, for the cross-origin check.
TAKMDM_CONSOLE_ORIGIN={console_url}

# Authentik terminates administrator sign-in and forwards the identity.
TAKMDM_ADMIN_AUTH_MODE=forward_auth

# ⚠️ Deliberately empty: Authentik decides, not ATLAS.
#
# ATLAS can require a group of its own, but on infra-TAK that would mean a
# second place to manage access and a group the operator has never heard of —
# and until somebody created it and added themselves, nobody could sign in at
# all. infra-TAK's access-policy converge is default-deny: the ATLAS
# application it registers is bound to "Allow authentik Admins" because it is
# not on the user-visible allowlist. So the console is admin-only, enforced at
# the identity provider, and blank here means "whoever Authentik let through".
TAKMDM_ADMIN_GROUP=
"""

_COMPOSE_OVERRIDE = """# Written by the infra-TAK ATLAS module.
#
# Caddy is the only thing that talks to this application, so it binds loopback
# and publishes nothing else. ATLAS's own nginx is simply not started — `up -d
# api` brings the database and the migration step with it and stops there.
services:
  api:
    ports:
      - "127.0.0.1:{app_port}:8000"

  # No self-signed server certificate. Caddy holds a publicly-issued one, and a
  # local CA here would end up pinned in provisioning QRs that then fail.
  init:
    command: ["python", "-m", "app.cli", "init-pki"]
"""


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def register(ctx):
    from flask import jsonify

    def logs_view():
        try:
            r = ctx['probe_run'](['docker', 'logs', '--tail', '200', API_CONTAINER],
                                 text=True, timeout=10)
            lines = ((r.stdout or '') + (r.stderr or '')).splitlines()
        except Exception as exc:
            lines = [f'could not read logs: {exc}']
        return jsonify({'lines': lines[-200:]})

    def version_view():
        """What is installed, what is available, and whether that is a newer one.

        ⚠️ `update_available` is only ever True when *both* versions parsed and
        the upstream one is genuinely higher. An unknown latest (offline, or
        GitHub's 60/hour spent) leaves it False and `latest` null, so the page
        can say "could not check" instead of claiming the box is current.
        """
        installed = _installed_version(ctx)
        latest = _latest_version()
        here, there = _parse_version(installed), _parse_version(latest)
        return jsonify({
            'version': installed,
            'latest': latest,
            'update_available': bool(here and there and there > here),
        })

    def update_view():
        import threading
        if _update_status['running']:
            return jsonify({'success': False, 'error': 'An update is already running'})
        threading.Thread(target=_run_update, args=(ctx,), daemon=True).start()
        return jsonify({'success': True})

    def update_status_view():
        return jsonify({
            'running': _update_status['running'],
            'complete': _update_status['complete'],
            'error': _update_status['error'],
            'entries': list(_update_status['log']),
        })

    register_module({
        'key': KEY,
        'name': 'ATLAS MDM',
        'description': 'Android device management for ATAK tablets — policies, apps, enrolment',
        'icon': '\U0001F4F1',  # 📱 as an escape: a literal surrogate pair corrupts on edit
        # ATLAS's own banner, the one its web UI wears. The console and
        # marketplace tiles hide the module name when a logo is present, so
        # this is the wordmark artwork rather than the bare mark.
        'icon_url': '/static/logos/atlas-banner.png',
        'route': '/atlas',
        'template': 'atlas.html',
        'priority': 16,
        'detect': detect,
        'deploy': deploy,
        'deploy_validate': deploy_validate,
        'uninstall': uninstall,
        'control_map': {
            'start':   lambda c: _compose_argv(c, 'up', '-d', 'api'),
            'stop':    lambda c: _compose_argv(c, 'stop'),
            'restart': lambda c: _compose_argv(c, 'restart'),
        },
        'extra_routes': [
            {'url': f'/api/{KEY}/logs', 'methods': ['GET'],
             'endpoint': f'{KEY}_logs', 'view': logs_view},
            {'url': f'/api/{KEY}/version', 'methods': ['GET'],
             'endpoint': f'{KEY}_version', 'view': version_view},
            {'url': f'/api/{KEY}/update', 'methods': ['POST'],
             'endpoint': f'{KEY}_update', 'view': update_view},
            {'url': f'/api/{KEY}/update-status', 'methods': ['GET'],
             'endpoint': f'{KEY}_update_status', 'view': update_status_view},
        ],
        # One public port. The console is Caddy-only on 443, the agent package is
        # a Caddy path route on the well-known port, and the database never
        # leaves the container bridge.
        'ports': [f'{DEVICE_PORT}/tcp'],
        'service_units': [],
        'settings_keys': [
            f'{KEY}_enabled', f'{KEY}_pg_password', f'{KEY}_commit_sha',
            f'{KEY}_version', f'{KEY}_domain',
        ],
    })
