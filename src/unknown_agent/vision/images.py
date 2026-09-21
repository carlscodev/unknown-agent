"""Image ingestion: anything the user or a tool points at -> a safe attachment.

Accepted inputs and what happens to them:

* a filesystem path      -> validated (magic bytes, size), base64 data URL
* an existing data URL   -> validated shape, passed through
* an http(s) URL         -> passed through *as a URL* (the provider fetches);
                            allowed only where [vision] permits remote URLs

Size discipline: files above ``max_image_bytes`` are downscaled when Pillow is
available, otherwise refused with the exact limit in the message.  Screenshots
from the browser gate are typically PNGs well under the cap.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.config.models import VisionConfig
from unknown_agent.errors import ToolError

__all__ = ["ImageAttachment", "load_image", "data_url_for", "sniff_format", "image_dimensions"]

#: Magic bytes -> (format, mime).  Only formats the major vision APIs accept.
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpeg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"RIFF", "webp", "image/webp"),  # RIFF....WEBP checked below
)


def sniff_format(blob: bytes) -> tuple[str, str] | None:
    """Return ``(format, mime)`` from magic bytes, or None."""

    for magic, fmt, mime in _MAGIC:
        if blob.startswith(magic):
            if fmt == "webp" and blob[8:12] != b"WEBP":
                continue
            return fmt, mime
    return None


def image_dimensions(blob: bytes) -> tuple[int, int] | None:
    """Width/height without Pillow, for the formats we accept.  Best-effort."""

    sniffed = sniff_format(blob)
    if sniffed is None:
        return None
    fmt = sniffed[0]
    try:
        if fmt == "png" and len(blob) >= 24:
            width, height = struct.unpack(">II", blob[16:24])
            return int(width), int(height)
        if fmt == "gif" and len(blob) >= 10:
            width, height = struct.unpack("<HH", blob[6:10])
            return int(width), int(height)
        if fmt == "jpeg":
            index = 2
            while index + 9 < len(blob):
                if blob[index] != 0xFF:
                    index += 1
                    continue
                marker = blob[index + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    height, width = struct.unpack(">HH", blob[index + 5 : index + 9])
                    return int(width), int(height)
                length = struct.unpack(">H", blob[index + 2 : index + 4])[0]
                index += 2 + length
    except (struct.error, IndexError):  # pragma: no cover - malformed headers
        return None
    return None


@dataclass(slots=True)
class ImageAttachment:
    """One provider-ready image plus everything the audit trail wants."""

    #: What goes on the wire: a data URL or an https URL.
    url: str
    source: str            # original path/URL the user gave
    kind: str              # "file" | "data-url" | "url"
    mime: str = ""
    bytes_size: int = 0
    width: int | None = None
    height: int | None = None
    downscaled: bool = False
    #: Path of the audit copy under artifacts/, when one was kept.
    artifact: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "mime": self.mime,
            "bytes": self.bytes_size,
            "width": self.width,
            "height": self.height,
            "downscaled": self.downscaled,
            "artifact": self.artifact,
        }

    @property
    def label(self) -> str:
        dims = f" {self.width}x{self.height}" if self.width else ""
        return f"{self.source} ({self.mime or 'image'}{dims}, {self.bytes_size or '?'} bytes)"


def pillow_available() -> bool:
    """True when Pillow is importable (downscaling works)."""

    try:
        import PIL  # noqa: F401  type: ignore[import-not-found]
    except ImportError:
        return False
    return True


def data_url_for(blob: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _try_downscale(blob: bytes, *, target_px: int) -> tuple[bytes, str, bool]:
    """Downscale with Pillow when available; return ``(blob, mime, changed)``."""

    try:
        import io

        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return blob, "", False
    try:
        with Image.open(io.BytesIO(blob)) as image:
            width, height = image.size
            longest = max(width, height)
            if longest <= target_px:
                return blob, "", False
            ratio = target_px / float(longest)
            resized = image.resize((max(1, int(width * ratio)), max(1, int(height * ratio))))
            buffer = io.BytesIO()
            if resized.mode in ("RGBA", "P"):
                resized.save(buffer, format="PNG", optimize=True)
                return buffer.getvalue(), "image/png", True
            resized.convert("RGB").save(buffer, format="JPEG", quality=85)
            return buffer.getvalue(), "image/jpeg", True
    except Exception:  # noqa: BLE001 - a broken image falls through to the size check
        return blob, "", False


def load_image(
    source: str,
    config: VisionConfig,
    *,
    project_root: Path | None = None,
    from_tool: bool = False,
    artifacts_dir: Path | None = None,
) -> ImageAttachment:
    """Turn ``source`` into a provider-ready attachment or raise :class:`ToolError`.

    ``from_tool`` marks calls initiated by the model rather than the user;
    remote URLs are then subject to ``allow_remote_urls_in_tools`` (an agent
    that can embed arbitrary URLs into requests is a data-exfiltration channel).
    """

    text = (source or "").strip()
    if not text:
        raise ToolError("No image source given.")

    if text.startswith("data:image/"):
        if "data-url" not in config.allowed_sources:
            raise ToolError("Data-URL images are disabled by [vision] allowed_sources.")
        approx = int(len(text) * 0.75)
        if approx > config.max_image_bytes:
            raise ToolError(f"Image is ~{approx // 1024} KiB; the [vision] limit is {config.max_image_bytes // 1024} KiB.")
        mime = text[5:].partition(";")[0]
        return ImageAttachment(url=text, source="<data-url>", kind="data-url", mime=mime, bytes_size=approx)

    if text.startswith(("http://", "https://")):
        if "url" not in config.allowed_sources:
            raise ToolError("Remote image URLs are disabled by [vision] allowed_sources.")
        if from_tool and not config.allow_remote_urls_in_tools:
            raise ToolError(
                "Remote image URLs are not allowed from tools.",
                hint="Set [vision] allow_remote_urls_in_tools = true to permit it, or download the file first.",
            )
        return ImageAttachment(url=text, source=text, kind="url", mime="", bytes_size=0)

    if "file" not in config.allowed_sources:
        raise ToolError("File images are disabled by [vision] allowed_sources.")
    path = Path(text).expanduser()
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    if not path.is_file():
        raise ToolError(f"Image not found: {path}")
    blob = path.read_bytes()
    sniffed = sniff_format(blob)
    if sniffed is None:
        raise ToolError(f"{path.name} is not a supported image (png, jpeg, gif, webp).")
    fmt, mime = sniffed
    downscaled = False
    if len(blob) > config.max_image_bytes:
        blob2, mime2, changed = _try_downscale(blob, target_px=config.downscale_to_px)
        if changed and len(blob2) <= config.max_image_bytes:
            blob, mime, downscaled = blob2, mime2, True
        else:
            raise ToolError(
                f"{path.name} is {len(blob) // 1024} KiB; the [vision] limit is {config.max_image_bytes // 1024} KiB.",
                hint="Install Pillow for automatic downscaling, or resize the image.",
            )
    dims = image_dimensions(blob)
    artifact = ""
    if config.keep_copies_in_artifacts and artifacts_dir is not None:
        try:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            copy = artifacts_dir / f"vision-{path.stem[:40]}.{fmt}"
            copy.write_bytes(blob)
            artifact = str(copy)
        except OSError:  # pragma: no cover - artifact copies are best-effort
            artifact = ""
    return ImageAttachment(
        url=data_url_for(blob, mime),
        source=str(path),
        kind="file",
        mime=mime,
        bytes_size=len(blob),
        width=dims[0] if dims else None,
        height=dims[1] if dims else None,
        downscaled=downscaled,
        artifact=artifact,
    )
