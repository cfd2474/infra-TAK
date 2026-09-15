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
import json
import os
from glob import glob as _glob
import secrets
import shutil
import subprocess
# ⚠️ Module level. `ensure_authentik_app` sleeps between attempts while it
# waits for Authentik's authorization flow; in app.py that name came from a
# module-level import, and moving the function here left it unbound. It only
# fires when the flow is not ready on the first try — a fresh box — so it
# would have waited for the worst possible moment to surface.
import time

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
ATLAS_TAG = 'v1.34.0'
# ⚠️ The **commit**, not the tag object. `v0.1.0` is an annotated tag, so
# `git rev-parse v0.1.0` returns the tag object's own SHA while a clone's HEAD
# is the commit it points at — two different hashes, and comparing them made
# every deploy refuse itself. `git rev-parse 'v0.1.0^{}'` is the one to record.
ATLAS_SHA = '2e4f87f053c3250fe3383842b22af180c079940a'

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


#: What we fall back to when the Docker network does not exist yet.
#:
#: ⚠️ On a **fresh install** it always does not exist: `.env` is written before
#: `docker compose up` creates the network, so detection cannot work at that
#: point. This value keeps the install safe — it still refuses a request arriving
#: from a public address — and `_set_trusted_proxies` narrows it to the real subnet
#: once the network is there.
_TRUSTED_FALLBACK = '172.16.0.0/12,10.0.0.0/8,192.168.0.0/16,127.0.0.0/8'


def _bridge_gateway():
    """The address Caddy appears as from inside the container.

    Caddy runs on the host and reaches the application through Docker's bridge,
    so the peer the application sees is the network's gateway — 172.24.0.1 on the
    reference box, but the subnet is assigned by Docker and differs per host.

    ⚠️ **Falls back to the whole RFC1918 space rather than to nothing.** A wrong
    guess here locks an operator out of their own console; a broad value still
    refuses a request arriving from a public address, which is the case worth
    closing. The narrow value is an optimisation, not the control.

    ⚠️ Asked of the *network*, not of a running container: on a first install
    the network exists before the application does, and on a redeploy the
    container may be down.
    """
    try:
        r = subprocess.run(
            ['docker', 'network', 'inspect', '-f',
             '{{range .IPAM.Config}}{{.Subnet}}{{end}}', 'takmdm_default'],
            capture_output=True, text=True, timeout=30,
        )
        subnet = (r.stdout or '').strip()
        if r.returncode == 0 and '/' in subnet:
            return subnet
    except Exception:
        pass
    return _TRUSTED_FALLBACK


def _set_trusted_proxies(dirpath, plog):
    """Make `.env` name the addresses the admin interface answers. Returns True
    when it changed, so the caller knows the container needs recreating.

    Handles three states, and the third is the one a fresh install lands in:

    * **Absent** — a box installed before this existed. Appended. ⚠️ The update
      path does not rewrite `.env`, so without this such a box would keep
      accepting an identity header from anywhere for ever.
    * **The fallback** — written at install time, when the Docker network did not
      exist yet and the subnet could not be read. Narrowed to the real one now
      that it can.
    * **Anything else** — left alone. That is either an operator's own value or a
      subnet we already detected, and neither is ours to overwrite.
    """
    env_path = os.path.join(dirpath, '.env')
    try:
        with open(env_path, 'r') as f:
            body = f.read()
    except OSError:
        return False

    detected = _bridge_gateway()

    if 'TAKMDM_TRUSTED_PROXIES' not in body:
        with open(env_path, 'a') as f:
            f.write('\n# Added by the ATLAS module (SEC_AUDIT.md S-1).\n')
            f.write('TAKMDM_TRUSTED_PROXIES=%s\n' % detected)
        plog('✓ Restricted the admin interface to %s' % detected)
        return True

    # ⚠️ Only ever the exact fallback string is replaced. Matching loosely — on
    # the key alone, say — would overwrite a value an operator had narrowed or
    # widened deliberately, through a deploy they ran for an unrelated reason.
    stale = 'TAKMDM_TRUSTED_PROXIES=%s' % _TRUSTED_FALLBACK
    if stale in body and detected != _TRUSTED_FALLBACK:
        body = body.replace(stale, 'TAKMDM_TRUSTED_PROXIES=%s' % detected, 1)
        with open(env_path, 'w') as f:
            f.write(body)
        plog('✓ Narrowed the admin interface to %s' % detected)
        return True

    return False


#: Settings keys recording whether Authentik still restricts the ATLAS app.
ACCESS_KEY = KEY + '_access_restricted'
ACCESS_CHECKED_KEY = KEY + '_access_checked_at'


