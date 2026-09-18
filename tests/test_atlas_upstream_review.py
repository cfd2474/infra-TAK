"""The items TAKWerx raised reviewing the PR (W229).

Each test names the item it holds. ⚠️ **The review is the source of truth for
*why*, so the reasoning is quoted rather than paraphrased** — a guard whose
comment has drifted from the argument that produced it is the one that gets
relaxed later.
"""

import json
import pathlib
import sys

import pytest

BS = chr(92)
Q = chr(34)

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modules.atlas as atlas  # noqa: E402
from modules import atlas_instances as ai  # noqa: E402


# --------------------------------------------------------------------------- #
# Item 3 — the proxy-auth secret, and the branch that did not exist
# --------------------------------------------------------------------------- #
#
# Upstream: *"any local process on the host can reach 127.0.0.1:<port> with
# forged X-Authentik-* headers … and be an ATLAS administrator."*


def test_the_secret_is_looked_for_where_the_console_keeps_it(tmp_path):
    """⚠️ It asked `/root/infra-TAK/.config/proxy_auth.json`. The console's
    file is under `CONFIG_DIR`, which `ctx` carries from v10.1.80."""
    config = tmp_path / 'conf'
    config.mkdir()
    (config / 'proxy_auth.json').write_text(
        json.dumps({'secret': 's3cret'}), encoding='utf-8')

    assert atlas._proxy_auth_secret({'CONFIG_DIR': str(config)}) == 's3cret'


def test_the_fallback_is_derived_from_this_module_not_from_a_home(tmp_path):
    """⚠️ Ours at v10.1.76 has no `CONFIG_DIR` in ctx, so a fallback is needed
    — but it must not be another hardcoded `/root`. `modules/atlas.py` sits one
    directory below the console, so `../.config` is where `CONFIG_DIR`
    defaults, whichever user runs it and wherever it is installed."""
    candidates = list(atlas._proxy_auth_secret_path(None))

    assert candidates, 'there must be a fallback for a console without the key'
    for path in candidates:
        assert '/root/' not in path.replace('\\', '/'), path
    expected = ROOT / '.config' / 'proxy_auth.json'
    assert any(pathlib.Path(c) == expected for c in candidates), candidates


def test_ctx_is_preferred_over_the_fallback(tmp_path):
    ordered = list(atlas._proxy_auth_secret_path({'CONFIG_DIR': str(tmp_path)}))

    assert pathlib.Path(ordered[0]) == tmp_path / 'proxy_auth.json'


def test_an_unreadable_secret_is_empty_rather_than_an_exception():
    assert atlas._proxy_auth_secret({'CONFIG_DIR': '/nowhere/at/all'}) == ''


def _env(tmp_path, body='TAKMDM_ADMIN_GROUP=authentik Admins\n'):
    (tmp_path / '.env').write_text(body, encoding='utf-8')
    return str(tmp_path)


def test_caddy_injecting_with_no_secret_stops_the_deploy(tmp_path, monkeypatch):
    """⚠️ **The branch that did not exist, and the reason this is a blocker.**

    `secret and injects` armed it and `not injects` warned; `secret == '' and
    injects` fell through in silence. Nothing was logged,
    `TAKMDM_PROXY_AUTH_SECRET` was never written, and the vhost attached the
    header anyway — so the deploy read as armed while ATLAS could not tell a
    forged `X-Authentik-Username` from a real one.
    """
    monkeypatch.setattr(atlas, '_proxy_auth_secret', lambda ctx=None: '')
    monkeypatch.setattr(atlas, '_caddyfile_injects_proxy_auth', lambda: True)

    with pytest.raises(RuntimeError) as refused:
        atlas._arm_admin_gates(None, _env(tmp_path), lambda _m: None)

    assert 'forged identity headers' in str(refused.value)


