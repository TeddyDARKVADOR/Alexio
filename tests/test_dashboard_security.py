"""
Phase 06 — the remote dashboard is the only part of Alexio a stranger can reach.

Everything else in this project fails safe by being unreachable: an action that
misbehaves needs someone at the keyboard. dashboard/server.py listens on
0.0.0.0, so its bugs are reachable by anyone on the network — which is why the
five defects this phase fixed are pinned here, in the form that would catch them
coming back.

WHAT THESE TESTS ARE ACTUALLY CHECKING

  The AES key is not derived from the login PIN. Six characters out of a
  31-symbol alphabet is about 2^29.7 possibilities. Hashed once, with a salt
  written into the source file, that is a key anyone who captured one encrypted
  command could recover on a laptop over lunch. The PIN now only ever pairs a
  device; the key is 256 random bits the server mints.

  Credentials expire. All of them. A device token that never dies is a
  permanent credential in a phone's localStorage — it outlives the phone being
  lost, lent, or sold, and the only way to cut it used to be restarting Alexio.

  The gate on `/` is on the server. The page used to be served to anyone and
  then redirect itself in JavaScript. Anything that ships before the check is a
  check that did not happen.

  The API cannot be driven by a cookie. This is what makes the cookie above
  safe to add: navigation is authenticated by the cookie, the API only by the
  Authorization header, and a cross-site page can forge the first but never the
  second.

  Nothing here asks to be root. The old firewall setup raised a UAC dialog on
  Windows and reached for pkexec, sudo and osascript elsewhere. A program that
  routinely asks for administrator rights teaches the user to grant them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dashboard import auth

ROOT   = Path(__file__).resolve().parent.parent
SERVER = ROOT / "dashboard" / "server.py"


def code_only(path: Path) -> str:
    """The file with its comments and docstrings removed.

    Needed because the tests below assert that certain mechanisms are *absent*,
    and the source explains at length why they were removed. Searching the raw
    text would make a file that documents its own history fail the test that
    checks the history did not repeat. Comments are already gone by the time ast
    is done; docstrings have to be dropped by hand.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