def _record_access_state(ctx, restricted, plog=None):
    """Remember whether the ATLAS application is bound to an access policy.

    ⚠️ **Stored rather than probed on demand.** `detect()` runs on every dashboard
    poll, from several threads, and must answer in under a second — an Authentik
    API call there would make the whole console's tile refresh depend on
    Authentik's latency, and a slow identity provider would read as ATLAS being
    down. So the expensive check happens where there is already a job and a log:
    deploy and update.
    """
    import time as _time
    try:
        s = ctx['load_settings']()
        s[ACCESS_KEY] = bool(restricted)
        s[ACCESS_CHECKED_KEY] = int(_time.time())
        ctx['save_settings'](s)
    except Exception as exc:
        if plog:
            plog('  ⚠ Could not record the access-control state: %s' % str(exc)[:80])
        return restricted

    if plog:
        if restricted:
            plog('  ✓ Verified: the ATLAS application is restricted to Authentik admins')
        else:
            plog('  ✗ ATLAS IS NOT ACCESS-RESTRICTED. Every authenticated Authentik')
            plog('    user can reach the console — remote wipe, factory reset, policy')
            plog('    push. Fix: Authentik → Reconfigure, then redeploy ATLAS.')
    return restricted


def _verify_access_control(ctx, plog=None):
    """Re-run the binding check outside a deploy. Returns True/False/None.

    None means the question could not be asked — no Authentik, no token, no
    network. ⚠️ That is deliberately not the same as False: reporting "not
    restricted" because Authentik was briefly unreachable would train an operator
    to ignore the one message that matters.
    """
    try:
        s = ctx['load_settings']()
        fqdn = ctx['_get_authentik_env_value'](s, 'AUTHENTIK_FQDN') or ''
        token = (ctx['_get_authentik_env_value'](s, 'AUTHENTIK_TOKEN') or
                 s.get('authentik_api_token') or '')
        if not fqdn or not token:
            return None
        ak_url = ctx['_get_authentik_api_url'](s)
        headers = {'Authorization': 'Bearer %s' % token,
                   'Content-Type': 'application/json'}
        return _record_access_state(
            ctx, _restrict_to_admins(ak_url, headers, plog=plog), plog=plog)
    except Exception as exc:
        if plog:
            plog('  ⚠ Could not verify access control: %s' % str(exc)[:80])
        return None


def _pki_dir():
    """Where this install keeps its PKI, or None if it is not here."""
    for base in ('/root/atlas', os.path.expanduser('~/atlas')):
        candidate = os.path.join(base, 'pki')
        if os.path.isdir(candidate):
            return candidate
    return None


def _write_root_key(path, pem):
    """Put the root key on disk for the length of one command.

    ⚠️ Created 0600 by `os.open`, not written and chmod'd after — the same
    reasoning as ATLAS's own key writer. A world-readable window on *this* key is
    the worst one in the system.

    Owned by the container's uid, or the application cannot read what it was
    given and the ceremony fails with a permission error nobody expects.
    """
    body = pem if pem.endswith('\n') else pem + '\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    try:
        os.chown(path, APP_UID, APP_GID)
    except Exception:
        pass


def _shred(path):
    """Remove the root key, overwriting it first. Returns True when it is gone.

    ⚠️ **Overwriting is not secure erasure** and is not claimed to be: on a
    journaling or copy-on-write filesystem the original blocks may survive. It is
    done because it costs nothing and raises the bar slightly. The honest position
    is that the root key touched this machine, which is a moment of exposure the
    ceremony cannot avoid — only shorten.
    """
    try:
        if os.path.exists(path):
            size = os.path.getsize(path)
            with open(path, 'r+b') as fh:
                fh.write(b'\0' * size)
                fh.flush()
                os.fsync(fh.fileno())
            os.unlink(path)
        return not os.path.exists(path)
    except Exception as exc:
        print('[' + KEY + '] could not remove the root key: ' + str(exc), flush=True)
        return False


def _compose_exec(ctx, argv, timeout=60, stdin=None):
    """Run a command inside the API container. Returns its output, or None.

    ⚠️ `stdin` exists so the recovery file can be checked **without ever being
    written to this host's disk**. `docker exec -i` pipes it straight into the
    process; the alternative — a temp file plus a shred afterwards — puts the
    root key on the filesystem for the length of a command, which is the exposure
    this whole feature exists to remove.
    """
    argv = list(argv)
    flags = ['-i'] if stdin is not None else []
    try:
        r = subprocess.run(
            ['docker', 'exec'] + flags + [API_CONTAINER] + argv,
            capture_output=True, text=True, timeout=timeout,
            input=stdin if stdin is not None else None,
        )
    except Exception:
        return None
    if r.returncode != 0 and not (r.stdout or '').strip():
        return None
    return (r.stdout or '') + (r.stderr or '')


def _compose_exec_rc(ctx, argv, timeout=60, stdin=None):
    """Same, but the caller needs the exit status rather than the text.

    `_compose_exec` collapses failure into None, which suits a status read and
    not a yes/no question: `ca-verify-root` answers by exit code, and "did not
    run" must not look like "the key does not match".
    """
    argv = list(argv)
    flags = ['-i'] if stdin is not None else []
    try:
        r = subprocess.run(
            ['docker', 'exec'] + flags + [API_CONTAINER] + argv,
            capture_output=True, text=True, timeout=timeout,
            input=stdin if stdin is not None else None,
        )
    except Exception as exc:
        return None, str(exc)
    return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()


