"""Where a deployment's directory is, for tests that build one by hand (W233).

⚠️ **It asks the module rather than spelling the path out.** Deployments used
to sit side by side — `<base>/atlas`, `<base>/atlas-corona` — and now nest
under `<base>/atlas/`, the plain one as `default`. Every fixture that created
a checkout, a `pki/` or an `.env` had the old shape written into it as a
literal, so the change turned 55 honest tests red at once.

Spelling the new shape out here instead would only move the problem: a helper
that recomputes the layout from the same constants drifts in the same
direction as the bug it is meant to catch. So it calls `instance_paths`, which
is the thing under test elsewhere and the single answer to "where does this
live" everywhere.
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import modules.atlas as atlas             # noqa: E402
from modules import atlas_instances as ai  # noqa: E402


def deployment_dir(name):
    """The directory for the deployment named `name`, as a `pathlib.Path`.

    `name` is the deployment name the rest of the suite already uses -- the
    compose project's stem: `atlas` for the plain deployment, `atlas-corona`
    for an agency. ⚠️ Those names did *not* change; only the directory did.
    """
    slug = name[len('atlas-'):] if name.startswith('atlas-') else ''
    inst = ai.make(slug, ai.MODE_FIXED, 1, 8761) if slug else None
    return pathlib.Path(atlas.instance_paths(None, inst)['dir'])
