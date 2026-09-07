"""
core/ai/types.py — the vocabulary the rest of Alexio speaks to models in.

Nothing here mentions a vendor. That is the whole point: an action says what it
needs (a fast answer, an image looked at, a live web fact) and never which
company answers it.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path


class Tier:
    """How much model a task deserves.

    Named for the *job*, not for a price bracket, because the mapping from tier
    to model id changes every few months and the call sites must not.

      FAST     — extraction, classification, reformatting. Cheap and immediate.
      STANDARD — the default. Summaries, explanations, code review.
      DEEP     — planning, multi-file projects, anything worth waiting for.
    """
    FAST     = "fast"
    STANDARD = "standard"
    DEEP     = "deep"

    ALL = (FAST, STANDARD, DEEP)


@dataclass(frozen=True)
class Media:
    """Something to look at or listen to: bytes plus its type.

    Not "Image" as the underlying name, because actions/file_processor.py also
    hands models audio to transcribe. One type covers both, and `capability()`
    below is what tells the gateway whether the request needs a provider that
    can see or one that can hear — those are different capabilities and a
    provider may have one without the other.

    Bytes rather than a PIL object on purpose: PIL is an optional dependency,
    and the gateway must not force it on a machine that only ever sends text.
    """
    data:      bytes
    mime_type: str = "image/png"

    def capability(self) -> str:
        head = (self.mime_type or "").split("/")[0].lower()
        return {"image": "vision", "audio": "audio", "video": "video"}.get(head, "vision")

    @classmethod
    def from_path(cls, path: str | Path) -> "Media":
        p = Path(path)
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return cls(data=p.read_bytes(), mime_type=mime)

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str) -> "Media":
        return cls(data=data, mime_type=mime_type)

    @classmethod
    def from_pil(cls, img, fmt: str = "PNG") -> "Media":
        """Accept a PIL Image without importing PIL at module level."""
        import io
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return cls(data=buf.getvalue(), mime_type=f"image/{fmt.lower()}")


# The overwhelmingly common case reads better spelled out, and `ai.Image.from_path`
# is what a call site looking at a screenshot wants to say.
Image = Media


@dataclass(frozen=True)
class Usage:
    """What the call consumed. `cached_in` is the share of `tokens_in` that hit
    the provider's prompt cache — billed at a tenth of the rate by both Gemini
    and Anthropic, so omitting it overstates cost several-fold on a workload as
    repetitive as this one."""
    tokens_in:  int | None = None
    tokens_out: int | None = None
    cached_in:  int | None = None


@dataclass(frozen=True)
class Completion:
    """One answer. `text` is what almost every call site wants.

    Carries which model actually answered so a caller can log it, and so the
    registry in a later phase can attribute measured latency correctly when the
    router starts choosing between several.
    """
    text:     str
    provider: str
    model:    str
    usage:    Usage = field(default_factory=Usage)

    def __str__(self) -> str:
        return self.text

    @property
    def stripped(self) -> str:
        return self.text.strip()
