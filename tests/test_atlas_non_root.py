"""The module never reaches for privilege it does not have (W230).

⚠️ **The guard whose absence let the whole class ship.** Upstream's review
found six separate root assumptions in one module, and every one of them was
invisible here: the tests stub `ctx`, and the box this was written on ran the
console as root. A test that stubs the privileged call can never notice that
the call is not available.

So this one reads the source instead. It is a static guard, and that is
deliberate: the dynamic question — "does a deploy succeed on a non-root box?"
— needs a non-root box, and the honest answer is that it belongs in the audit
log of a real deploy. This catches the regression before it gets that far.

⚠️ **Measured against the box, not against the guide.** The allowlist below is
what `/opt/infratak/.shims` actually contains on a converted install, read off
the filesystem on 2026-09-18. `losetup`, `umount`, `mount`, `resize2fs`,
`e2fsck`, `truncate`, `systemd-escape` and `mkfs.ext4` are **not** there.
"""

import ast
import io
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULE_PATH = ROOT / 'modules' / 'atlas.py'
SOURCE = io.open(MODULE_PATH, encoding='utf-8').read()
TREE = ast.parse(SOURCE)


#: Binaries the console can actually run, from `/opt/infratak/.shims` on the
#: converted box. ⚠️ Adding a name here is a claim about that directory, not a
#: preference — check it before you do.
SHIMMED = {
    'apt', 'apt-get', 'chcon', 'chmod', 'chown', 'cp',
    'debconf-set-selections', 'dnf', 'docker', 'dpkg', 'fail2ban-client',
    'fallocate', 'firewall-cmd', 'gpg', 'install', 'journalctl', 'ln',
    'loginctl', 'mkdir', 'mkswap', 'mv', 'newaliases', 'pg_createcluster',
    'postconf', 'postmap', 'restorecon', 'rm', 'rmdir', 'runuser', 'semanage',
    'semodule', 'swapoff', 'swapon', 'sysctl', 'systemctl', 'systemd-run',
    'tee', 'touch', 'ufw', 'yum',
}

#: Read-only probes the console runs as itself. They need no privilege at all,
#: so they are neither shimmed nor a problem.
UNPRIVILEGED = {'ss', 'git', 'python3', 'id', 'getent', 'uname', 'df'}

#: The binaries this work removed, named so a regression says which.
RETIRED = {
    'losetup': 'the loop store is gone; use a directory',
    'umount': 'nothing is mounted any more',
    'mount': 'nothing is mounted any more',
    'mkfs.ext4': 'the loop store is gone',
    'resize2fs': 'the loop store is gone',
    'e2fsck': 'the loop store is gone',
    'truncate': 'the loop store is gone',
    'systemd-escape': 'no .mount units are written any more',
}


def _run_root_binaries():
    """The first argv element of every literal `_run_root([...])` call."""
    found = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, 'id', None)
        if name != '_run_root' or not node.args:
            continue
        argv = node.args[0]
        if isinstance(argv, ast.List) and argv.elts:
            first = argv.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((first.value, node.lineno))
    return found


def test_the_extractor_finds_something():
    """⚠️ A guard that matches nothing passes forever. This one has been wrong
    that way before — a regex with a `\\b` that silently matched no lines and
    reported the page no longer called compose."""
    found = _run_root_binaries()

    assert len(found) >= 3, found


@pytest.mark.parametrize('binary,line', _run_root_binaries())
def test_every_binary_the_module_runs_is_available_to_the_console(binary, line):
    """⚠️ Not "exists on the box" — *runnable by the console*. `losetup` is
    installed on every Linux system and the console cannot execute it, which
    is exactly how six root assumptions survived a 590-test suite."""
    if binary in RETIRED:
        pytest.fail('%s:%d runs `%s`, which was retired: %s'
                    % (MODULE_PATH.name, line, binary, RETIRED[binary]))
    assert binary in SHIMMED or binary in UNPRIVILEGED, (
        '%s:%d runs `%s`, which is neither shimmed for the console nor an '
        'unprivileged probe. Either it needs adding to the broker (say so in '
        'the PR body) or the code needs another way.'
        % (MODULE_PATH.name, line, binary))


