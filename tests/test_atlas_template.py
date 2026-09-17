"""The ATLAS console page, rendered (W214).

Three things an operator asked for, and one bug found while doing them:

* the deploy log is the **first** card, not the last;
* the **Install** card stands down while a deploy is running;
* the break-glass commands are sized to their content instead of getting a
  340px box around one line of `sed`.

⚠️ **The bug.** Those commands used `.log-box`, which had no `white-space`
rule — so the CSS default `normal` collapsed the newline between the two
commands and printed them as one line:

    sed -i '/^TAKMDM_PROXY_AUTH_SECRET=/d' /root/atlas/.env cd /root/atlas && ...

`sed` then treats `cd` and `/root/atlas` as further files to edit and the
command fails. That is the recovery path for an ATLAS lockout, so it mattered
more than the empty space that led to finding it. The same missing rule made a
running deploy's log render as one run-on paragraph, because `renderLog` joins
its lines with a newline into `textContent`.

These render the real template with the real loader rather than matching source
text, because what is asserted here is the *output* an operator sees.
"""

import pathlib
import re
import sys

import pytest

jinja2 = pytest.importorskip("jinja2", reason="jinja2 not installed")

ROOT = pathlib.Path(__file__).resolve().parents[1]


def render(**ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
        undefined=jinja2.ChainableUndefined,
    )
    return env.get_template("atlas.html").render(**ctx)


def card_titles(html):
    return re.findall(r'class="card-title">([^<]+)<', html)


def attrs_of(html, element_id):
    """The attributes on an element, or None when it is not on the page."""
    found = re.search(r'id="%s"([^>]*)>' % element_id, html)
    return found.group(1).strip() if found else None


@pytest.fixture
def idle():
    return render(installed=False, deploy_log=[], deploy_running=False)


@pytest.fixture
def deploying():
    return render(installed=False, deploy_running=True,
                  deploy_log=["cloning", "building"])


@pytest.fixture
def installed():
    return render(installed=True, deploy_log=[], deploy_running=False)


# --------------------------------------------------------------------------- #
# Where the deploy log sits
# --------------------------------------------------------------------------- #


def test_the_deploy_log_is_the_first_card(idle):
    """⚠️ It used to be last, below two cards of prose — so the one thing worth
    watching for five to ten minutes was off-screen when it started moving."""
    assert card_titles(idle)[0] == "Deploy log"


def test_the_deploy_log_is_above_install(deploying):
    titles = card_titles(deploying)

    assert titles.index("Deploy log") < titles.index("Install")


def test_the_deploy_log_is_hidden_until_there_is_one(idle):
    assert 'style="display:none"' in attrs_of(idle, "deployCard")


def test_the_deploy_log_is_shown_while_deploying(deploying):
    assert attrs_of(deploying, "deployCard") == ""


# --------------------------------------------------------------------------- #
# The Install card standing down
# --------------------------------------------------------------------------- #


def test_install_is_offered_when_nothing_is_running(idle):
    assert attrs_of(idle, "installCard") == ""


def test_install_is_hidden_while_a_deploy_runs(deploying):
    """Nothing on it can be acted on: the button is disabled, the size is
    already committed, and the prose describes a decision made."""
    assert 'style="display:none"' in attrs_of(deploying, "installCard")


def test_install_is_not_on_the_page_at_all_once_installed(installed):
    assert attrs_of(installed, "installCard") is None