# ⚠️ Imported rather than re-implemented. `modules/__init__.py` calls this "the
# 12-copy pattern, one copy" — adding a thirteenth is how a security check drifts.
# It is the module package this file lives in, not `app.py`, so rule 10 holds.
from modules import _check_admin_password  # noqa: E402


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
    # ⚠️ Read from settings, never probed here. This function runs on every
    # dashboard poll from several threads and must answer in under a second; an
    # Authentik round-trip would make ATLAS's tile report Authentik's health.
    # False is only ever written by a check that actually ran (H-1).
    restricted = s.get(ACCESS_KEY)

    return {'installed': enabled, 'running': running,
            'version': _installed_version(ctx) if enabled else None,
            'access_restricted': restricted,
            'warning': (None if restricted is not False else
                        'Not access-restricted: every Authentik user can reach '
                        'this console. Re-run Authentik → Reconfigure, then '
                        'redeploy ATLAS.')}


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
            trusted_proxies=_bridge_gateway(),
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

        # ⚠️ Only now can the bridge subnet be read — `docker compose up` is what
        # creates the network, and `.env` was written before it existed. So a fresh
        # install starts on the broad fallback and is narrowed here, on the same
        # deploy, rather than staying wide until somebody happens to update
        # (SEC_AUDIT.md S-1).
        if _set_trusted_proxies(dirpath, plog):
            r2 = _compose(ctx, 'up -d api', timeout=600)
            if r2.returncode != 0:
                plog('  ⚠ Could not restart with the narrowed range; it applies on '
                     'the next update')

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

        # ── The certificate authority is split before anyone can use it ──────
        #
        # ⚠️ Automatic, because a manual ceremony is not a control. The previous
        # design asked the operator to run five commands over SSH; SEC_AUDIT S-2
        # stayed Severe for months because nobody did, and W185 found the console
        # telling the ones who half-finished that they were done.
        #
        # This leaves the root key **on the box** — the card is what gets it off.
        # Issuing here is what makes that possible: once an intermediate signs,
        # removing the root costs nothing and breaks nothing.
        plog('')
        plog('━━━ Securing the certificate authority ━━━')
        out = _compose_exec(
            ctx, ['python', '-m', 'app.cli', 'ca-issue-intermediate'], timeout=120,
        )
        if out and 'issuing CA' in out:
            plog('✓ Issuing certificate created — the root key is now only needed')
            plog('  to renew it, about twice a decade.')
            plog('⚠ The root key is still on this server. Open the ATLAS module')
            plog('  page and save your recovery file — it takes one click.')
        else:
            # Not fatal. A deployment with an unsplit CA works exactly as it
            # always did; it is simply still carrying the risk.
            plog('⚠ Could not create the issuing certificate. ATLAS works, but the')
            plog('  root key cannot be moved off this server until it exists.')

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
            ensure_authentik_app(ctx, fqdn, token, plog=plog, settings=s)
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
# Authentik, and the CA Caddy needs
#
# ⚠️ These live here rather than in app.py. They are ATLAS's own logic — how
# ATLAS registers itself with Authentik, how it is restricted to
# administrators, and where its device CA has to be copied for Caddy to read.
# Nothing else in infra-TAK calls them, so nothing else should carry them: the
# module adapts to the console, not the other way round.
#
# What they *do* need from the console arrives through ctx, which is the
# sanctioned direction (rule 10: a module imports nothing from app.py).
# --------------------------------------------------------------------------- #

def sync_device_ca_for_caddy():
    """Deploy a Caddy-readable copy of ATLAS's device CA; return its path or None.

    ATLAS issues its own client certificates to enrolled tablets, and Caddy has
    to verify them at the device listener. The CA lives in the module's install
    directory, which is root-owned — and Caddy runs as the unprivileged `caddy`
    user, so pointing `client_auth` there makes Caddy fail to start. Same
    problem, same answer, as the custom-certificate copy below.

    ⚠️ Only the *certificate* is copied. The CA private key stays where it is:
    Caddy needs to verify signatures, which takes the public half alone, and a
    copy of the key readable by a web server is a fleet's device identity one
    file-read away.
    """
    import shutil, pwd
    for base_dir in ('/root/atlas', os.path.expanduser('~/atlas')):
        src = os.path.join(base_dir, 'pki', 'ca.crt')
        if os.path.exists(src):
            break
    else:
        return None

    # ⚠️ Root **plus every intermediate**, not just `ca.crt` (ATLAS W172).
    #
    # Once the root is taken offline, devices are issued by an intermediate and
    # present a certificate Caddy cannot verify from the root alone. Caddy's
    # `trust_pool file` reads a bundle, so they concatenate — and retired
    # intermediates stay in it, because the certificates they signed are valid
    # until they expire and those devices chain through them.
    #
    # Only certificates. The private halves never leave the install directory:
    # verification takes the public half, and a key readable by a web server is a
    # fleet's identity one file-read away.
    pki_dir = os.path.join(base_dir, 'pki')
    bundle_parts = []
    for candidate in ([os.path.join(pki_dir, 'ca.crt'),
                       os.path.join(pki_dir, 'issuing.crt')] +
                      sorted(_glob(os.path.join(pki_dir, 'retired', '*.crt')))):
        try:
            with open(candidate, 'r') as fh:
                text = fh.read().strip()
            if text and text not in bundle_parts:
                bundle_parts.append(text)
        except OSError:
            continue
    if not bundle_parts:
        return None
    try:
        try:
            caddy_pw = pwd.getpwnam('caddy')
            base = caddy_pw.pw_dir if os.path.isdir(caddy_pw.pw_dir) else '/var/lib/caddy'
        except KeyError:
            caddy_pw = None
            base = '/var/lib/caddy'
        dest_dir = os.path.join(base, KEY)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, 'device-ca.crt')
        with open(dest, 'w') as fh:
            fh.write('\n'.join(bundle_parts) + '\n')
        os.chmod(dest, 0o644)
        if caddy_pw:
            os.chown(dest_dir, caddy_pw.pw_uid, caddy_pw.pw_gid)
            os.chown(dest, caddy_pw.pw_uid, caddy_pw.pw_gid)
        return dest
    except Exception as exc:
        print('[' + KEY + '] could not stage device CA for Caddy: ' + str(exc), flush=True)
        return None