class Clock:
    """A clock the tests can move. Expiry that can only be checked by waiting
    thirty days is expiry nobody ever checks."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock) -> auth.CredentialStore:
    return auth.CredentialStore(clock=clock)


# ── 1. the key is not the PIN ────────────────────────────────────────────────

def test_the_session_secret_is_256_random_bits_and_not_derived_from_anything(store):
    pin = store.new_pairing_key()
    assert store.redeem_pairing_key(pin)
    _, _, session = store.open_session()

    assert len(session.key_bytes()) == 32
    # The whole point: nothing about the key can be predicted from the PIN.
    assert pin.encode() not in session.key_bytes()


def test_two_sessions_never_share_a_key(store):
    secrets_seen = set()
    for _ in range(20):
        _, _, session = store.open_session()
        secrets_seen.add(session.secret)
    assert len(secrets_seen) == 20


def test_the_old_fixed_salt_derivation_is_gone_from_the_source():
    """The precise regression: SHA-256(pin ‖ constant) as a key.

    Read from the file rather than the imported module, because the dangerous
    version was a module-level constant and a three-line helper — both of which
    can come back in a copy-paste without any test noticing.
    """
    code = code_only(SERVER)
    assert "JARVIS-DASHBOARD-v1" not in code, "the fixed AES salt is back"
    assert "_derive_key" not in code, "the PIN is being stretched into a key again"

    client = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    assert "CryptoJS.SHA256" not in client, "the browser is deriving the key again"


def test_the_pairing_key_works_once_and_then_never_again(store):
    pin = store.new_pairing_key()
    assert store.redeem_pairing_key(pin) is True
    assert store.redeem_pairing_key(pin) is False


def test_the_pairing_key_dies_with_the_qr_code(store, clock):
    pin = store.new_pairing_key(ttl=600)
    clock.advance(601)
    assert store.redeem_pairing_key(pin) is False


def test_a_burst_of_wrong_guesses_takes_the_pin_away(store):
    """Rate limiting alone does not protect a thirty-bit secret — a handful of
    addresses on a LAN sidesteps a per-address limit. What protects it is
    noticing that something is guessing and throwing the PIN out."""
    pin = store.new_pairing_key()
    burned = False
    for i in range(auth.BURN_LIMIT):
        burned = store.throttle.fail(f"10.0.0.{i}")
    assert burned, "the global failure threshold never fired"

    store.burn_pairing_keys()
    assert store.redeem_pairing_key(pin) is False


def test_a_correct_login_does_not_eat_anyone_else_s_budget(store):
    """Only failures count. A phone reconnecting over a flaky network must not
    be able to lock itself out."""
    for _ in range(auth.FAIL_LIMIT - 1):
        store.throttle.fail("192.168.1.50")
    assert store.throttle.retry_after("192.168.1.50") == 0.0
    store.throttle.clear("192.168.1.50")
    assert store.throttle.retry_after("192.168.1.50") == 0.0


def test_too_many_failures_from_one_address_are_refused_then_forgiven(store, clock):
    for _ in range(auth.FAIL_LIMIT):
        store.throttle.fail("192.168.1.50", now=clock())
    assert store.throttle.retry_after("192.168.1.50", now=clock()) > 0
    clock.advance(auth.FAIL_WINDOW + 1)
    assert store.throttle.retry_after("192.168.1.50", now=clock()) == 0.0


# ── 2. everything expires ────────────────────────────────────────────────────

def test_a_bearer_token_expires_when_it_goes_unused(store, clock):
    bearer, _, _ = store.open_session()
    clock.advance(auth.SESSION_TTL - 10)
    assert store.session_for_bearer(bearer) is not None      # still inside the window

    clock.advance(auth.SESSION_TTL - 10)                     # …and it slid forward
    assert store.session_for_bearer(bearer) is not None

    clock.advance(auth.SESSION_TTL + 1)
    assert store.session_for_bearer(bearer) is None


def test_an_active_session_still_stops_at_the_absolute_ceiling(store, clock):
    """Sliding expiry alone means a session touched every morning lives for
    ever. SESSION_MAX is the answer to "and then what?"."""
    start        = clock()
    bearer, _, _ = store.open_session()

    # Use it constantly, so the idle window never comes close to running out.
    while store.session_for_bearer(bearer) is not None:
        clock.advance(auth.SESSION_TTL // 4)
        assert clock() - start < auth.SESSION_MAX * 2, "the session never expires"

    assert clock() - start >= auth.SESSION_MAX


def test_a_paired_device_expires_after_a_month_however_often_it_is_used(store, clock):
    """The absolute ceiling, checked the only way that means anything: by using
    the phone regularly, so the idle window is never what kills it."""
    start         = clock()
    _, _, session = store.open_session()
    device        = store.pair_device(session)

    while store.open_session_for_device(device) is not None:
        clock.advance(auth.DEVICE_IDLE // 2)
        assert clock() - start < auth.DEVICE_TTL * 2, "the device token never expires"

    assert clock() - start >= auth.DEVICE_TTL


def test_a_paired_device_expires_sooner_if_it_is_never_seen(store, clock):
    _, _, session = store.open_session()
    device = store.pair_device(session)
    clock.advance(auth.DEVICE_IDLE + 1)
    assert store.open_session_for_device(device) is None


def test_a_returning_device_keeps_the_key_it_already_stored(store):
    """The phone still holds the old secret in sessionStorage. Handing it a new
    one would silently break decryption on the first reconnect."""
    _, _, session = store.open_session()
    device        = store.pair_device(session)
    opened        = store.open_session_for_device(device)
    assert opened is not None
    assert opened[2].secret == session.secret


def test_the_number_of_paired_devices_is_capped(store, clock):
    _, _, session = store.open_session()
    tokens = []
    for _ in range(auth.MAX_DEVICES + 4):
        clock.advance(1)               # distinct last_seen, so eviction is defined
        tokens.append(store.pair_device(session))

    assert store.device_count() == auth.MAX_DEVICES
    assert store.open_session_for_device(tokens[0]) is None    # oldest evicted
    assert store.open_session_for_device(tokens[-1]) is not None


def test_revoking_devices_actually_revokes_them(store):
    _, _, session = store.open_session()
    device = store.pair_device(session)
    assert store.revoke_devices() == 1
    assert store.open_session_for_device(device) is None


def test_tokens_are_never_held_in_the_clear(store):
    """A traceback, a debugger, or a future decision to persist sessions across
    restarts should not hand anyone a working credential."""
    bearer, nav, session = store.open_session()
    device = store.pair_device(session)

    holders = [store._sessions, store._navs, store._devices, store._tickets]
    for value in (bearer, nav, device):
        for holder in holders:
            assert value not in holder
            assert value not in [str(v) for v in holder.values()]


# ── 3. WebSocket tickets ─────────────────────────────────────────────────────

def test_a_ws_ticket_works_once(store):
    bearer, _, _ = store.open_session()
    ticket = store.new_ticket(bearer)
    assert store.redeem_ticket(ticket) is not None
    assert store.redeem_ticket(ticket) is None


def test_a_ws_ticket_is_worthless_thirty_seconds_later(store, clock):
    """The reason it exists: a browser cannot put a header on a WebSocket
    handshake, so something goes in the query string, and query strings end up
    in logs. This one is dead before the log is flushed."""
    bearer, _, _ = store.open_session()
    ticket = store.new_ticket(bearer)
    clock.advance(auth.TICKET_TTL + 1)
    assert store.redeem_ticket(ticket) is None


def test_no_ticket_without_a_live_session(store):
    assert store.new_ticket("not-a-token") is None
    assert store.redeem_ticket("") is None


# ── 4. the HTTP surface ──────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """A real FastAPI app, with the uploads folder pointed somewhere harmless."""
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer

    server = DashboardServer()
    server._uploads_dir  = tmp_path
    server._uploads_root = tmp_path.resolve()
    with TestClient(server.app) as c:
        c.server = server
        yield c


def _pair(client) -> tuple[str, str]:
    """Walk the real login flow; return (bearer, secret)."""
    pin = client.server.new_key()
    r   = client.post("/login", json={"pin": pin})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["token"], body["secret"]


def test_the_app_page_is_not_served_to_an_anonymous_visitor(client):
    """The defect verbatim: `/` used to return the whole application and let its
    own JavaScript decide whether to redirect."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_the_app_page_is_served_once_paired(client):
    _pair(client)                      # the login response sets the nav cookie
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "JARVIS" in r.text


