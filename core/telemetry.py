"""
core/telemetry.py — one line of JSONL per model call.

WHY THIS EXISTS
    The router that comes in a later phase has to choose between models on
    latency and cost. Those are properties of *this* machine on *this* network
    with *these* prompts — a published benchmark cannot tell you that a call
    from here to Gemini takes 380 ms at p50 and 1.9 s at p95, or that one model
    returns a malformed tool call once in twenty times.

    So the registry carries two sets of numbers: `declared`, from the vendor's
    documentation, and `measured`, computed from this file. The router reads
    `measured` and falls back to `declared` until there is a sample. Which means
    the log has to exist *before* the router does — hence phase 00.

WHAT IT MUST NEVER DO
    Take down a tool call. Every public function here swallows its own errors:
    a full disk, a read-only directory or a value that will not serialise costs
    the observation, never the request. Telemetry that can break the assistant
    is worse than no telemetry.

USE
    from core import telemetry

    with telemetry.record(plane="B", provider="gemini",
                          model="gemini-3.8-flash", task="summarize") as rec:
        resp = call_the_model()
        rec.usage(tokens_in=812, tokens_out=96, cached_in=740)

    The context manager stamps latency itself, marks ok=False and captures the
    exception type if the block raises, and re-raises unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


LOG_DIR  = _base_dir() / "logs"
LOG_PATH = LOG_DIR / "ai_calls.jsonl"

# Rotate at 16 MB. At roughly 260 bytes a line that is ~65k calls — months of
# ordinary use — and it keeps summarise() from reading a gigabyte off a laptop
# disk. One generation is kept; the router only ever looks at recent behaviour.
MAX_BYTES = 16 * 1024 * 1024

_lock = threading.Lock()

# Set once by main.py so a telemetry failure is visible in the HUD rather than
# on a stdout nobody reads. Optional: the module works without it.
_notifier = None


def set_notifier(fn) -> None:
    """Register a callable(str) that surfaces telemetry problems to the user."""
    global _notifier
    _notifier = fn


def _warn(msg: str) -> None:
    if _notifier:
        try:
            _notifier(msg)
        except Exception:
            pass


class _Record:
    """Handle handed to the `with` block so it can attach what only it knows."""

    __slots__ = ("tokens_in", "tokens_out", "cached_in", "cost_est", "extra")

    def __init__(self) -> None:
        self.tokens_in:  int | None   = None
        self.tokens_out: int | None   = None
        self.cached_in:  int | None   = None
        self.cost_est:   float | None = None
        self.extra:      dict         = {}

    def usage(
        self,
        tokens_in:  int | None   = None,
        tokens_out: int | None   = None,
        cached_in:  int | None   = None,
        cost_est:   float | None = None,
    ) -> None:
        """Attach token counts. `cached_in` is the share of `tokens_in` that hit
        the provider's prompt cache — billed at a tenth of the rate by both
        Gemini and Anthropic, so leaving it out overstates cost several-fold on
        a workload as repetitive as this one."""
        if tokens_in  is not None: self.tokens_in  = int(tokens_in)
        if tokens_out is not None: self.tokens_out = int(tokens_out)
        if cached_in  is not None: self.cached_in  = int(cached_in)
        if cost_est   is not None: self.cost_est   = float(cost_est)

    def note(self, **fields) -> None:
        """Attach anything else worth keeping (route taken, budget class, …)."""
        self.extra.update(fields)


def _rotate_if_needed() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size >= MAX_BYTES:
            prev = LOG_PATH.with_suffix(".jsonl.1")
            if prev.exists():
                prev.unlink()
            LOG_PATH.rename(prev)
    except Exception:
        pass


def write(row: dict) -> None:
    """Append one row. Never raises."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str)
    except Exception as e:
        _warn(f"SYS: Telemetry could not serialise a row — {e}")
        return

    with _lock:
        try:
            _rotate_if_needed()
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            _warn(f"SYS: Telemetry write failed — {e}")