def _restrict_to_admins(ak_url, ak_headers, plog=None):
    """Bind "Allow authentik Admins" to the ATLAS application. Returns True when
    the application is restricted, False when it is NOT.

    ⚠️ This cannot wait for the startup access-policy converge. That converge
    is default-deny and would catch ATLAS eventually — but it runs when the
    console *boots*, and a module deploy does not reboot the console. In between,
    the application exists with no binding at all, and ATLAS is configured with an
    empty admin group precisely because it trusts Authentik to decide. That
    combination is an MDM console — remote wipe, factory reset, policy push —
    reachable by every authenticated user on the box, for however long it takes
    somebody to restart the console. Observed live before this existed.

    A failure here is reported as a failure. An access control that quietly did
    not apply is worse than one never attempted, because the deploy log would
    say the console is admin-only.
    """
    import urllib.request as _urlreq
    import urllib.error

    def log(msg):
        if plog:
            plog(msg)

    def _get(path):
        req = _urlreq.Request(f'{ak_url}/api/v3/{path}', headers=ak_headers)
        return json.loads(_urlreq.urlopen(req, timeout=10).read().decode())

    policy_name = 'Allow authentik Admins'
    try:
        app_pk = _get('core/applications/atlas/')['pk']

        policy_pk = None
        for p in _get('policies/all/?page_size=200').get('results', []):
            if p.get('name') == policy_name:
                policy_pk = p.get('pk')
                break
        if not policy_pk:
            log(f"  ✗ ATLAS is NOT restricted: no {policy_name!r} policy exists. "
                f"Run Authentik → Reconfigure, then bind it to the ATLAS MDM "
                f"application by hand.")
            return False

        bindings = _get(f'policies/bindings/?target={app_pk}&page_size=100')['results']
        if any(str(b.get('policy')) == str(policy_pk) or
               (b.get('policy_obj', {}) or {}).get('name') == policy_name
               for b in bindings):
            log("  ✓ Console restricted to Authentik administrators (already bound)")
            return True

        req = _urlreq.Request(f'{ak_url}/api/v3/policies/bindings/',
            data=json.dumps({'target': app_pk, 'policy': policy_pk,
                             'order': 0, 'negate': False, 'enabled': True,
                             'timeout': 30}).encode(),
            headers=ak_headers, method='POST')
        _urlreq.urlopen(req, timeout=10)
        log("  ✓ Console restricted to Authentik administrators")
        return True
    except Exception as e:
        log(f"  ✗ ATLAS is NOT restricted — every authenticated user can reach "
            f"the console ({str(e)[:80]}). Bind {policy_name!r} to the ATLAS MDM "
            f"application in Authentik before using this deployment.")
        return False