def test_the_navigation_cookie_cannot_drive_the_api(client):
    """This is what makes the cookie safe to introduce at all.

    A page on another site can make the browser attach a cookie to a request.
    It cannot make the browser set an Authorization header. So navigation is
    authenticated by the cookie and the API is not — which leaves no CSRF to
    defend against rather than a defence to get right.
    """
    _pair(client)
    assert client.cookies.get("alexio_nav")               # the cookie is there…
    for path in ("/api/command", "/api/wake", "/api/revoke-devices", "/api/ws-ticket"):
        assert client.post(path, json={"text": "hello"}).status_code == 401, path
    assert client.get("/api/files").status_code == 401


def test_the_navigation_cookie_is_httponly_and_samesite_strict(client):
    pin = client.server.new_key()
    r   = client.post("/login", json={"pin": pin})
    raw = r.headers.get("set-cookie", "")
    assert "HttpOnly" in raw, "an XSS in the page could otherwise steal the cookie"
    assert "strict" in raw.lower()


def test_a_bearer_token_drives_the_api(client):
    bearer, _ = _pair(client)
    h = {"Authorization": f"Bearer {bearer}"}
    assert client.post("/api/wake", headers=h).status_code == 200
    assert client.post("/api/command", json={"text": "quelle heure est-il"},
                       headers=h).status_code == 200
    assert client.server._command_queue.get_nowait() == "quelle heure est-il"


