import json
import subprocess
import sys
import time
from pathlib import Path

# R-09 : ce module tape un message dans une fenêtre de messagerie. C'est
# exactement le cas où une frappe perdue est invisible — le texte n'arrive nulle
# part et rien ne le signale. La façade choisit un mécanisme qui atteint
# réellement la fenêtre, et refuse bruyamment quand il n'y en a pas.
from core import desktop


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_os() -> str:
    try:
        cfg = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return cfg.get("os_system", "windows").lower()
    except Exception:
        return "windows"


def _require_input():
    """Refuse before typing, with the reason and the fix — see
    actions/computer_control.py for why the message comes from the facade."""
    cap = desktop.capabilities()["input"]
    if not cap.available:
        raise RuntimeError(f"Cannot type the message: {cap.detail}")


def _modifier() -> str:
    """The chord modifier for this OS.

    "super" resolves to Command on macOS and to the Windows/Super key elsewhere,
    so callers stop branching on the OS to pick between "command" and "ctrl" —
    except that on macOS the *editing* chords really are Command and on the
    others they really are Ctrl, which is a different key, not a different name.
    """
    return "super" if _get_os() == "mac" else "ctrl"


def _paste_text(text: str) -> None:
    _require_input()
    try:
        desktop.clipboard_set(text)
        time.sleep(0.15)
        desktop.hotkey(_modifier(), "v")
        time.sleep(0.1)
    except Exception:
        # No clipboard on this machine — type it. Slower, and the only option
        # that still delivers the message.
        desktop.type_text(text, interval=0.03)


def _clear_and_paste(text: str) -> None:
    _require_input()
    desktop.hotkey(_modifier(), "a")
    time.sleep(0.1)
    desktop.key("delete")
    time.sleep(0.1)
    _paste_text(text)

def _open_app(app_name: str) -> bool:
    _require_input()
    os_name = _get_os()

    try:
        if os_name == "windows":
            desktop.key("win")
            time.sleep(0.5)
            _paste_text(app_name)
            time.sleep(0.6)
            desktop.key("enter")
            time.sleep(2.5)
            return True

        elif os_name == "mac":
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["open", "-a", f"{app_name}.app"],
                    capture_output=True, text=True, timeout=10,
                )
            time.sleep(2.5)
            return result.returncode == 0

        else: 
            launched = False
            for launcher in [
                ["gtk-launch", app_name.lower()],
                [app_name.lower()],
            ]:
                try:
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched = True
                    break
                except FileNotFoundError:
                    continue
            time.sleep(2.5)
            return launched

    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open {app_name}: {e}")
        return False


def _open_browser_url(url: str) -> bool:
    import webbrowser
    try:
        webbrowser.open(url)
        time.sleep(4.0) 
        return True
    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open browser: {e}")
        return False

def _search_in_app(query: str) -> None:
    _require_input()
    desktop.hotkey(_modifier(), "f")
    time.sleep(0.5)
    _clear_and_paste(query)
    time.sleep(1.0)

def _desktop_send(app_name: str, receiver: str, message: str) -> str:
    if not _open_app(app_name):
        return f"Could not open {app_name}."

    time.sleep(1.0)
    _search_in_app(receiver)
    desktop.key("enter")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    desktop.key("enter")
    time.sleep(0.3)
    return f"Message sent to {receiver} via {app_name}."

def _send_whatsapp(receiver: str, message: str) -> str:
    return _desktop_send("WhatsApp", receiver, message)

def _send_telegram(receiver: str, message: str) -> str:
    return _desktop_send("Telegram", receiver, message)

def _send_signal(receiver: str, message: str) -> str:
    return _desktop_send("Signal", receiver, message)


def _send_discord(receiver: str, message: str) -> str:
    return _desktop_send("Discord", receiver, message)


def _send_instagram(receiver: str, message: str) -> str:
    _require_input()

    if not _open_browser_url("https://www.instagram.com/direct/new/"):
        return "Could not open Instagram in browser."

    _paste_text(receiver)
    time.sleep(1.5)

    desktop.key("down")
    time.sleep(0.3)
    desktop.key("enter")   
    time.sleep(0.4)

    for _ in range(4):
        desktop.key("tab")
        time.sleep(0.15)
    desktop.key("enter")
    time.sleep(2.0)

    _paste_text(message)
    time.sleep(0.2)
    desktop.key("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Instagram."


def _send_messenger(receiver: str, message: str) -> str:
    _require_input()

    if not _open_browser_url("https://www.messenger.com/"):
        return "Could not open Messenger in browser."


    _search_in_app(receiver)
    time.sleep(0.5)
    desktop.key("down")
    time.sleep(0.3)
    desktop.key("enter")
    time.sleep(1.0)

    _paste_text(message)
    time.sleep(0.2)
    desktop.key("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Messenger."

_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp"},              _send_whatsapp),
    ({"telegram", "tg"},                      _send_telegram),
    ({"instagram", "ig", "insta"},            _send_instagram),
    ({"signal"},                               _send_signal),
    ({"discord"},                              _send_discord),
    ({"messenger", "facebook", "fb"},         _send_messenger),
]


def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, handler in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return handler
    return lambda r, m: _desktop_send(platform_str.strip().title(), r, m)


def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip()

    if not receiver:
        return "Please specify a recipient."
    if not message_text:
        return "Please specify the message content."
    cap = desktop.capabilities()["input"]
    if not cap.available:
        return f"Cannot send the message — no way to type on this machine: {cap.detail}"

    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(f"[SendMessage] 📨 {platform} → {receiver}: {preview}")
    if player:
        player.write_log(f"[msg] {platform} → {receiver}")

    try:
        handler = _resolve_platform(platform)
        result  = handler(receiver, message_text)
    except Exception as e:
        result = f"Could not send message: {e}"

    print(f"[SendMessage] {'✅' if 'sent' in result.lower() else '❌'} {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result