def ensure_authentik_app(ctx, fqdn, ak_token, plog=None, flow_pk=None, inv_flow_pk=None, settings=None):
    """Create the ATLAS MDM proxy provider + application in Authentik.

    Same pattern as the TAK Video Restreamer: Caddy's forward_auth protects
    atlas.FQDN and the embedded outpost decides who gets in.

    ⚠️ Creating the application is what makes ATLAS admin-only. The startup
    access-policy converge is default-deny — anything not on the user-visible
    allowlist gets bound to "Allow authentik Admins" — so an MDM console that
    can factory-reset a fleet is restricted without a policy written here. A
    deployment that skipped this step would not be "open": ATLAS would answer
    401 to everyone forever, because nothing would ever set the identity
    headers it reads.
    """
    if not fqdn or not ak_token:
        return False
    def log(msg):
        if plog:
            plog(msg)
    import urllib.request as _urlreq
    import urllib.error
    _ak_headers = {'Authorization': f'Bearer {ak_token}', 'Content-Type': 'application/json'}
    _ak_url = ctx['_get_authentik_api_url'](settings) if settings else 'http://127.0.0.1:9090'

    try:
        if not flow_pk or not inv_flow_pk:
            for attempt in range(36):
                try:
                    req = _urlreq.Request(f'{_ak_url}/api/v3/flows/instances/?designation=authorization&ordering=slug', headers=_ak_headers)
                    resp = _urlreq.urlopen(req, timeout=10)
                    flows = json.loads(resp.read().decode())['results']
                    flow_pk = next((f['pk'] for f in flows if 'implicit' in f.get('slug', '')), flows[0]['pk'] if flows else None)
                    if flow_pk:
                        req = _urlreq.Request(f'{_ak_url}/api/v3/flows/instances/?designation=invalidation', headers=_ak_headers)
                        resp = _urlreq.urlopen(req, timeout=10)
                        inv_flows = json.loads(resp.read().decode())['results']
                        inv_flow_pk = next((f['pk'] for f in inv_flows if 'provider' not in f.get('slug', '')), inv_flows[0]['pk'] if inv_flows else None)
                        if inv_flow_pk:
                            break
                except Exception:
                    pass
                if attempt % 6 == 0:
                    log(f"  ⏳ Waiting for authorization flow... ({attempt * 5}s)")
                time.sleep(5)
            if not flow_pk or not inv_flow_pk:
                log("  ⚠ No authorization/invalidation flow — skipping ATLAS proxy provider")
                return False

        provider_pk = None
        # ⚠️ Built in statements, not inside an f-string. Nesting the same quote
        # character inside an f-string is Python 3.12 syntax (PEP 701) and a
        # SyntaxError on the 3.10 that Ubuntu 22.04 ships — which would stop this
        # module importing at all, taking the tile with it.
        if settings:
            _host_name = ctx['_get_service_domain'](settings, KEY)
        else:
            _host_name = 'atlas.' + fqdn
        _atlas_host = 'https://' + _host_name
        _cookie = f'.{fqdn.split(":")[0]}'
        try:
            req = _urlreq.Request(f'{_ak_url}/api/v3/providers/proxy/',
                data=json.dumps({'name': 'ATLAS MDM Proxy', 'authorization_flow': flow_pk,
                    'invalidation_flow': inv_flow_pk,
                    'external_host': _atlas_host, 'mode': 'forward_single',
                    'token_validity': 'hours=24', 'cookie_domain': _cookie}).encode(),
                headers=_ak_headers, method='POST')
            resp = _urlreq.urlopen(req, timeout=10)
            provider_pk = json.loads(resp.read().decode())['pk']
            log("  ✓ Proxy provider created")
        except Exception as e:
            if hasattr(e, 'code') and e.code == 400:
                req = _urlreq.Request(f'{_ak_url}/api/v3/providers/proxy/?search=ATLAS+MDM', headers=_ak_headers)
                resp = _urlreq.urlopen(req, timeout=10)
                results = json.loads(resp.read().decode())['results']
                if results:
                    provider_pk = results[0]['pk']
                    try:
                        req = _urlreq.Request(f'{_ak_url}/api/v3/providers/proxy/{provider_pk}/',
                            data=json.dumps({'external_host': _atlas_host, 'cookie_domain': _cookie}).encode(),
                            headers=_ak_headers, method='PATCH')
                        _urlreq.urlopen(req, timeout=10)
                    except Exception:
                        pass
                log("  ✓ Proxy provider already exists (external_host updated)")
            else:
                log(f"  ⚠ Proxy provider error: {str(e)[:100]}")

        if provider_pk:
            try:
                req = _urlreq.Request(f'{_ak_url}/api/v3/core/applications/',
                    data=json.dumps({'name': 'ATLAS MDM', 'slug': 'atlas',
                        'provider': provider_pk, 'open_in_new_tab': True}).encode(),
                    headers=_ak_headers, method='POST')
                _urlreq.urlopen(req, timeout=10)
                log("  ✓ Application 'ATLAS MDM' created")
            except Exception as e:
                if hasattr(e, 'code') and e.code == 400:
                    try:
                        req = _urlreq.Request(f'{_ak_url}/api/v3/core/applications/atlas/',
                            data=json.dumps({'provider': provider_pk, 'open_in_new_tab': True}).encode(),
                            headers=_ak_headers, method='PATCH')
                        _urlreq.urlopen(req, timeout=10)
                    except Exception:
                        pass
                    log("  ✓ Application 'ATLAS MDM' updated")
                else:
                    log(f"  ⚠ Application error: {str(e)[:80]}")

            ctx['_outpost_add_providers_safe'](_ak_url, _ak_headers, [provider_pk], plog=log)
            ctx['_authentik_application_open_in_new_tab'](_ak_url, _ak_headers, 'atlas', plog=log)
            # ⚠️ The answer is recorded, not discarded (SEC_AUDIT.md H-1). ATLAS
            # runs with an empty admin group — it trusts Authentik to decide who is
            # an administrator — so this binding *is* the access control. It was
            # once found absent on a live box, and nothing noticed. Now the result
            # is stored where the tile and the next update can see it.
            _record_access_state(
                ctx, _restrict_to_admins(_ak_url, _ak_headers, plog=log), plog=log)
        else:
            log("  ⚠ Could not create or find the ATLAS proxy provider")
    except Exception as e:
        log(f"  ⚠ Forward auth setup error: {str(e)[:100]}")
    return True


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