def test_repeated_bad_keys_are_throttled(client):
    for _ in range(auth.FAIL_LIMIT):
        assert client.post("/login", json={"pin": "ZZZZZZ"}).status_code == 401
    r = client.post("/login", json={"pin": "ZZZZZZ"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_an_encrypted_command_round_trips(client):
    """End to end with the real cipher: if the key handling on either side
    drifts, this is what stops it shipping."""
    import base64, os
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad

    bearer, secret = _pair(client)
    key   = base64.b64decode(secret)
    iv    = os.urandom(16)
    pad   = sym_pad.PKCS7(128).padder()
    body  = pad.update("ouvre le navigateur".encode()) + pad.finalize()
    enc   = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    blob  = base64.b64encode(iv + enc.update(body) + enc.finalize()).decode()

    r = client.post("/api/command", json={"enc": blob},
                    headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 200
    assert client.server._command_queue.get_nowait() == "ouvre le navigateur"


def test_the_security_headers_are_present(client):
    r = client.get("/login")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    # The QR link carries the pairing PIN in its query string; without this the
    # first outbound request from that page puts the PIN in someone's logs.
    assert r.headers["Referrer-Policy"] == "no-referrer"
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_a_download_cannot_climb_out_of_the_uploads_folder(client, tmp_path):
    secret_file = tmp_path.parent / "not-yours.txt"
    secret_file.write_text("private", encoding="utf-8")
    _pair(client)
    for attempt in ("../not-yours.txt", "..%2Fnot-yours.txt", "....//not-yours.txt"):
        r = client.get(f"/uploads/{attempt}")
        assert r.status_code in (404, 401), f"{attempt} returned {r.status_code}"
        assert "private" not in r.text


def test_a_download_needs_a_session(client, tmp_path):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    assert client.get("/uploads/note.txt").status_code == 401
    _pair(client)
    r = client.get("/uploads/note.txt")
    assert r.status_code == 200 and r.text == "hello"


def test_a_websocket_needs_a_ticket_not_a_token(client):
    from starlette.websockets import WebSocketDisconnect

    bearer, _ = _pair(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?ticket={bearer}"):
            pass

    ticket = client.post("/api/ws-ticket",
                         headers={"Authorization": f"Bearer {bearer}"}).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}"):
        pass


def test_the_command_queue_is_bounded(client):
    """An authenticated phone holding the send button must not be able to grow
    a queue until the process dies."""
    from dashboard import server as srv

    assert client.server._command_queue.maxsize == srv.MAX_QUEUED_COMMANDS
    bearer, _ = _pair(client)
    h = {"Authorization": f"Bearer {bearer}"}
    codes = {client.post("/api/command", json={"text": f"x{i}"},
                         headers=h).status_code
             for i in range(srv.MAX_QUEUED_COMMANDS + 5)}
    assert 503 in codes, "the queue accepted more than its bound"


# ── 5. nothing asks to be root, nothing trusts a CDN ─────────────────────────

def test_the_dashboard_never_asks_for_elevation():
    """Four mechanisms, three platforms, one mistake.

    ShellExecuteW with the "runas" verb raises UAC regardless of the user's UAC
    level; pkexec and osascript's `with administrator privileges` are the same
    move on Linux and macOS. An assistant that asks to be root as a side effect
    of starting up trains the person in front of it to say yes.
    """
    code = code_only(SERVER)
    for forbidden in ("ShellExecuteW", "runas", "pkexec",
                      "with administrator privileges", "windll"):
        assert forbidden not in code, f"{forbidden} is back in dashboard/server.py"
    assert "'sudo'" not in code and '"sudo"' not in code, "sudo is back"


def test_the_firewall_is_only_touched_when_asked():
    src = SERVER.read_text(encoding="utf-8")
    assert "dashboard_open_firewall" in src, "the opt-in setting is gone"
    # …and the manual command is printed either way, because a "no" has to say
    # what would make it a yes.
    assert "netsh advfirewall firewall add rule" in src


def test_the_crypto_bundle_is_pinned_and_verified():
    """It used to be downloaded at import time and served unchecked, with a CDN
    redirect as the fallback — a third party writing JavaScript onto the page
    that holds the session key."""
    import base64, hashlib
    from dashboard import server as srv

    blob = (ROOT / "dashboard" / "static" / "crypto-js.min.js").read_bytes()
    digest = base64.b64encode(hashlib.sha512(blob).digest()).decode()
    assert digest == srv._CRYPTOJS_SHA512, "the cached bundle is not the pinned one"
    assert srv._digest_ok(blob) is True
    assert srv._digest_ok(blob + b"//x") is False

    src = SERVER.read_text(encoding="utf-8")
    assert "RedirectResponse(_CRYPTOJS_CDN" not in src, "the CDN fallback is back"


def test_importing_the_dashboard_does_not_touch_the_network():
    """`_ensure_crypto_js()` used to run at import. A module that reaches for
    the network when it is merely imported cannot be tested offline."""
    import ast

    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    calls = [n for n in tree.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    names = {n.value.func.id for n in calls if isinstance(n.value.func, ast.Name)}
    assert "_ensure_crypto_js" not in names


def test_the_credential_store_needs_nothing_but_the_standard_library():
    """It is where every expiry decision lives, so it has to be testable without
    a server, a socket, or a browser."""
    import ast

    tree = ast.parse((ROOT / "dashboard" / "auth.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"base64", "hashlib", "secrets", "string", "time",
                        "dataclasses", "__future__"}, sorted(imported)


def test_the_private_key_is_ignored_and_untracked():
    """The pair is generated per install precisely so that no private key ever
    ships. The .gitignore is what keeps it that way."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "config/certs/" in ignored
    assert "*.key" in ignored


def test_expiry_is_observable(store):
    """A number nobody can read is a number nobody trusts."""
    _, _, session = store.open_session()
    store.pair_device(session)
    stats = store.stats()
    assert stats["sessions"] == 1 and stats["devices"] == 1
