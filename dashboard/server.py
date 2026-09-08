"""
dashboard/server.py — the remote dashboard: a phone on the same network drives
Alexio through a browser.

PHASE 06 — WHAT WAS HARDENED AND WHY
    This is the only part of Alexio that listens on a socket, so it is the only
    part where a mistake is reachable by someone who is not already sitting at
    the keyboard. Five things were wrong, and each had the same shape: a control
    that looked present and was not.

    1. The AES key was SHA-256(six-character PIN ‖ a salt written in this file).
       Under a billion possibilities, one hash per guess, same salt everywhere.
       The PIN is now a *pairing* credential only: it is compared, never
       stretched into a key, and pairing hands back 256 real random bits. See
       dashboard/auth.py.

    2. Device tokens never expired. Now: thirty days absolute, seven days idle,
       at most eight paired devices, stored hashed, revocable.

    3. `GET /` returned the full application to anyone who asked, and the page
       redirected itself to /login in JavaScript if it did not like what it
       found in sessionStorage. That is a suggestion, not a gate. It is now
       checked on the server, against an HttpOnly cookie — with the API still
       requiring the Authorization header, so the cookie can never be used to
       forge an API call from another site.

    4. The firewall was opened automatically, with elevation: a UAC dialog on
       Windows, pkexec or sudo on Linux, an admin prompt on macOS. An assistant
       should not ask for administrator rights as a side effect of starting.
       It now prints the exact command and leaves the decision alone; setting
       `dashboard_open_firewall` in config/api_keys.json opts back in, and even
       then it never elevates.

    5. Nothing counted failed logins, and the CryptoJS bundle was downloaded
       from a CDN at import time and served with no integrity check at all.
       Both are fixed below.

WHAT IS STILL TRUE, AND SHOULD BE SAID
    Payload encryption over plain HTTP does not protect against someone who
    watched the pairing. Over TLS — which is generated on first run and is the
    default — it is defence in depth. Over HTTP it is a second lock on the same
    door. The dashboard prefers HTTPS for that reason.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import socket
from pathlib import Path

from dashboard.auth import CredentialStore, Session

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import (HTMLResponse, JSONResponse, FileResponse,
                                   RedirectResponse, Response)
    import uvicorn
    _DEPS_OK = True
except Exception:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR      = Path(__file__).resolve().parent.parent
STATIC_DIR    = Path(__file__).parent / "static"
PORT          = 8000
MAX_UPLOAD_MB = 500
NAV_COOKIE    = "alexio_nav"

# A queue with no bound is a memory leak with an authenticated trigger: a phone
# holding the send button while the assistant is busy would grow it forever.
MAX_QUEUED_COMMANDS = 200


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()


def _setting(name: str, default=None):
    """Read one key out of config/api_keys.json.

    Anything that changes the security posture — opening a firewall port, in
    practice — has to be something the user turned on, not something the code
    decided. Missing file, missing key, unreadable JSON: the default wins, and
    every default here is the conservative one.
    """
    try:
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return json.load(f).get(name, default)
    except Exception:
        return default


# ── AES-256-CBC ───────────────────────────────────────────────────────────────
#
# The key is now 32 bytes straight out of the credential store — no derivation,
# no salt, nothing to precompute. What is left here is only transport.

def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


# ── CryptoJS — cached locally, and checked ────────────────────────────────────
#
# The old version downloaded this from a CDN and served whatever came back, then
# fell back to redirecting the browser to the CDN if the download had failed.
# Both are ways of letting a third party put JavaScript on a page that holds the
# session key. The hash below is the published Subresource Integrity digest for
# crypto-js 4.2.0 on cdnjs, verified against the copy in this repository on
# 2026-09-07. A file that does not match it is not served — offline is a better
# outcome than running an unknown script.

_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"
_CRYPTOJS_SHA512 = ("a+SUDuwNzXDvz4XrIcXHuCf089/iJAoN4lmrXJg18XnduKK6YlDHNRalv"
                    "4yd1N40OKI80tFidF+rqTFKGPoWFQ==")


def _digest_ok(data: bytes) -> bool:
    return secrets.compare_digest(
        base64.b64encode(hashlib.sha512(data).digest()).decode(), _CRYPTOJS_SHA512
    )


# (mtime, size) of the copy last verified. The file is written once by
# _ensure_crypto_js and then served on every page load; re-reading and
# re-hashing 50 kB per request bought nothing, because a file that changed would
# change its mtime too. Keyed on the stat rather than a plain flag so a
# replacement is still caught.
_verified_stat: tuple[float, int] | None = None


def _cached_digest_ok(path: Path) -> bool:
    global _verified_stat
    try:
        st = path.stat()
    except OSError:
        return False
    key = (st.st_mtime, st.st_size)
    if _verified_stat == key:
        return True
    if _digest_ok(path.read_bytes()):
        _verified_stat = key
        return True
    _verified_stat = None
    return False


def _ensure_crypto_js() -> bool:
    """Make sure the local CryptoJS copy is the one we expect.

    Called from serve(), not at import: a module that reaches for the network
    when it is merely imported cannot be tested offline, and turns `import
    dashboard.server` into an outbound connection nobody asked for.
    """
    if _CRYPTOJS_FILE.exists():
        if _digest_ok(_CRYPTOJS_FILE.read_bytes()):
            return True
        print("[Dashboard] Cached CryptoJS does not match the expected digest — discarding.")
        try:
            _CRYPTOJS_FILE.unlink()
        except Exception:
            return False

    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        with urllib.request.urlopen(_CRYPTOJS_CDN, timeout=20) as r:
            data = r.read()
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print("[Dashboard] Commands will be sent unencrypted — use the HTTPS URL.")
        return False

    if not _digest_ok(data):
        print("[Dashboard] Downloaded CryptoJS failed its integrity check — refusing it.")
        return False

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    _CRYPTOJS_FILE.write_bytes(data)
    print("[Dashboard] CryptoJS verified and cached — served locally from now on.")
    return True


# ── firewall: tell, do not take ───────────────────────────────────────────────

def _firewall_hint(first: int, last: int) -> None:
    """Say what would open the ports, and say it once.

    Both ports at once — the dashboard listens on PORT and, when TLS is on, on
    PORT+1 as well. Two separate prompts for the same decision is how a warning
    becomes wallpaper.

    The previous version wrote a .bat file and ran it through ShellExecuteW with
    the "runas" verb — a UAC prompt raised by an assistant that was only meant
    to start listening. On Linux it tried pkexec and then sudo; on macOS an
    osascript admin dialog. Three platforms, one mistake: a program that starts
    by asking to be root teaches the user to say yes to that question.

    So this prints. Setting `dashboard_open_firewall: true` in
    config/api_keys.json makes it try the command directly — which succeeds when
    Alexio is already running with the rights and fails cleanly when it is not.
    It never elevates.
    """
    import sys, subprocess

    opt_in = bool(_setting("dashboard_open_firewall", False))
    rule   = "JARVIS Dashboard"

    def _run(cmd: list[str]) -> bool:
        """Run it as we are. Never with elevation, never through a helper that
        prompts — if the rights are not already there, the answer is the printed
        command, not a dialog."""
        try:
            return subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
        except Exception:
            return False

    def _tell(manual: str) -> None:
        print("[Dashboard] The phone cannot reach this machine until the firewall "
              f"allows ports {first}-{last}. To allow them:")
        print(f"[Dashboard]   {manual}")

    if sys.platform == "win32":
        ports  = f"{first}-{last}"
        manual = (f'netsh advfirewall firewall add rule name="{rule}" '
                  f"protocol=TCP dir=in localport={ports} action=allow")
        try:    # already there? then there is nothing to say
            r = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and "No rules match" not in r.stdout:
                return
        except Exception:
            pass
        if opt_in and _run(["netsh", "advfirewall", "firewall", "add", "rule",
                            f"name={rule}", "protocol=TCP", "dir=in",
                            f"localport={ports}", "action=allow"]):
            print(f"[Dashboard] Firewall rule added for ports {ports}.")
            return
        _tell(f"{manual}    (in an Administrator prompt)")
        return

    if sys.platform == "darwin":
        print("[Dashboard] If the macOS firewall blocks the connection, allow "
              "Python in System Settings › Network › Firewall.")
        return

    # Linux — name the firewall that is actually running, not all three.
    for probe, active, cmd, manual in (
        (["ufw", "status"], "active",
         ["ufw", "allow", f"{first}:{last}/tcp"],
         f"sudo ufw allow {first}:{last}/tcp"),
        (["firewall-cmd", "--state"], "running",
         ["firewall-cmd", "--add-port", f"{first}-{last}/tcp", "--permanent"],
         f"sudo firewall-cmd --add-port={first}-{last}/tcp --permanent "
         f"&& sudo firewall-cmd --reload"),
    ):
        try:
            r = subprocess.run(probe, capture_output=True, text=True, timeout=5)
        except Exception:      # not installed, or not answering — try the next
            continue
        if active not in r.stdout.lower():
            continue
        if opt_in and _run(cmd):
            print(f"[Dashboard] Ports {first}-{last} allowed.")
            return
        _tell(manual)
        return


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._creds                       = CredentialStore()
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue(maxsize=MAX_QUEUED_COMMANDS)
        self._wake_callback               = None
        self._connect_callback            = None
        self._phone_audio_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._uploads_dir                 = UPLOADS_DIR
        self._uploads_root                = UPLOADS_DIR.resolve()
        self._startup_tasks: list          = []
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        """The six characters shown in the desktop UI and encoded in the QR."""
        return self._creds.new_pairing_key(expiry_secs)

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── command intake ───────────────────────────────────────────────────

    def _enqueue(self, text: str) -> bool:
        """Drop rather than block when the queue is full.

        The alternative is awaiting a put() inside a request handler, which
        holds the connection open and lets one impatient client stall the
        others — R-07 in a place that is easy to miss because nothing here
        looks blocking.
        """
        try:
            self._command_queue.put_nowait(text)
        except asyncio.QueueFull:
            return False
        if self._wake_callback:
            self._wake_callback()
        return True

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        # Whether this response is going out over TLS is a property of the
        # request, not of the process: the certificate is generated inside
        # serve(), *after* this function runs, so reading it once here would
        # have marked every cookie non-Secure on the very first launch — the
        # one run where it matters most.
        def _is_secure(req: "Request") -> bool:
            return req.url.scheme in ("https", "wss")

        # Content-Security-Policy is the one that earns its place: it stops the
        # page loading script from anywhere but this server, which is what makes
        # the pinned CryptoJS copy meaningful. The pages use inline <script> and
        # inline handlers, so 'unsafe-inline' has to stay until they are split
        # out — but no external origin can contribute code either way.
        _CSP = ("default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "media-src 'self' blob:; "
                "connect-src 'self' ws: wss:; "
                "object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'")

        @app.middleware("http")
        async def security_headers(request: "Request", call_next):
            response = await call_next(request)
            response.headers["Content-Security-Policy"]   = _CSP
            response.headers["X-Content-Type-Options"]    = "nosniff"
            response.headers["X-Frame-Options"]           = "DENY"
            # The QR link carries the pairing PIN in its query string. Without
            # this, the first outbound request from that page would put the PIN
            # in someone else's logs.
            response.headers["Referrer-Policy"]           = "no-referrer"
            response.headers["Permissions-Policy"]        = "geolocation=(), camera=()"
            response.headers.setdefault("Cache-Control", "no-store")
            if _is_secure(request):
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
            return response

        def _bearer(req: "Request") -> str:
            return req.headers.get("authorization", "").removeprefix("Bearer ").strip()

        def _auth(req: "Request") -> Session | None:
            """API authentication — the Authorization header, and nothing else.

            Deliberately does not accept the navigation cookie. A cross-site
            page can make a browser send a cookie; it cannot make it set a
            header. Keeping the two apart is what makes every endpoint below
            CSRF-proof without a single token in a form.
            """
            return self._creds.session_for_bearer(_bearer(req))

        def _who(req: "Request") -> str:
            return req.client.host if req.client else "?"

        def _note_failure(req: "Request") -> None:
            if self._creds.throttle.fail(_who(req)):
                burned = self._creds.burn_pairing_keys()
                if burned:
                    print(f"[Dashboard] Repeated bad logins — {burned} pending key(s) "
                          f"discarded. Press 'Remote Control' for a new one.")

        def _set_nav_cookie(req: "Request", resp: "Response", nav: str) -> None:
            resp.set_cookie(
                NAV_COOKIE, nav,
                httponly=True,          # unreachable from JavaScript, so an XSS
                                        # in the page cannot walk off with it
                samesite="strict",      # never attached to a cross-site request
                secure=_is_secure(req),
                path="/",
                max_age=12 * 3600,
            )

        def _connected(what: str) -> None:
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast({"type": "sys", "text": what}))

        # ── static ───────────────────────────────────────────────────────────

        @app.get("/static/crypto.js")
        async def serve_crypto():
            """Served only if the local copy matches the pinned digest.

            No CDN redirect fallback: sending the browser to fetch script from
            somewhere else is precisely the thing the CSP above forbids, and a
            page that silently loses its encryption is worse than one that says
            NO ENC in the corner — which is what the client does when this 404s.
            """
            if _CRYPTOJS_FILE.exists() and _cached_digest_ok(_CRYPTOJS_FILE):
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            return JSONResponse({"error": "crypto bundle unavailable"}, status_code=503)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index(req: Request):
            """Gated on the server, against an HttpOnly cookie.

            The old version served this page to anyone and let its own
            JavaScript decide whether to redirect. Anything that ships to the
            browser before the check is a check that did not happen.
            """
            if self._creds.session_for_nav(req.cookies.get(NAV_COOKIE, "")) is None:
                return RedirectResponse("/login", status_code=303)
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        # ── pairing ──────────────────────────────────────────────────────────

        @app.post("/login")
        async def login(req: Request):
            wait = self._creds.throttle.retry_after(_who(req))
            if wait > 0:
                return JSONResponse(
                    {"ok": False, "error": "Too many attempts"},
                    status_code=429, headers={"Retry-After": str(int(wait) + 1)},
                )
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)

            entered = str(body.get("pin", "")).strip().upper()
            if not self._creds.redeem_pairing_key(entered):
                _note_failure(req)
                return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                    status_code=401)

            self._creds.throttle.clear(_who(req))
            bearer, nav, session = self._creds.open_session()
            _connected("Remote connection established.")
            resp = JSONResponse({"ok": True, "token": bearer, "secret": session.secret})
            _set_nav_cookie(req, resp, nav)
            return resp

        @app.get("/auto-login", response_class=HTMLResponse)
        async def auto_login_page(req: Request, key: str = ""):
            """QR code target. Renders; it does not spend anything.

            This used to redeem the pairing PIN, mint a session and pair a
            device — all on a GET. A browser prefetching the QR link, a crawler,
            a chat client generating a link preview, or a security scanner would
            each burn a live single-use credential just by looking at it, and
            the user would be told the link had expired.

            GET is the one verb the network issues without being asked, so the
            page now only carries the key to the POST below. The trip costs one
            round-trip on the phone and nothing anywhere else.
            """
            safe_key = re.sub(r"[^A-Z0-9]", "", key.strip().upper())[:6]
            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  h2{{color:#f87171;margin-bottom:12px}}p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body><div id="s"><p>Connecting to JARVIS…</p></div>
<script>
  fetch('/auto-login', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key: '{safe_key}'}})
  }}).then(function(r){{ return r.ok ? r.json() : Promise.reject(r); }})
    .then(function(d){{
      sessionStorage.setItem('jarvis_token', d.token);
      sessionStorage.setItem('jarvis_secret', d.secret);
      localStorage.setItem('jarvis_device_token', d.device);
      location.replace('/');
    }})
    .catch(function(){{
      document.getElementById('s').innerHTML =
        '<h2>Link Expired</h2><p>Press <strong style="color:#dde3ed">Remote Control</strong>'
        + ' in JARVIS to get a new QR code.</p>';
    }});
</script>
</body></html>""")

        @app.post("/auto-login")
        async def auto_login(req: Request):
            """Spend the pairing key. POST, because this changes state."""
            if self._creds.throttle.retry_after(_who(req)) > 0:
                return JSONResponse({"ok": False}, status_code=429)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)

            key = str(body.get("key", "")).strip().upper()
            if not self._creds.redeem_pairing_key(key):
                if key:
                    _note_failure(req)
                return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                    status_code=401)

            self._creds.throttle.clear(_who(req))
            bearer, nav, session = self._creds.open_session()
            device = self._creds.pair_device(session)
            _connected("Remote connection established via QR code.")
            resp = JSONResponse({"ok": True, "token": bearer,
                                 "secret": session.secret, "device": device})
            _set_nav_cookie(req, resp, nav)
            return resp

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh session for a previously paired device token."""
            wait = self._creds.throttle.retry_after(_who(req))
            if wait > 0:
                return JSONResponse({"ok": False}, status_code=429,
                                    headers={"Retry-After": str(int(wait) + 1)})
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)

            opened = self._creds.open_session_for_device(
                (body.get("device_token") or "").strip())
            if opened is None:
                _note_failure(req)
                return JSONResponse({"ok": False}, status_code=401)

            bearer, nav, session = opened
            _connected("Known device reconnected automatically.")
            resp = JSONResponse({"ok": True, "token": bearer, "secret": session.secret})
            _set_nav_cookie(req, resp, nav)
            return resp

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if _auth(req) is None:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({"ok": True, "revoked": self._creds.revoke_devices()})

        @app.get("/api/sessions")
        async def sessions_ep(req: Request):
            """What credentials are outstanding right now.

            Expiry that cannot be observed is expiry nobody believes in.
            """
            if _auth(req) is None:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse(self._creds.stats())

        # ── commands ─────────────────────────────────────────────────────────

        @app.post("/api/command")
        async def command(req: Request):
            session = _auth(req)
            if session is None:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            enc = body.get("enc", "")
            if enc:
                try:
                    text = _decrypt_cbc(session.key_bytes(), enc)
                except Exception:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text and not self._enqueue(text):
                return JSONResponse({"error": "Busy — command dropped"}, status_code=503)
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if _auth(req) is None:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/ws-ticket")
        async def ws_ticket(req: Request):
            """Trade the bearer token for a thirty-second, single-use ticket.

            A WebSocket handshake from a browser cannot carry an Authorization
            header, so something has to go in the query string. A ticket that is
            dead before the log file is rotated is a much smaller thing to leave
            there than a twelve-hour session token.
            """
            ticket = self._creds.new_ticket(_bearer(req))
            if ticket is None:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({"ticket": ticket})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, ticket: str = ""):
            if self._creds.redeem_ticket(ticket.strip()) is None:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
            return name or "upload"

        def _inside_uploads(name: str) -> Path | None:
            """Resolve a name against the uploads folder, or refuse.

            The character filter above is already strict, but it is a denylist,
            and a denylist is a claim about every input nobody has thought of
            yet. This is the check that does not depend on being clever: after
            resolving symlinks and `..`, is the result still under the folder?
            """
            try:
                candidate = (self._uploads_dir / name).resolve()
            except Exception:
                return None
            if candidate == self._uploads_root or not candidate.is_relative_to(self._uploads_root):
                return None
            return candidate

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if _auth(req) is None:
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = _inside_uploads(safe)
                if dest is None:
                    return JSONResponse({"error": "Invalid filename"}, status_code=400)
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if _auth(req) is None:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(req: Request, filename: str):
            """Authenticated by the navigation cookie, not a token in the URL.

            A download is a navigation: `<a download>` sends cookies and cannot
            send headers. The old version put the session token in the query
            string of every file link, which put it in the browser history and
            anything that logs URLs. The cookie cannot be used to *read* the
            response from another origin, so nothing is lost.
            """
            if (self._creds.session_for_nav(req.cookies.get(NAV_COOKIE, "")) is None
                    and _auth(req) is None):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            path = _inside_uploads(_safe_filename(filename))
            if path is None or not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=path.name)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, ticket: str = ""):
            session = self._creds.redeem_ticket(ticket.strip())
            if session is None:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") != "command":
                        continue
                    enc = data.get("enc", "")
                    if enc:
                        try:
                            text = _decrypt_cbc(session.key_bytes(), enc)
                        except Exception:
                            continue
                    else:
                        text = (data.get("text") or "").strip()
                    if text:
                        self._enqueue(text)
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        loop = asyncio.get_running_loop()

        # Both of these touch the network or a subprocess, so neither belongs on
        # the event loop (R-07). Neither blocks startup either — uvicorn is up
        # before they finish.
        # Kept, not awaited. Neither should block startup, but discarding the
        # Future means an exception inside either is never retrieved and never
        # printed — a read-only cache directory or a firewall probe that raises
        # would simply produce nothing at all.
        def _report(fut, what=""):
            exc = fut.exception()
            if exc is not None:
                print(f"[Dashboard] {what} failed: {type(exc).__name__}: {exc}")

        self._startup_tasks = [
            loop.run_in_executor(None, _ensure_crypto_js),
            loop.run_in_executor(None, _firewall_hint, PORT, PORT + 1),
        ]
        for fut, what in zip(self._startup_tasks, ("CryptoJS setup", "firewall check")):
            fut.add_done_callback(lambda f, w=what: _report(f, w))

        # Generate the TLS pair on first run so no private key ships in the repo.
        await loop.run_in_executor(None, _ensure_certs)

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        if not use_ssl:
            print("[Dashboard] Plain HTTP: anyone on this network can read the pairing.")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