def test_the_refusal_names_where_it_looked(tmp_path, monkeypatch):
    """An operator has to be able to fix it, and the fix is a path."""
    monkeypatch.setattr(atlas, '_proxy_auth_secret', lambda ctx=None: '')
    monkeypatch.setattr(atlas, '_caddyfile_injects_proxy_auth', lambda: True)

    with pytest.raises(RuntimeError) as refused:
        atlas._arm_admin_gates({'CONFIG_DIR': '/etc/takwerx'},
                               _env(tmp_path), lambda _m: None)

    assert '/etc/takwerx' in str(refused.value)


def test_a_console_with_no_proxy_auth_at_all_is_not_refused(tmp_path, monkeypatch):
    """⚠️ **The refusal must not fire on a legitimately secret-less install.**
    A console without the feature does not inject, and that case warns exactly
    as it did before — so the fatal branch is reachable only when Caddy *is*
    injecting and the module cannot find the file, which is a misconfiguration
    rather than a state anyone runs in on purpose."""
    monkeypatch.setattr(atlas, '_proxy_auth_secret', lambda ctx=None: '')
    monkeypatch.setattr(atlas, '_caddyfile_injects_proxy_auth', lambda: False)
    said = []

    atlas._arm_admin_gates(None, _env(tmp_path), said.append)

    assert any('does not inject' in line for line in said), said


def test_the_secret_is_still_written_when_both_sides_agree(tmp_path, monkeypatch):
    monkeypatch.setattr(atlas, '_proxy_auth_secret', lambda ctx=None: 's3cret')
    monkeypatch.setattr(atlas, '_caddyfile_injects_proxy_auth', lambda: True)
    dirpath = _env(tmp_path)

    atlas._arm_admin_gates(None, dirpath, lambda _m: None)

    body = (tmp_path / '.env').read_text(encoding='utf-8')
    assert 'TAKMDM_PROXY_AUTH_SECRET=s3cret' in body


# --------------------------------------------------------------------------- #
# Item 6 — deploy refuses an unknown slug
# --------------------------------------------------------------------------- #


def test_deploy_refuses_a_slug_nobody_registered(monkeypatch, tmp_path):
    """⚠️ `by_slug` answers None for both "no slug given" and "no such slug",
    and None means the plain deployment. So a deploy naming an unregistered
    slug would have built **over the plain deployment** — its directory, its
    port, its compose project, its database volume."""
    monkeypatch.setattr(atlas, 'load_instances', lambda c: [])
    monkeypatch.setattr(atlas, '_plog', lambda *a, **k: None)

    with pytest.raises(RuntimeError) as refused:
        atlas.deploy({}, None, {'slug': 'ghost'})

    assert 'ghost' in str(refused.value)
    assert 'plain deployment' in str(refused.value)


def test_deploy_with_no_slug_still_means_the_plain_deployment(monkeypatch):
    """The other half: absent is not unknown, and must keep working."""
    monkeypatch.setattr(atlas, 'load_instances', lambda c: [])
    monkeypatch.setattr(atlas, '_plog', lambda *a, **k: None)

    # It gets past the slug check and fails later, on Docker — which is the
    # assertion: no RuntimeError naming a slug.
    with pytest.raises(Exception) as stopped:
        atlas.deploy({}, None, {})

    assert 'registered with the slug' not in str(stopped.value)


def test_an_empty_slug_is_treated_as_absent(monkeypatch):
    """A form that posts `slug=""` means the plain deployment, not a missing
    one."""
    monkeypatch.setattr(atlas, 'load_instances', lambda c: [])
    monkeypatch.setattr(atlas, '_plog', lambda *a, **k: None)

    with pytest.raises(Exception) as stopped:
        atlas.deploy({}, None, {'slug': '   '})

    assert 'registered with the slug' not in str(stopped.value)


