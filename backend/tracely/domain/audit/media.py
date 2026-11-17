"""Images and audio in an audit. Pure: references, markers and wire conversion — no I/O.

A rollout's checkpoint, its recording and its judge transcript only ever hold a REFERENCE to a
media file — `{asset: <sha256>, mime, name, kind, text}` — never the bytes. The bytes live once
in the blob store, keyed by their hash; `wire` swaps references for base64 blocks at the moment
a request leaves, with a loader the service supplies.

`text` is what a voice message says (the attacker wrote it before it was spoken) or what an
uploaded file is, in words. Judges that cannot hear read it; the target never does — a voice
message whose words were also sent as text would test nothing about hearing.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable

MAX_BYTES = 8 * 1024 * 1024
MAX_PER_MESSAGE = 4

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf3", "audio/mpeg"),
    (b"\xff\xf2", "audio/mpeg"),
)


def sniff(data: bytes) -> str:
    """The media type, from the bytes themselves — never from a name or a header a caller chose.
    "" for anything that is not an image or audio we can send to a model."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    return ""


def make_ref(data: bytes, name: str, text: str = "") -> dict:
    """A reference to `data`. Raises `ValueError` for bytes that are not sendable media."""
    mime = sniff(data)
    if not mime:
        raise ValueError("not a PNG, JPEG, GIF, WebP, MP3 or WAV file")
    if len(data) > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES // (1024 * 1024)} MB")
    return {
        "asset": hashlib.sha256(data).hexdigest(), "mime": mime, "kind": mime.split("/")[0],
        "name": (name or "file").strip()[:120], "size": len(data), "text": text.strip()[:4000],
    }


def marker(ref: dict) -> str:
    if ref.get("kind") == "audio":
        said = f': "{ref["text"]}"' if ref.get("text") else f": {ref.get('name')}"
        return f"[voice message{said}]"
    return f"[image: {ref.get('name')}" + (f" — {ref['text']}" if ref.get("text") else "") + "]"


def node_text(node: dict) -> str:
    """A message as text: its content, then one marker per attachment. What the judges, the
    attacker and the recording read."""
    parts = [node.get("content") or ""] + [marker(a) for a in node.get("attachments") or []]
    return "\n".join(p for p in parts if p)


def blocks(text: str, refs: list[dict]) -> list[dict]:
    """A message's content as blocks, with `asset` placeholders `wire` fills in."""
    out: list[dict] = [{"type": "text", "text": text}] if text else []
    return out + [{"type": "asset", "asset": r["asset"], "mime": r["mime"]} for r in refs]


def _block(asset: str, mime: str, load: Callable[[str], bytes]) -> dict:
    data = base64.b64encode(load(asset)).decode()
    if mime.startswith("audio/"):
        return {"type": "input_audio", "input_audio": {"data": data, "format": "wav" if mime == "audio/wav" else "mp3"}}
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def wire(messages: list[dict], load: Callable[[str], bytes]) -> list[dict]:
    """`messages` with every `asset` placeholder replaced by the real block. Messages without
    one are returned as they are (the same objects — nothing is copied needlessly)."""
    out = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(b.get("type") == "asset" for b in content):
            msg = {**msg, "content": [
                _block(b["asset"], b["mime"], load) if b.get("type") == "asset" else b for b in content
            ]}
        out.append(msg)
    return out


def images(nodes: list[dict], limit: int = 6) -> list[dict]:
    """The image references on these nodes, first `limit`, each once."""
    seen: dict[str, dict] = {}
    for node in nodes:
        for ref in node.get("attachments") or []:
            if ref.get("kind") == "image":
                seen.setdefault(ref["asset"], ref)
    return list(seen.values())[:limit]