def function_body(js, name):
    """The text of one JS function, by brace matching from its opening `{`."""
    start = js.index("function %s(" % name)
    opened = js.index("{", start)
    depth = 0
    for i in range(opened, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[opened:i + 1]
    raise AssertionError("unbalanced braces in %s" % name)


# ⚠️ **Three paths, asserted separately, because counting was not a check.**
# This was one test asserting `count("setInstallCard(true)") >= 2`. A mutation
# deleting the call from the refusal branch left two others behind and the test
# passed — a count is a proxy for the thing, and any hedge in an assertion is a
# hole in it. Each failure path is now named and checked where it lives.
#
# The reserved-storage field lives on the Install card, so a failure that hides
# it and leaves it hidden strands an operator with nothing left to act on.


def test_a_refused_deploy_restores_the_install_card(idle):
    """The server said no — the operator needs the field back to correct it."""
    refusal = re.search(r"if \(d\.error\) \{([^}]*)\}",
                        function_body(idle, "startDeploy"))

    assert refusal, "startDeploy has no refusal branch"
    assert "setInstallCard(true)" in refusal.group(1)


def test_a_network_failure_restores_the_install_card(idle):
    """The request never landed, so nothing is running and nothing is hidden."""
    caught = re.search(r"\.catch\(\(\) => \{([^}]*)\}\)",
                       function_body(idle, "startDeploy"))

    assert caught, "startDeploy does not handle a failed request"
    assert "setInstallCard(true)" in caught.group(1)


def test_a_deploy_that_fails_midway_restores_the_install_card(idle):
    """The deploy started and then failed — the log is up, and retrying still
    needs the card."""
    failed = re.search(r"if \(d\.error && !d\.running\) \{(.*?)\n    \}",
                       function_body(idle, "pollDeploy"), re.S)

    assert failed, "pollDeploy has no failure branch"
    assert "setInstallCard(true)" in failed.group(1)


def test_the_failure_banner_points_at_the_log_s_new_position(idle):
    assert "see the log above" in idle
    assert "see the log below" not in idle


# --------------------------------------------------------------------------- #
# The note promising a refresh
# --------------------------------------------------------------------------- #

# ⚠️ **A promise the page has to keep.** `pollDeploy` reloads on
# `d.complete && !d.error` and on nothing else, so the note belongs to a
# *running* deploy only. Above a failed one it would send an operator off to
# wait for something that is never coming, instead of reading the error under
# it — which is worse than saying nothing at all.


def test_the_note_is_at_the_top_of_the_deploy_log_card(deploying):
    """Asked for at the top of the card: after the title, before the log.

    ⚠️ Asserted as an ordering rather than by matching the markup between the
    two. The first attempt looked for `</div>` immediately followed by the
    log-box and matched nothing, because the note and its comment sit in
    between — the regex encoded the layout it was supposed to be checking.
    """
    span = deploying[deploying.index('id="deployCard"'):
                     deploying.index('class="log-box" id="deployLog"')]

    assert "card-title" in span, "the card has no title"
    assert 'id="deployNote"' in span, "the note is not above the log"
    assert span.index("card-title") < span.index('id="deployNote"'), (
        "the note sits above the card's own title"
    )


def test_the_note_says_the_page_will_refresh(deploying):
    note = re.search(r'id="deployNote"[^>]*>(.*?)</p>', deploying, re.S)

    assert note, "there is no deploy note"
    text = " ".join(note.group(1).split())
    assert "refreshes itself" in text
    assert "deployed" in text


def test_the_note_is_shown_while_deploying(deploying):
    attrs = re.search(r'id="deployNote"(.*?)>', deploying, re.S).group(1)

    assert "display:none" not in attrs, "the note is hidden during a deploy"


def test_the_note_is_absent_when_no_deploy_is_running(idle):
    """⚠️ Including when the card itself is up showing a *finished* log."""
    attrs = re.search(r'id="deployNote"(.*?)>', idle, re.S).group(1)

    assert "display:none" in attrs


def test_the_page_really_does_reload_on_success(idle):
    """The note is only honest if this line exists. If the reload is ever
    removed, the note becomes a lie and this test is what says so."""
    body = function_body(idle, "pollDeploy")
    success = re.search(r"if \(d\.complete && !d\.error\) \{([^}]*)\}", body)

    assert success, "pollDeploy no longer has a success branch"
    assert "location.reload()" in success.group(1)


def test_a_failed_deploy_stops_promising_a_refresh(idle):
    failed = re.search(r"if \(d\.error && !d\.running\) \{(.*?)\n    \}",
                       function_body(idle, "pollDeploy"), re.S)

    assert failed, "pollDeploy has no failure branch"
    assert "setDeployNote(false)" in failed.group(1)


def test_a_refused_deploy_stops_promising_a_refresh(idle):
    refusal = re.search(r"if \(d\.error\) \{([^}]*)\}",
                        function_body(idle, "startDeploy"))

    assert refusal, "startDeploy has no refusal branch"
    assert "setDeployNote(false)" in refusal.group(1)


def test_an_accepted_deploy_puts_the_note_up(idle):
    body = function_body(idle, "startDeploy")

    assert "setDeployNote(true)" in body, (
        "the note never appears for a deploy started from this page"
    )


# --------------------------------------------------------------------------- #
# The break-glass commands
# --------------------------------------------------------------------------- #


def test_the_recovery_commands_keep_their_line_break(installed):
    """⚠️ **The bug.** Collapsed, this reads
    `sed -i '...' /root/atlas/.env cd /root/atlas && ...`, and `sed` treats
    `cd` as a file to edit. Two commands have to stay two commands."""
    blocks = re.findall(r'<div class="cmd"[^>]*>(.*?)</div>', installed, re.S)

    assert blocks, "no command blocks on the page"
    two_part = [b for b in blocks if "sed -i" in b]
    assert two_part, "the sed recovery commands are gone"
    for block in two_part:
        assert "\n" in block, f"collapsed into one line: {block!r}"
        assert block.splitlines()[1].startswith("cd /root/atlas"), (
            "the second command is not on its own line"
        )


def test_the_commands_are_rendered_with_a_whitespace_preserving_rule(installed):
    """A newline in the source is only two lines on screen if the CSS says so.
    The rule and the markup have to agree, and the rule is what was missing."""
    rule = re.search(r'\.cmd\{([^}]*)\}', installed)

    assert rule, ".cmd is not defined"
    assert "white-space:pre-wrap" in rule.group(1)


def test_the_commands_are_not_given_a_log_sized_box(installed):
    """⚠️ The empty space in the screenshot: `.log-box` is `height:340px`,
    which is right for a stream and absurd for one line of `sed`.

    ⚠️ Anchored to a property boundary, because `"height:" not in rule` is
    satisfied by `line-height:1.55` — which it was, on the first run. Sixth
    time an assertion on this project has matched the wrong occurrence.
    """
    rule = re.search(r'\.cmd\{([^}]*)\}', installed).group(1)

    assert not re.search(r'(?:^|;)\s*height:', rule), (
        "a command block was given a fixed height"
    )


def test_no_static_command_still_uses_the_log_viewer(installed):
    """The four snippets were `.log-box`. If one comes back, the 340px box and
    the collapsed newline come back with it.

    ⚠️ Asked of the log-boxes rather than of the commands. Searching backwards
    from each command found the `sed -i` written *inside the CSS comment* that
    explains this very bug, where there is no enclosing element at all.
    """
    boxes = re.findall(r'<div[^>]*class="log-box"[^>]*>(.*?)</div>',
                       installed, re.S)

    for box in boxes:
        for snippet in ("sed -i", "docker logs --tail"):
            assert snippet not in box, f"{snippet!r} is back inside a log-box"


# --------------------------------------------------------------------------- #
# The streaming log, which had the same missing rule
# --------------------------------------------------------------------------- #


def test_the_streaming_log_preserves_its_newlines(idle):
    """⚠️ `renderLog` sets `textContent` to lines joined with a newline, so
    without this rule a running deploy rendered as one run-on paragraph — the
    same root cause as the commands, on the card an operator watches."""
    rule = re.search(r'\.log-box\{([^}]*)\}', idle)

    assert rule, ".log-box is not defined"
    assert "white-space:pre-wrap" in rule.group(1)


def test_the_streaming_log_keeps_a_stable_height(idle):
    """Deliberately fixed, unlike `.cmd`: the box exists before the first line
    arrives and must not resize under the operator as output lands."""
    rule = re.search(r'\.log-box\{([^}]*)\}', idle).group(1)

    assert "height:340px" in rule
