# SPDX-License-Identifier: AGPL-3.0-or-later
# infra-TAK — TAK Infrastructure Platform
# Copyright (C) 2026 Andreas Johansson (TAKWERX)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""MediaMTX editor overlay: nested stream paths (v10.2.4, GH #82).

OBS and DJI publish to rtmp://host/live/<name>, so stream names are nested. The
overlay assumed they were not:

1. /hls-proxy/ checked the FIRST path segment's visibility, so a PRIVATE live/<name>
   was looked up as "live" (no entry -> public) and served to anyone.
2. The hlsviewer password kept its YAML quotes, so MediaMTX refused every off-box
   viewer (on-box requests pass as the loopback `any` user, which hid it).
3. /watch/<name> could not match live/<name> (404) — and it wrote the name unescaped
   into a JS string, so a quote in the URL ran script.

Exercised on the real overlay applied to a bare Flask app, MediaMTX stubbed.
"""

import base64
import importlib.util
import json
import pathlib

import pytest

flask = pytest.importorskip('flask')

REPO = pathlib.Path(__file__).resolve().parent.parent


class _Resp:
    def __init__(self, body=b'#EXTM3U\n', ctype='application/vnd.apple.mpegurl'):
        self._body, self.headers = body, {'Content-Type': ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / 'mediamtx.yml'
    cfg.write_text('authInternalUsers:\n- user: any\n  ips: [127.0.0.1]\n'
                   '- user: hlsviewer\n  pass: "s3cretPW"\n  permissions:\n  - action: read\n')
    monkeypatch.setenv('MEDIAMTX_CONFIG', str(cfg))
    spec = importlib.util.spec_from_file_location('overlay_under_test', REPO / 'mediamtx_ldap_overlay.py')
    ov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ov)
    vis = tmp_path / 'stream_visibility.json'
    vis.write_text(json.dumps({'live/secret': 'private', 'secret': 'private'}))
    monkeypatch.setattr(ov, 'VISIBILITY_FILE', str(vis))
    fetched = []

    def fake_urlopen(req, timeout=None, context=None):
        fetched.append({'url': req.full_url, 'auth': req.get_header('Authorization')})
        return _Resp()
    monkeypatch.setattr(ov.urllib.request, 'urlopen', fake_urlopen)
    app = flask.Flask('overlay-test')
    app.secret_key = 'test'
    ov.apply_ldap_overlay(app)
    return ov, app.test_client(), fetched


VIEWER_PRIVATE = {'X-Authentik-Username': 'alice', 'X-Authentik-Groups': 'vid_private'}
VIEWER_PUBLIC = {'X-Authentik-Username': 'bob', 'X-Authentik-Groups': 'vid_public'}


def test_private_nested_stream_is_not_served_to_anonymous(env):
    _, c, fetched = env
    for p in ('/hls-proxy/live/secret/index.m3u8', '/hls-proxy/live/secret/abc_seg7.mp4',
              '/hls-proxy/live/secret/', '/hls-proxy/live/secret', '/hls-proxy/secret/index.m3u8',
              '/hls-proxy/secret/', '/hls-proxy/secret'):
        assert c.get(p).status_code == 403, p
    assert fetched == []


def test_public_nested_stream_is_proxied_to_the_same_path(env):
    _, c, fetched = env
    r = c.get('/hls-proxy/live/public/index.m3u8')
    assert r.status_code == 200 and r.data == b'#EXTM3U\n'
    assert fetched[-1]['url'] == 'http://127.0.0.1:8888/live/public/index.m3u8'


def test_private_nested_stream_follows_vid_private(env):
    _, c, _ = env
    assert c.get('/hls-proxy/live/secret/index.m3u8', headers=VIEWER_PRIVATE).status_code == 200
    assert c.get('/hls-proxy/live/secret/index.m3u8', headers=VIEWER_PUBLIC).status_code == 403


@pytest.mark.parametrize('path', [
    '/hls-proxy/live/pub/../secret/index.m3u8',
    '/hls-proxy/pub/../secret/index.m3u8',
    '/hls-proxy/live/./secret/index.m3u8',
    '/hls-proxy/live//secret/index.m3u8',
    '/hls-proxy/live/pub/..%2Fsecret/index.m3u8',
    '/hls-proxy/live/secret%3F/index.m3u8',
])
def test_names_another_layer_could_normalise_are_refused(env, path):
    # The visibility lookup is literal; a name that a proxy or MediaMTX might resolve
    # to a different (private) path must never be looked up as an unknown -> public one.
    _, c, fetched = env
    r = c.get(path, environ_overrides={'PATH_INFO': path.replace('%2F', '/').replace('%3F', '?')})
    assert r.status_code == 404, path
    assert fetched == []


def test_hls_password_reaches_mediamtx_without_its_yaml_quotes(env):
    _, c, fetched = env
    c.get('/hls-proxy/live/public/index.m3u8')
    assert fetched[-1]['auth'] == 'Basic ' + base64.b64encode(b'hlsviewer:s3cretPW').decode()
    r = c.get('/api/viewer/hlscred', headers=VIEWER_PUBLIC)
    assert r.get_json() == {'username': 'hlsviewer', 'password': 's3cretPW'}


def test_yaml_scalar_text(env):
    ov = env[0]
    assert ov._yaml_scalar_text(' "s3cretPW" ') == 's3cretPW'
    assert ov._yaml_scalar_text('"a\\"b\\\\c"') == 'a"b\\c'
    assert ov._yaml_scalar_text("'it''s'") == "it's"
    assert ov._yaml_scalar_text(' plain ') == 'plain'
    assert ov._yaml_scalar_text('"') == '"'


def test_watch_serves_nested_paths_and_keeps_private_private(env):
    _, c, _ = env
    r = c.get('/watch/live/xbotgo')
    assert r.status_code == 200 and b'url="/hls-proxy/live/xbotgo/index.m3u8"' in r.data
    assert c.get('/watch/teststream').status_code == 200
    assert c.get('/watch/live/secret').status_code == 403
    assert c.get('/watch/live/secret', headers=VIEWER_PRIVATE).status_code == 200


@pytest.mark.parametrize('name', ['";alert(1);"', 'live/<script>', "x'y", 'live/../secret', 'a b'])
def test_watch_refuses_names_that_are_not_plain_path_segments(env, name):
    # Before v10.2.4 `/watch/";alert(1);"` returned 200 with the quote inside the page's
    # JS string literal — reflected script on an anonymous page.
    _, c, _ = env
    r = c.get('/watch/x', environ_overrides={'PATH_INFO': '/watch/' + name})
    assert r.status_code == 404
    assert b'alert' not in r.data and b'<script>' not in r.data
