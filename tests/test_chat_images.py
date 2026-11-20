"""Chat image attachments: sniffing, storage, jail, and prompt encoding."""

from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path

import pytest

from app.chat_images import (
    ImageAttachmentError,
    attach_prompt_images,
    delete_conversation_images,
    file_for,
    read_image,
    save_images,
    sniff_mime,
    validate_image,
    with_urls,
)
from app.config import load_settings

# A 1x1 transparent PNG. Only its magic bytes matter to the validation layer.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


@pytest.fixture()
def settings(tmp_path: Path):
    return replace(
        load_settings(),
        project_root=tmp_path,
        browser_profile_dir=tmp_path / "browser" / "profile",
    )


def test_sniff_mime_accepts_the_supported_formats_and_rejects_others() -> None:
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(JPEG) == "image/jpeg"
    assert sniff_mime(GIF) == "image/gif"
    assert sniff_mime(WEBP) == "image/webp"

    # SVG is a document format that can carry script, so it is not an image here.
    assert sniff_mime(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert sniff_mime(b"plain text pretending to be a photo") is None
    assert sniff_mime(b"") is None


def test_validate_image_decodes_data_urls_and_enforces_the_limits() -> None:
    payload, mime = validate_image(b64(PNG), max_bytes=1024)
    assert payload == PNG
    assert mime == "image/png"

    data_url = f"data:image/png;base64,{b64(PNG)}"
    payload, mime = validate_image(data_url, max_bytes=1024)
    assert payload == PNG
    assert mime == "image/png"

    with pytest.raises(ImageAttachmentError, match="could not be read"):
        validate_image("not base64 !!", max_bytes=1024)

    with pytest.raises(ImageAttachmentError, match="under 1 MB"):
        validate_image(b64(PNG), max_bytes=1)

    with pytest.raises(ImageAttachmentError, match="PNG, JPEG, WebP, or GIF"):
        validate_image(b64(b"just text"), max_bytes=1024)

    with pytest.raises(ImageAttachmentError, match="empty"):
        validate_image("", max_bytes=1024)


def test_save_and_read_images_stay_inside_the_conversation_folder(settings) -> None:
    saved = save_images(
        settings,
        conversation_id=7,
        items=[{"name": "../../notes.png", "mime": "image/jpeg", "data": b64(PNG)}],
        max_count=4,
    )

    assert len(saved) == 1
    ref = saved[0]
    # The stored name is generated, never the client's, and the sniffed mime wins.
    assert ref["mime"] == "image/png"
    assert ref["name"].endswith("notes.png")
    assert ref["size"] == len(PNG)

    folder = settings.chat_images_dir / "7"
    assert (folder / ref["file"]).is_file()

    payload, mime = read_image(settings, 7, ref["file"])
    assert payload == PNG
    assert mime == "image/png"

    # A missing conversation folder is just "not available", not a crash.
    with pytest.raises(ImageAttachmentError, match="not valid|no longer available"):
        file_for(settings, 7, "../7/" + ref["file"])

    with pytest.raises(ImageAttachmentError, match="no longer available"):
        file_for(settings, 8, ref["file"])


def test_save_images_rejects_too_many_before_writing_anything(settings) -> None:
    with pytest.raises(ImageAttachmentError, match="at most 2 images"):
        save_images(
            settings,
            conversation_id=1,
            items=[
                {"name": "a.png", "data": b64(PNG)},
                {"name": "b.png", "data": b64(PNG)},
                {"name": "c.png", "data": b64(PNG)},
            ],
            max_count=2,
        )

    assert not (settings.chat_images_dir / "1").exists()


def test_with_urls_adds_same_origin_links_and_skips_bad_references() -> None:
    rendered = with_urls(
        3,
        [
            {"file": "a" * 32 + ".png", "name": "a.png", "mime": "image/png"},
            {"name": "missing.png"},
            "not a dict",
        ],
    )

    assert len(rendered) == 1
    assert rendered[0]["url"] == f"/api/chat/images/3/{'a' * 32}.png"


def test_attach_prompt_images_is_bounded_and_user_only(settings) -> None:
    saved = save_images(
        settings,
        conversation_id=4,
        items=[{"name": "one.png", "data": b64(PNG)}],
        max_count=4,
    )
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "assistant", "content": "earlier answer", "images": saved},
        {"role": "user", "content": "old question", "images": saved},
        {"role": "user", "content": "what is in this image?", "images": saved},
    ]

    attached = attach_prompt_images(settings, 4, messages, limit=1)
    assert attached == 1

    # The newest user message wins the single slot; both the assistant entry and
    # the older question lose their references.
    assert "images" not in messages[0]
    assert "images" not in messages[1]
    assert "images" not in messages[2]
    assert [base64.b64decode(item) for item in messages[3]["images"]] == [PNG]

    # No budget left for another turn.
    assert attach_prompt_images(settings, 4, messages, limit=0) == 0


def test_attach_prompt_images_skips_missing_files_on_disk(settings) -> None:
    messages = [
        {
            "role": "user",
            "content": "look",
            "images": [{"file": "b" * 32 + ".png", "name": "gone.png"}],
        }
    ]
    assert attach_prompt_images(settings, 9, messages) == 0
    assert "images" not in messages[0]


def test_delete_conversation_images_only_touches_that_conversation(settings) -> None:
    keep = save_images(
        settings, conversation_id=1, items=[{"name": "a.png", "data": b64(PNG)}], max_count=2
    )
    save_images(
        settings, conversation_id=2, items=[{"name": "b.png", "data": b64(PNG)}], max_count=2
    )

    removed = delete_conversation_images(settings, 2)
    assert removed == 1
    assert not (settings.chat_images_dir / "2").exists()
    assert (settings.chat_images_dir / "1" / keep[0]["file"]).is_file()

    # Deleting again is a no-op rather than an error.
    assert delete_conversation_images(settings, 2) == 0