# --------------------------------------------------------------------------- #
# Item 7 — the agency name is free text in a compose .env
# --------------------------------------------------------------------------- #
#
# ⚠️ Every expectation below was measured with `docker compose config` on the
# box, not recalled. Unquoted truncates at `#` and interpolates `${...}`;
# single quotes are literal but an apostrophe makes compose fail to parse the
# **whole file**; double quotes with `\`, `"` and `$` escaped round-trip
# everything an agency name may legally contain.


@pytest.mark.parametrize('name', [
    'Corona Fire Department',
    "O'Brien County Sheriff",          # apostrophe: legal, and fatal unquoted
    'Corona & Co #1',                  # `#` truncated the value
    'Cost $HOME and ${HOME}',          # interpolation reached the environment
    'She said "hi"',
    'back' + chr(92) + 'slash',
])
def test_a_legal_name_survives_quoting(name):
    quoted = atlas._env_quote(name)

    assert quoted.startswith('"') and quoted.endswith('"')
    # Undo compose's double-quoted rules to get back what the container sees.
    inner = quoted[1:-1]
    unescaped = (inner.replace('$$', '$')
                      .replace(chr(92) + '"', '"')
                      .replace(chr(92) * 2, chr(92)))
    assert unescaped == name


def test_each_character_compose_gives_meaning_to_is_escaped():
    """⚠️ **Asserted on the output, not on a round trip.** The round-trip
    test above applies the escape and then undoes it — which passes just as
    happily when the escape does nothing at all, because the inverse is then
    also nothing. A mutation deleting the `"` escape survived it. These are the
    three literal forms, measured against `docker compose config`.
    """
    assert atlas._env_quote('say ' + Q + 'hi' + Q) == (
        Q + 'say ' + BS + Q + 'hi' + BS + Q + Q)
    assert atlas._env_quote('a' + BS + 'b') == Q + 'a' + BS + BS + 'b' + Q
    assert atlas._env_quote('$HOME') == Q + '$$HOME' + Q


def test_the_escapes_are_applied_in_an_order_that_does_not_double_up():
    """⚠️ Backslash first. Escaping the quote before the backslash would
    turn the backslash it just inserted into `\\`, and the value would grow a
    stray one every time."""
    assert atlas._env_quote(Q) == Q + BS + Q + Q
    assert atlas._env_quote(BS + Q) == Q + BS + BS + BS + Q + Q


def test_a_hash_cannot_truncate_the_line():
    assert '#' in atlas._env_quote('Corona & Co #1')
    assert atlas._env_quote('Corona & Co #1').endswith('"')


def test_a_dollar_cannot_reach_the_environment():
    """⚠️ `$$` is compose's literal-dollar escape. Without it an agency name
    could expand a variable out of the rendering process's environment."""
    assert atlas._env_quote('$HOME') == '"$$HOME"'


def test_an_empty_name_is_still_quoted():
    """Clearing a name writes `""`, not a bare `=` — one shape, always."""
    assert atlas._env_quote('') == '""'
    assert atlas._env_quote(None) == '""'


def test_the_instance_record_keeps_the_plain_name():
    """⚠️ Quoting belongs only where compose parses it. The record is what the
    console renders and what the footer is named after; quotation marks there
    would show up on screen."""
    inst = ai.make('corona', ai.MODE_DYNAMIC, 50, 8761,
                   agency_name="O'Brien County")

    assert inst['agency_name'] == "O'Brien County"


# --------------------------------------------------------------------------- #
# Item 8 — a write that failed reported success
# --------------------------------------------------------------------------- #


def test_a_write_that_cannot_happen_raises(tmp_path):
    with pytest.raises(OSError):
        atlas.write_env_value(str(tmp_path / 'missing' / '.env'), 'K', 'v')


def test_a_write_that_changes_nothing_is_still_False(tmp_path):
    """The honest no-op must survive the change, or every unchanged save would
    start reporting a failure."""
    env = tmp_path / '.env'
    env.write_text('K=v\n', encoding='utf-8')

    assert atlas.write_env_value(str(env), 'K', 'v') is False