def test_no_retired_binary_is_named_anywhere_in_the_module():
    """Including in a string built at run time, which the AST walk above would
    miss. ⚠️ Comments are stripped first: this file's own history is written in
    them, and matching prose would make the guard unfailable-by-editing."""
    code = re.sub(r'#[^\n]*', '', SOURCE)
    code = re.sub(r'"""[\s\S]*?"""', '', code)

    for binary in sorted(RETIRED):
        # ⚠️ Word boundaries. Plain `in` matched `mount` inside the key
        # `'mounted'` and failed on honest code -- and a guard that cries wolf
        # is a guard somebody deletes.
        hit = re.search(r'[^A-Za-z0-9_.]' + re.escape(binary) + r'[^A-Za-z0-9_.]',
                        code)
        assert hit is None, (
            '`%s` still appears in executable code at offset %d: %s'
            % (binary, hit.start(), RETIRED[binary]))


def test_nothing_chowns_through_the_python_api():
    """⚠️ `os.chown` is `EPERM` for a non-root console, and the recursive one
    in `deploy` was unguarded — so every deploy stopped there. The one chown
    that remains (Postgres's data directory, uid 70) goes through the shimmed
    binary instead."""
    code = re.sub(r'#[^\n]*', '', SOURCE)
    code = re.sub(r'"""[\s\S]*?"""', '', code)

    assert 'os.chown' not in code


def test_nothing_writes_to_a_privileged_path_directly():
    """⚠️ `/etc/systemd/system` and `/var/lib/<anything>` are not the
    console's. The module wrote unit files there with a plain `open()`; the
    Caddy trust pool now goes through `ctx['_write_priv']`, which is the seam
    the console exposes for exactly this.
    """
    code = re.sub(r'#[^\n]*', '', SOURCE)
    code = re.sub(r'"""[\s\S]*?"""', '', code)

    # ⚠️ **Writes only.** Reading `/etc/caddy/Caddyfile` is how
    # `_caddyfile_injects_proxy_auth` checks that the vhost really injects the
    # header, and the console can read it perfectly well. Flagging that too
    # made this fail on code that is right, which is how a guard gets relaxed
    # into uselessness.
    for match in re.finditer(
            r"open\(\s*'(?:/etc/|/var/lib/)[^']*'\s*,\s*'[wa]", code):
        pytest.fail('a privileged path is opened for writing at offset %d; '
                    "use ctx['_write_priv']" % match.start())


def test_the_install_directory_is_not_hardcoded_to_root():
    """⚠️ Guide §8: a non-root console keeps its modules under its own home.
    `/root` survives only as the *probe* for a box that has not been
    converted, never as the answer."""
    code = re.sub(r'#[^\n]*', '', SOURCE)
    code = re.sub(r'"""[\s\S]*?"""', '', code)

    literals = set(re.findall(r"'(/root[^']*)'", code))

    # ⚠️ Two legitimate mentions, both **reads** of a box that has not been
    # converted: the layout probe in `install_base`, and the legacy candidate
    # list that finds a device CA in a checkout predating `install_base`.
    # Anything else is an install path being hardcoded, which is the bug.
    assert literals <= {'/root', '/root/atlas'}, literals


def test_the_store_is_a_directory_not_an_image():
    """The shape of the fix, asserted so a revert is loud."""
    import modules.atlas as atlas
    from modules import atlas_instances as ai

    paths = atlas.instance_paths(None, ai.make('x', ai.MODE_FIXED, 1, 8761))

    assert 'image' not in paths
    assert paths['store'].endswith('/store')
    assert paths['store'].startswith(paths['dir'])