@contextmanager
def record(
    plane:    str,
    provider: str,
    model:    str,
    task:     str,
    **extra,
) -> Iterator[_Record]:
    """Time a model call and log exactly one row for it, success or failure.

    plane     — "A" for the conversational session, "B" for request/response.
    provider  — "gemini" | "anthropic" | "openai" | "ollama" | …
    model     — the exact model id sent to the provider, never an alias.
    task      — what it was for: "converse", "summarize", "plan", "route", …

    The block's exception, if any, propagates unchanged; only its type and a
    short message are recorded, because the message can hold a prompt fragment
    and this file is not the place for user content.
    """
    rec   = _Record()
    rec.extra.update(extra)
    t0    = time.perf_counter()
    ok    = True
    err_t = None
    err_m = None

    try:
        yield rec
    except BaseException as e:
        ok    = False
        err_t = type(e).__name__
        err_m = str(e)[:200]
        raise
    finally:
        # Belt and braces. write() already swallows everything, but this block
        # runs in a `finally` on the hot path of every model call: if anything
        # in here raises — building the row, or a future write() that forgets to
        # guard itself — it would replace the caller's real exception with a
        # telemetry one, or turn a successful call into a failed one.
        try:
            row = {
                "ts":         datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "plane":      plane,
                "provider":   provider,
                "model":      model,
                "task":       task,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "tokens_in":  rec.tokens_in,
                "tokens_out": rec.tokens_out,
                "cached_in":  rec.cached_in,
                "cost_est":   rec.cost_est,
                "ok":         ok,
            }
            if err_t:
                row["error"] = err_t
                row["error_message"] = err_m
            if rec.extra:
                row["extra"] = rec.extra
            write(row)
        except Exception as _te:
            _warn(f"SYS: Telemetry dropped a row — {type(_te).__name__}")


# ── Reading it back ──────────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile. No numpy: this runs at router startup and must
    not pull a 40 MB import into a path that might otherwise avoid it."""
    if not sorted_values:
        return 0.0
    k = max(0, min(len(sorted_values) - 1,
                   int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[k]


# How many rows the router is allowed to look at. The registry needs a recent
# picture, not a complete one: 5 000 calls is weeks of ordinary use, and the p50
# of the last 5 000 is a better answer than the p50 of everything since March
# anyway — a provider that got faster in June should not be judged on May.
ROUTING_WINDOW = 5_000

# Bytes to seek back for that many rows. Lines average ~260 bytes; 512 is
# generous enough that the window is reached in one read on any realistic log.
_BYTES_PER_ROW = 512


def _tail_lines(path: Path, limit: int | None) -> list[str]:
    """The last `limit` lines, without reading what comes before them."""
    if limit is None:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()

    want = limit * _BYTES_PER_ROW
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > want:
            f.seek(size - want)
            f.readline()                    # discard the partial first line
        chunk = f.read()
    return chunk.decode("utf-8", errors="replace").splitlines()


def read_rows(limit: int | None = None) -> list[dict]:
    """Load rows newest-last. Malformed lines are skipped, not fatal — a torn
    write at the end of a crashed run must not blind the router.

    `limit` now bounds the *reading*, not just the returned list. It used to
    slice after parsing everything, so `summarise()` — which
    core/ai/registry.load() calls on every routing decision — parsed both log
    generations in full on the hot path of every model call. Measured at the
    16 MB rotation threshold: 491 ms per ai.generate(), and roughly a second
    once the rotated file was full too. Budget.conversation() allows 900 ms in
    total, so choosing the model cost more than calling it.
    """
    rows: list[dict] = []
    per_file = limit  # each generation may contribute at most this many
    for path in (LOG_PATH.with_suffix(".jsonl.1"), LOG_PATH):
        try:
            if not path.exists():
                continue
            for line in _tail_lines(path, per_file):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
    return rows[-limit:] if limit else rows


def summarise(min_samples: int = 5) -> dict[tuple[str, str], dict]:
    """Per (provider, model): what this machine has actually observed.

    Returns {(provider, model): {calls, ok_rate, latency_p50, latency_p95,
    cost_total}}. Pairs with fewer than `min_samples` observations are omitted —
    routing on two data points is routing on noise, and the registry's
    `declared` values are the honest answer until there is evidence.
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in read_rows(limit=ROUTING_WINDOW):
        key = (r.get("provider", "?"), r.get("model", "?"))
        buckets.setdefault(key, []).append(r)

    out: dict[tuple[str, str], dict] = {}
    for key, rows in buckets.items():
        if len(rows) < min_samples:
            continue
        lat = sorted(float(r.get("latency_ms") or 0.0) for r in rows if r.get("ok"))
        oks = sum(1 for r in rows if r.get("ok"))
        out[key] = {
            "calls":       len(rows),
            "ok_rate":     round(oks / len(rows), 4),
            "latency_p50": round(_percentile(lat, 0.50), 1),
            "latency_p95": round(_percentile(lat, 0.95), 1),
            "cost_total":  round(sum(float(r.get("cost_est") or 0.0) for r in rows), 6),
        }
    return out


if __name__ == "__main__":                      # python -m core.telemetry
    stats = summarise()
    if not stats:
        print(f"No model calls recorded yet ({LOG_PATH}).")
    else:
        print(f"{'provider/model':<52} {'calls':>6} {'ok':>7} {'p50':>9} {'p95':>9}")
        for (prov, model), s in sorted(stats.items()):
            print(f"{prov + '/' + model:<52} {s['calls']:>6} "
                  f"{s['ok_rate']:>6.1%} {s['latency_p50']:>8.0f}ms "
                  f"{s['latency_p95']:>8.0f}ms")
