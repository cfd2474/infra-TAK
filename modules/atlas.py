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
import subprocess

from . import register_module, job_log

KEY = 'atlas'

# Source, pinned. Rule 8: a tag *and* the commit it resolved to, verified after
# fetch — a moving branch is not a pin, and a tag can be moved by whoever owns
# the repo.
ATLAS_REPO_SSH = 'git@github.com:cfd2474/TAK-MDM.git'
ATLAS_REPO_HTTPS = 'https://github.com/cfd2474/TAK-MDM.git'
ATLAS_TAG = 'v0.1.3'
# ⚠️ The **commit**, not the tag object. `v0.1.0` is an annotated tag, so
# `git rev-parse v0.1.0` returns the tag object's own SHA while a clone's HEAD
# is the commit it points at — two different hashes, and comparing them made
# every deploy refuse itself. `git rev-parse 'v0.1.0^{}'` is the one to record.
ATLAS_SHA = 'a690aef7a8b485b4310d5c86b953fc16cceafa8f'

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

    return {'installed': enabled, 'running': running}


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #


def deploy_validate(data):
    """The deploy key, if the source repository is still private.

    Returned as params rather than read from settings so it never has to be
    typed twice, and so an empty value means "public repo, clone over HTTPS"
    rather than an error.
    """
    key = (data or {}).get('deploy_key') or ''
    if key and 'PRIVATE KEY' not in key:
        return None, 'That does not look like an SSH private key.'
    return {'deploy_key': key.strip()}, None


def _deploy_key_path(dirpath):
    """Beside the install directory, never inside it.

    ⚠️ A key inside the clone is inside a git working tree, and one `git add -A`
    in a debugging session puts a private key in a commit.
    """
    return os.path.join(os.path.dirname(dirpath), f'.{KEY}_deploy_key')


def _write_deploy_key(ctx, dirpath, key_text, plog):
    """Put the deploy key somewhere git can use it, readable by nobody else.

    ⚠️ Mode 600 and outside the clone. A key inside the install directory would
    be inside a git working tree, and one `git add -A` in a debugging session
    puts a private key in a commit.
    """
    key_path = _deploy_key_path(dirpath)
    # ⚠️ `perm`, not `mode`: `_write_priv(path, content, mode='w', perm=None)`
    # takes the *open* mode there. Passing 0o600 as `mode` opens the file in
    # mode "384" and leaves a private key world-readable.
    ctx['_write_priv'](key_path, key_text.rstrip('\n') + '\n', perm=0o600)
    plog('  Deploy key written (mode 600, outside the clone)')
    return key_path


def _git_env(key_path):
    if not key_path:
        return None
    env = dict(os.environ)
    env['GIT_SSH_COMMAND'] = (
        f'ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
    )
    return env


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
        key_text = params.get('deploy_key') or settings.get(f'{KEY}_deploy_key') or ''
        if key_text:
            key_path = _write_deploy_key(ctx, dirpath, key_text, plog)
        else:
            existing = _deploy_key_path(dirpath)
            key_path = existing if os.path.exists(existing) else None
            if key_path:
                plog(f'  Using the deploy key already on this box ({key_path})')
        repo = ATLAS_REPO_SSH if key_path else ATLAS_REPO_HTTPS

        if os.path.isdir(os.path.join(dirpath, '.git')):
            plog(f'  Already cloned at {dirpath} — fetching {ATLAS_TAG}')
            ctx['_module_git'](dirpath, 'checkout', '--', '.', timeout=60)
            r = subprocess.run(
                ['git', '-C', dirpath, 'fetch', '--tags', '--depth=1', 'origin', ATLAS_TAG],
                capture_output=True, text=True, timeout=300, env=_git_env(key_path),
            )
            if r.returncode != 0:
                raise RuntimeError(f'git fetch failed: {r.stderr[-300:]}')
            ctx['_module_git'](dirpath, 'checkout', '-f', ATLAS_TAG, timeout=60)
        else:
            os.makedirs(dirpath, exist_ok=True)
            plog(f'  Cloning {repo} @ {ATLAS_TAG}')
            r = subprocess.run(
                ['git', 'clone', '--depth=1', '--branch', ATLAS_TAG, repo, dirpath],
                capture_output=True, text=True, timeout=600, env=_git_env(key_path),
            )
            if r.returncode != 0:
                hint = ''
                if 'Permission denied' in r.stderr or 'not read from remote' in r.stderr:
                    hint = ' — the repository is private; supply a read-only deploy key'
                raise RuntimeError(f'git clone failed{hint}: {r.stderr[-300:]}')
        commit = _verify_pin(ctx, dirpath, plog)
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
        if key_text:
            s[f'{KEY}_deploy_key'] = key_text
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
        job.update({'running': False, 'complete': True, 'error': False})
    except Exception as exc:
        plog(f'ERROR: {exc}')
        job.update({'running': False, 'complete': False, 'error': True})


# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #


def uninstall(ctx, job, params):
    """Stop ATLAS and take it off the network.

    ⚠️ **The install directory stays, and with it the database volume and the
    device CA.** Removing them would revoke every enrolled tablet's identity
    irreversibly — a factory reset each, in person. Uninstall means "stop
    serving this"; destroying a fleet's enrolment is a different act and should
    look like one.
    """
    steps = []
    dirpath = atlas_dir(ctx)

    if os.path.isdir(dirpath):
        _compose(ctx, 'down', timeout=180)
        steps.append('Containers stopped and removed')

    ctx['_fw_remove'](DEVICE_PORT, 'tcp')
    steps.append(f'Firewall rule for {DEVICE_PORT}/tcp removed')

    s = ctx['load_settings']()
    s[f'{KEY}_enabled'] = False
    ctx['save_settings'](s)
    ctx['generate_caddyfile'](s)
    ctx['_caddy_reload']()
    steps.append('Caddy vhosts removed')

    try:
        ctx['_deregister_authentik_proxy_app'](s, KEY, 'ATLAS MDM Proxy')
        steps.append('Authentik application deregistered')
    except Exception:
        steps.append('Authentik application not deregistered (not configured)')

    steps.append('Install directory kept — database and device CA are still there')
    return {'success': True, 'steps': steps}


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
        s = ctx['load_settings']()
        return jsonify({
            'version': (s.get(f'{KEY}_commit_sha') or '')[:12] or ATLAS_TAG,
            'latest': None,
            'update_available': False,
        })

    register_module({
        'key': KEY,
        'name': 'ATLAS MDM',
        'description': 'Android device management for ATAK tablets — policies, apps, enrolment',
        'icon': '\U0001F4F1',  # 📱 as an escape: a literal surrogate pair corrupts on edit
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
        ],
        # One public port. The console is Caddy-only on 443, the agent package is
        # a Caddy path route on the well-known port, and the database never
        # leaves the container bridge.
        'ports': [f'{DEVICE_PORT}/tcp'],
        'service_units': [],
        'settings_keys': [
            f'{KEY}_enabled', f'{KEY}_pg_password', f'{KEY}_commit_sha',
            f'{KEY}_deploy_key', f'{KEY}_domain',
        ],
    })