def _running_version():
    """The version the container reports, or None if it cannot be asked.

    ⚠️ This is the one that matters for "did the update work". The checkout and
    the process can disagree: `docker compose up -d` without `--build` keeps the
    old image, so `VERSION` can read 1.3.0 while the running app still serves
    1.0.0. Reading the repo alone reports a successful deploy that never
    happened.

    Loopback and unauthenticated by design — the console runs beside the
    container, not through Authentik.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(
            'http://127.0.0.1:%d/version' % APP_PORT, timeout=5
        ) as response:
            body = json.loads(response.read().decode())
    except Exception:
        return None
    if body.get('service') != 'atlas-mdm':
        # Something else is answering on that port; its version means nothing here.
        return None
    return (body.get('version') or '').strip().lstrip('vV') or None


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


def get_version_info(ctx):
    """`{version, update_available, latest}` — the shape the console cards read.

    ⚠️ The dashboard is where an operator actually notices an update; the
    module's own page is somewhere they go only once they already suspect one.
    `get_all_module_versions()` is what fills those cards, and a module absent
    from it simply shows no badge — silently, because nothing is broken.

    ⚠️ `update_available` is true only when *both* versions parsed and upstream
    is genuinely higher. An unreachable GitHub leaves `latest` null and the flag
    false, so a rate-limited box is never told it is current — it is told
    nothing, which the card renders as no badge rather than a reassuring one.
    """
    # ⚠️ The running container first, the checkout second. They agree on a
    # healthy deployment; when they do not, the running one is the truth and the
    # difference is a rebuild that did not take.
    checked_out = _installed_version(ctx)
    running = _running_version()
    installed = running or checked_out
    latest = _latest_version()
    here, there = _parse_version(installed), _parse_version(latest)

    if there and not here:
        # ⚠️ An install with no readable VERSION predates the file itself, which
        # arrived in 1.0.0 — so it is older than any release we can see, and it
        # is precisely the deployment that most needs telling. Reporting "no
        # update" here would leave the oldest boxes the quietest.
        update = bool(installed is None or not installed)
    else:
        update = bool(here and there and there > here)

    info = {'version': installed or '', 'latest': latest, 'update_available': update}
    # Surfaced rather than hidden: a checkout ahead of the process means the last
    # rebuild did not take, and an operator reading only the version would see a
    # number that is not what is serving their fleet.
    if running and checked_out and running != checked_out:
        info['stale_image'] = True
        info['checked_out'] = checked_out
    return info


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

        # ⚠️ Before the rebuild, or the new image starts without the setting and
        # spends a release accepting identity headers from anywhere.
        _set_trusted_proxies(dirpath, plog)

        plog('━━━ Step 2/3: Rebuilding ━━━')
        # ⚠️ No `-v` anywhere here. `down -v` would take the database and the
        # device CA with it, and every enrolled tablet would need a factory reset.
        r = _compose(ctx, 'up -d --build api', timeout=1800)
        if r.returncode != 0:
            raise RuntimeError('docker compose up failed: ' + (r.stderr or '')[-500:])
        plog('✓ Containers rebuilt — database and device CA untouched')
        plog('  The agent and launcher from this release load on start, and the')
        plog('  new agent is offered to the fleet on each device\'s next check-in.')

        # ⚠️ Re-asked on every update, because the binding can disappear long
        # after the deploy that made it — an Authentik restore, or somebody
        # unbinding the policy. A check that only runs at install answers a
        # question about the past (H-1).
        _verify_access_control(ctx, plog=plog)

        # ⚠️ Re-emit the vhost. `deploy` does this and `update` did not, so a
        # change to what ATLAS's Caddy block contains reached the box and then
        # sat there: the operator updates, nothing regenerates, and the new
        # directive only appears if somebody happens to redeploy. That is how
        # the upload body limit (SEC_AUDIT M-1) would have shipped inert.
        #
        # Regenerating is what deploy already does and is idempotent — the file
        # is built from current settings either way.
        try:
            ctx['generate_caddyfile'](ctx['load_settings']())
            ctx['_caddy_reload'](plog)
            plog('✓ Caddy vhost re-emitted')
        except Exception as exc:
            # Not fatal. The containers are already rebuilt and serving; a stale
            # vhost is worse reported than turned into a failed update.
            plog('⚠ Could not re-emit the Caddy vhost: ' + str(exc))

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

    Mirrors sync_device_ca_for_caddy above: Caddy runs unprivileged and cannot
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

# Where the administrative interface may be reached from (SEC_AUDIT.md S-1).
#
# ATLAS reads the administrator's identity out of the headers Caddy sets after
# forward_auth. Nothing in those headers proves they came from Caddy, so anything
# able to open a socket to this application's port is an administrator by sending
# two headers. This bounds who that can be.
#
# The value is the Docker bridge gateway: Caddy runs on the host and reaches the
# container through it, so that is the address the application actually observes.
#
# ⚠️ **This cannot tell Caddy from anything else on this host** — every host
# process arrives from the same gateway. It closes the case where the port is
# republished on 0.0.0.0 and reached from somewhere else. Host-local forgery needs
# the proxy to prove it is the proxy, which is what infra-TAK's own
# X-Infratak-Proxy-Auth secret does for the console and does not yet offer to
# module vhosts.
#
# Set it to `any` to switch the check off. ATLAS logs the address it refused and
# the ranges it allows, so a wrong value here is one log line from a fix.
TAKMDM_TRUSTED_PROXIES={trusted_proxies}
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
    from flask import jsonify, request

    def logs_view():
        try:
            r = ctx['probe_run'](['docker', 'logs', '--tail', '200', API_CONTAINER],
                                 text=True, timeout=10)
            lines = ((r.stdout or '') + (r.stderr or '')).splitlines()
        except Exception as exc:
            lines = [f'could not read logs: {exc}']
        return jsonify({'lines': lines[-200:]})

    def ca_view():
        """What the certificate authority looks like, for the page to render."""
        r = _compose_exec(ctx, ['python', '-m', 'app.cli', 'ca-status'])
        if r is None:
            return jsonify({'ok': False, 'error': 'ATLAS is not running'}), 200
        try:
            return jsonify(json.loads(r))
        except Exception:
            return jsonify({'ok': False, 'error': 'could not read the CA status'}), 200

    def ca_renew_view():
        """Run the intermediate ceremony: take the root key, use it, destroy it.

        ⚠️ **This handles the most dangerous secret in the system.** The root key
        arrives in a request body, is written to disk for the length of one
        command, and is removed in a `finally`. That is a real moment of exposure
        and it is inherent to the ceremony — the alternative is an operator doing
        the same thing by hand over SSH, which exposes it just as much and has no
        guarantee the cleanup happens at all.
        """
        data = request.get_json(silent=True) or {}
        err = _check_admin_password(ctx, data)
        if err:
            return jsonify({'success': False, 'error': err}), 403

        key_pem = (data.get('root_key') or '').strip()
        days = data.get('days') or 1825
        try:
            days = int(days)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'days must be a number'}), 400
        if days < 1 or days > 7300:
            return jsonify({'success': False, 'error': 'days must be 1-7300'}), 400

        pki = _pki_dir()
        if pki is None:
            return jsonify({'success': False, 'error': 'ATLAS is not installed here'}), 404
        key_path = os.path.join(pki, 'ca.key')

        # ⚠️ A root key already present is the legacy state, not an error — the
        # ceremony is exactly how it stops being present. But it must not be
        # silently replaced by whatever was pasted: that would be a way to swap the
        # CA of a running fleet through a web form.
        supplied = False
        if key_pem:
            if os.path.exists(key_path):
                return jsonify({
                    'success': False,
                    'error': 'a root key is already on the server; leave the field '
                             'empty to use it, or remove it first',
                }), 409
            if 'PRIVATE KEY' not in key_pem:
                return jsonify({'success': False, 'error': 'that is not a PEM private key'}), 400
            try:
                _write_root_key(key_path, key_pem)
                supplied = True
            except Exception as exc:
                return jsonify({'success': False, 'error': 'could not stage the key: %s' % exc}), 500
        elif not os.path.exists(key_path):
            return jsonify({
                'success': False,
                'error': 'no root key on the server and none supplied',
            }), 400

        steps = []
        try:
            out = _compose_exec(
                ctx,
                ['python', '-m', 'app.cli', 'ca-issue-intermediate', '--days', str(days)],
                timeout=120,
            )
            if out is None:
                return jsonify({'success': False, 'error': 'ATLAS is not running'}), 409
            # ⚠️ Never echo the command's whole output back without looking: it is
            # written for a terminal and names file paths, which is fine, but the
            # key must never appear. It does not — the CLI prints paths, not
            # contents — and this is the line that has to stay true.
            steps.append(out.strip())
            issued = 'issuing CA' in out
        finally:
            # ⚠️ Always, on every path, including the failure ones. An operator who
            # supplied a root key and got an error must not be left with it sitting
            # on the server — that is precisely the state the whole exercise exists
            # to avoid, reached by trying to fix it.
            if supplied:
                removed = _shred(key_path)
                steps.append('Root key removed from the server' if removed
                             else '⚠ COULD NOT REMOVE %s — delete it by hand NOW' % key_path)

        if not issued:
            return jsonify({'success': False, 'error': 'the command did not issue a '
                                                       'certificate', 'steps': steps}), 500

        # The new trust bundle has to reach Caddy or devices fail at the edge.
        staged = sync_device_ca_for_caddy()
        steps.append('Trust bundle staged for Caddy' if staged
                     else '⚠ Could not stage the trust bundle for Caddy')
        ctx['_caddy_reload']()

        r = _compose(ctx, 'restart api', timeout=180)
        steps.append('ATLAS restarted' if r.returncode == 0 else '⚠ Restart failed')
        return jsonify({'success': True, 'steps': steps})

    def ca_recovery_view():
        """Hand the root key to the operator, once, to save.

        ⚠️ **POST with the console password, not a GET.** `login_required`
        already gates it, but the most dangerous secret in the system should not
        be one URL away from an open tab — and a GET would land in browser
        history, in the access log, and in anything that prefetches links. The
        key goes in a JSON body the page turns into a download client-side.
        """
        data = request.get_json(silent=True) or {}
        err = _check_admin_password(ctx, data)
        if err:
            return jsonify({'success': False, 'error': err}), 403

        rc, out = _compose_exec_rc(ctx, ['python', '-m', 'app.cli', 'ca-export-root'])
        if rc is None:
            return jsonify({'success': False, 'error': 'ATLAS is not running'}), 409
        if rc != 0:
            return jsonify({'success': False, 'error': out or 'no root key on this server'}), 409
        if 'PRIVATE KEY' not in out:
            return jsonify({'success': False, 'error': 'that did not look like a key'}), 500

        return jsonify({
            'success': True,
            'key_pem': out,
            'filename': 'atlas-recovery-%s.key' % (ctx['load_settings']().get('fqdn') or 'server'),
        })

    def ca_recovery_confirm_view():
        """Check the operator really has the file, then remove it from the box.

        ⚠️ **Verify and delete are one call on purpose.** Two endpoints would
        allow a verified-but-not-deleted state, which is exactly the half-finished
        ceremony W185 found the console misreporting. Either the customer proves
        they hold the recovery file and the root goes, or nothing changes.
        """
        data = request.get_json(silent=True) or {}
        err = _check_admin_password(ctx, data)
        if err:
            return jsonify({'success': False, 'error': err}), 403

        key_pem = (data.get('root_key') or '').strip()
        if not key_pem:
            return jsonify({'success': False, 'error': 'upload your recovery file first'}), 400

        steps = []
        rc, out = _compose_exec_rc(
            ctx, ['python', '-m', 'app.cli', 'ca-verify-root'],
            stdin=key_pem if key_pem.endswith('\n') else key_pem + '\n',
        )
        if rc is None:
            return jsonify({'success': False, 'error': 'ATLAS is not running'}), 409
        if rc != 0:
            # ⚠️ The root is untouched on this path, and that is the point. A
            # customer who uploads the wrong file must end up exactly where they
            # started, with a message that says which mistake they made.
            return jsonify({'success': False, 'error': out or 'that file does not match'}), 400
        steps.append('✓ Recovery file checked against this server\'s certificate authority')

        # "Check my recovery file", years later, when the root is long gone.
        # ⚠️ Stops here deliberately. Running the delete would be a no-op and
        # would report a removal that did not happen in this call.
        if data.get('verify_only'):
            steps.append('This file can still renew your certificate authority.')
            steps.append('Nothing was changed.')
            return jsonify({'success': True, 'steps': steps})

        rc, out = _compose_exec_rc(
            ctx, ['python', '-m', 'app.cli', 'ca-delete-root'], timeout=60,
        )
        if rc is None:
            return jsonify({'success': False, 'error': 'ATLAS stopped responding',
                            'steps': steps}), 409
        if rc != 0:
            return jsonify({'success': False, 'error': out or 'could not remove the key',
                            'steps': steps}), 500
        steps.append('✓ Root key removed from this server')
        steps.append('Nothing on any device changes. Keep the file somewhere safe —')
        steps.append('you will need it to renew, in about five years.')
        return jsonify({'success': True, 'steps': steps})

    def version_view():
        """What is installed, what is available, and whether that is a newer one.

        ⚠️ `update_available` is only ever True when *both* versions parsed and
        the upstream one is genuinely higher. An unknown latest (offline, or
        GitHub's 60/hour spent) leaves it False and `latest` null, so the page
        can say "could not check" instead of claiming the box is current.
        """
        info = get_version_info(ctx)
        # The page distinguishes "no version" from an empty string; the cards
        # want a string. One source of truth, one conversion.
        return jsonify({
            'version': info['version'] or None,
            'latest': info['latest'],
            'update_available': info['update_available'],
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
            {'url': f'/api/{KEY}/ca', 'methods': ['GET'],
             'endpoint': f'{KEY}_ca', 'view': ca_view},
            {'url': f'/api/{KEY}/ca/renew', 'methods': ['POST'],
             'endpoint': f'{KEY}_ca_renew', 'view': ca_renew_view},
            # ⚠️ POST, both of them. See ca_recovery_view for why the download
            # is not a GET.
            {'url': f'/api/{KEY}/ca/recovery', 'methods': ['POST'],
             'endpoint': f'{KEY}_ca_recovery', 'view': ca_recovery_view},
            {'url': f'/api/{KEY}/ca/recovery/confirm', 'methods': ['POST'],
             'endpoint': f'{KEY}_ca_recovery_confirm', 'view': ca_recovery_confirm_view},
        ],
        # One public port. The console is Caddy-only on 443, the agent package is
        # a Caddy path route on the well-known port, and the database never
        # leaves the container bridge.
        'ports': [f'{DEVICE_PORT}/tcp'],
        'service_units': [],
        'settings_keys': [
            ACCESS_KEY, ACCESS_CHECKED_KEY,
            f'{KEY}_enabled', f'{KEY}_pg_password', f'{KEY}_commit_sha',
            f'{KEY}_version', f'{KEY}_domain',
        ],
    })
