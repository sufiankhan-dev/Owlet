"""Chat image attachments: validation, local storage, and prompt encoding.

The browser downscales images before upload, but nothing from the client is
trusted: every payload is size-checked, base64-decoded, and sniffed here before
a byte reaches the disk. Files live under ``database/chat_images/<conversation>/``
so they are never mixed with the user's course workspace, and the SQLite row
holds only a reference to them.

This module is the only place that touches the image directory. It raises
``ImageAttachmentError`` (a ``ValueError``) with messages that are safe to show
in the dashboard.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from app.config import Settings
from app.security import sanitize_filename

# Formats Ollama's vision models accept. SVG is deliberately excluded: it is a
# document format that can carry script, not a photo the model needs.
ALLOWED_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_MAX_ORIGINAL_NAME = 120
_STORED_NAME = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")
# How many of the most recent images one prompt may carry, and the byte budget
# for their base64 form. Older images stay visible in the chat history but are
# not re-sent, which keeps CPU inference from ballooning.
PROMPT_IMAGE_LIMIT = 4
PROMPT_IMAGE_BYTES = 12 * 1024 * 1024


class ImageAttachmentError(ValueError):
    """A rejected image attachment with a user-facing message."""


def sniff_mime(data: bytes) -> str | None:
    """Detect a supported image type from its magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode_payload(data: str) -> bytes:
    """Decode a plain base64 string, tolerating an accidental data URL."""
    raw = str(data or "").strip()
    if not raw:
        raise ImageAttachmentError("An attached image was empty. Remove it and try again.")
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageAttachmentError(
            "An attached image could not be read. Try attaching it again."
        ) from exc


def validate_image(
    data: str,
    *,
    max_bytes: int,
) -> tuple[bytes, str]:
    """Return ``(bytes, mime)`` for a well-formed, supported image."""

    payload = _decode_payload(data)
    if not payload:
        raise ImageAttachmentError("An attached image was empty. Remove it and try again.")
    if len(payload) > max_bytes:
        limit_mb = max(1, round(max_bytes / (1024 * 1024)))
        raise ImageAttachmentError(
            f"Each image must be under {limit_mb} MB. Remove the large one and try again."
        )
    mime = sniff_mime(payload)
    if mime is None:
        raise ImageAttachmentError(
            "Attached files must be PNG, JPEG, WebP, or GIF images."
        )
    # The bytes decide the type; a mislabelled upload still renders correctly
    # because the stored metadata carries the sniffed value.
    return payload, mime


def _conversation_dir(settings: Settings, conversation_id: int) -> Path:
    folder = settings.chat_images_dir / str(int(conversation_id))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def conversation_dir(settings: Settings, conversation_id: int) -> Path:
    return settings.chat_images_dir / str(int(conversation_id))


def save_images(
    settings: Settings,
    conversation_id: int,
    items: Iterable[dict[str, Any]],
    *,
    max_count: int,
) -> list[dict[str, Any]]:
    """Validate and persist uploads, returning the metadata stored in SQLite."""

    uploads = list(items)
    if len(uploads) > max_count:
        raise ImageAttachmentError(
            f"Attach at most {max_count} image{'s' if max_count != 1 else ''} per message."
        )

    validated: list[tuple[bytes, str, str]] = []
    for item in uploads:
        payload, mime = validate_image(
            str(item.get("data") or ""),
            max_bytes=settings.max_image_bytes,
        )
        validated.append(
            (payload, mime, sanitize_filename(str(item.get("name") or "image"), fallback="image"))
        )

    folder = _conversation_dir(settings, conversation_id)
    saved: list[dict[str, Any]] = []
    for payload, mime, original_name in validated:
        stored_name = f"{uuid.uuid4().hex}{ALLOWED_MIME[mime]}"
        target = folder / stored_name
        try:
            target.write_bytes(payload)
        except OSError as exc:
            raise ImageAttachmentError(
                "Owlet could not save an attached image on disk. Check free space."
            ) from exc
        saved.append(
            {
                "file": stored_name,
                "name": original_name[:_MAX_ORIGINAL_NAME],
                "mime": mime,
                "size": len(payload),
            }
        )
    return saved


def image_url(conversation_id: int, file_name: str) -> str:
    return f"/api/chat/images/{int(conversation_id)}/{file_name}"


def file_for(settings: Settings, conversation_id: int, file_name: str) -> Path:
    """Resolve a stored image name, refusing anything outside its folder."""

    if not _STORED_NAME.match(str(file_name or "")):
        raise ImageAttachmentError("That image reference is not valid.")
    folder = conversation_dir(settings, conversation_id)
    target = (folder / file_name).resolve()
    base = folder.resolve()
    if target != base and not target.is_relative_to(base):
        raise ImageAttachmentError("That image reference is not valid.")
    if not target.is_file():
        raise ImageAttachmentError("That image is no longer available.")
    return target


def read_image(settings: Settings, conversation_id: int, file_name: str) -> tuple[bytes, str]:
    target = file_for(settings, conversation_id, file_name)
    payload = target.read_bytes()
    mime = sniff_mime(payload[:16])
    if mime is None:
        raise ImageAttachmentError("That image file is damaged or unsupported.")
    return payload, mime


def with_urls(conversation_id: int, refs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the same-origin URL the dashboard uses to render history."""

    rendered: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("file"):
            continue
        item = dict(ref)
        item["url"] = image_url(conversation_id, str(ref["file"]))
        rendered.append(item)
    return rendered


def attach_prompt_images(
    settings: Settings,
    conversation_id: int,
    messages: list[dict[str, Any]],
    *,
    limit: int = PROMPT_IMAGE_LIMIT,
    max_bytes: int = PROMPT_IMAGE_BYTES,
) -> int:
    """Attach base64 images to chat messages, newest first, within a budget.

    Only user messages are considered. Images beyond the count or byte budget
    are dropped from the prompt (the dashboard still shows them) so a long
    conversation cannot quietly exceed the model's context. Returns how many
    images were attached.
    """

    remaining_count = limit
    remaining_bytes = max_bytes
    attached = 0

    for message in reversed(messages):
        refs = message.pop("images", None)
        if message.get("role") != "user":
            continue
        if not isinstance(refs, list) or not refs:
            continue
        if remaining_count <= 0:
            continue

        selected: list[str] = []
        for ref in reversed(refs):
            if remaining_count <= 0 or not isinstance(ref, dict):
                continue
            file_name = str(ref.get("file") or "")
            if not file_name:
                continue
            try:
                payload, _mime = read_image(settings, conversation_id, file_name)
            except (ImageAttachmentError, OSError):
                continue
            if len(payload) > remaining_bytes:
                continue
            remaining_count -= 1
            remaining_bytes -= len(payload)
            selected.append(base64.b64encode(payload).decode("ascii"))
            attached += 1

        if selected:
            message["images"] = list(reversed(selected))

    return attached


def delete_conversation_images(settings: Settings, conversation_id: int) -> int:
    """Remove every stored image for one conversation; returns files deleted."""

    folder = conversation_dir(settings, conversation_id)
    if not folder.is_dir():
        return 0
    removed = 0
    for child in folder.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                child.unlink()
                removed += 1
        except OSError:
            continue
    try:
        folder.rmdir()
    except OSError:
        pass
    return removed
