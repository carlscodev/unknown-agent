"""Upload storage and attachment consumption for the web panel.

Uploads are not decorative: text-ish files and PDFs are extracted and injected
into the model context, images travel through ``Message.images`` (both provider
dialects already render them as native vision parts), and everything else is
persisted under the data dir with its path referenced in the prompt so agent
tooling and MCP servers can open the real file.
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from typing import Any

EXTRACT_CHARS = 6000

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".log", ".py", ".js", ".ts",
    ".html", ".css", ".toml", ".yaml", ".yml", ".sh", ".sql", ".xml", ".rst",
}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi"}

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return "document"


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


class AttachmentStore:
    """Persists uploads under ``<data>/web_uploads`` and serves them back by id."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._meta: dict[str, dict[str, Any]] = {}

    def save(self, blob: bytes, name: str) -> dict[str, Any]:
        safe = "".join(ch for ch in Path(name).name if ch.isalnum() or ch in "._-")[:80] or "file"
        aid = secrets.token_hex(6)
        target = self.root / f"{aid}-{safe}"
        target.write_bytes(blob)
        meta = {
            "id": aid,
            "name": name,
            "kind": kind_of(name),
            "size": len(blob),
            "size_human": _human(len(blob)),
            "path": str(target),
        }
        self._meta[aid] = meta
        return meta

    def get(self, aid: str) -> dict[str, Any] | None:
        return self._meta.get(aid)

    # -- consumption -------------------------------------------------------- #
    def data_url(self, meta: dict[str, Any]) -> str:
        mime = _IMAGE_MIME.get(Path(meta["name"]).suffix.lower(), "image/png")
        blob = Path(meta["path"]).read_bytes()
        return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")

    def extract_text(self, meta: dict[str, Any]) -> str:
        """Best-effort text for the model context; '' when nothing readable."""
        path = Path(meta["path"])
        kind = meta["kind"]
        try:
            if kind == "text":
                return path.read_text(encoding="utf-8", errors="replace")[:EXTRACT_CHARS]
            if kind == "pdf":
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                out: list[str] = []
                for page in reader.pages[:3]:
                    out.append(page.extract_text() or "")
                return "\n".join(out)[:EXTRACT_CHARS]
        except Exception:
            return ""
        return ""

    def context_block(self, meta: dict[str, Any], *, auto_extract: bool) -> str:
        label = f"{meta['name']} ({meta['kind']}, {meta['size_human']})"
        if meta["kind"] == "image":
            return f"[attached image: {label}] — included in this request as vision input."
        text = self.extract_text(meta) if auto_extract else ""
        if text.strip():
            return (
                f"[attached file: {label}] — extracted content:\n"
                f"```\n{text.strip()}\n```"
            )
        return (
            f"[attached file: {label}] — stored at {meta['path']}; "
            "agent tooling and MCP tools can read the real file."
        )


__all__ = ["EXTRACT_CHARS", "AttachmentStore", "kind_of"]
