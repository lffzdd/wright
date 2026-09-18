from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from ..attachments import AttachmentError, AttachmentStore, DraftAttachments
from ..session_host import _dispatch_attachment_command


def _png_bytes(*, size: tuple[int, int] = (24, 12)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "navy").save(output, format="PNG")
    return output.getvalue()


def test_store_validates_deduplicates_and_creates_private_thumbnail(tmp_path):
    records = {}
    store = AttachmentStore(tmp_path / "attachments", "session-a")

    first = store.register_bytes("diagram.png", _png_bytes(), records)
    records[first.id] = first
    duplicate = store.register_bytes("renamed.png", _png_bytes(), records)

    assert duplicate == first
    assert store.read_bytes(first) == _png_bytes()
    assert store.path_for(first).stat().st_mode & 0o777 == 0o600
    assert store.thumbnail_path_for(first).stat().st_mode & 0o777 == 0o600
    assert len(list(store.session_root.glob("att_*"))) == 2
    assert not list(store.session_root.glob(".upload-*"))


def test_store_rejects_invalid_images_and_cross_session_records(tmp_path):
    store = AttachmentStore(tmp_path / "attachments", "session-a")
    with pytest.raises(AttachmentError, match="valid image"):
        store.register_bytes("not-an-image.svg", b"<svg></svg>", {})

    record = store.register_bytes("diagram.png", _png_bytes(), {})
    other_session = AttachmentStore(tmp_path / "attachments", "session-b")
    with pytest.raises(AttachmentError, match="another session"):
        other_session.read_bytes(record)


def test_draft_detach_removes_unsubmitted_attachment(tmp_path):
    records = {}
    store = AttachmentStore(tmp_path / "attachments", "session-a")
    drafts = DraftAttachments(store, records)
    source = tmp_path / "space name.png"
    source.write_bytes(_png_bytes())

    added = drafts.attach_paths([str(source)])
    assert drafts.summaries() == added
    removed = drafts.detach("1")

    assert removed == added
    assert records == {}
    assert not store.session_root.exists()


def test_terminal_attachment_commands_accept_shell_escaped_paths(tmp_path):
    class Renderer:
        def __init__(self) -> None:
            self.notices: list[str] = []

        def on_system_notice(self, text: str) -> None:
            self.notices.append(text)

    records = {}
    store = AttachmentStore(tmp_path / "attachments", "session-a")
    drafts = DraftAttachments(store, records)
    source = tmp_path / "Finder image.png"
    source.write_bytes(_png_bytes())
    renderer = Renderer()
    runtime = SimpleNamespace(draft_attachments=drafts, renderer=renderer)

    escaped_source = str(source).replace(" ", "\\ ")
    handled, prompt = _dispatch_attachment_command(f"/attach {escaped_source}", runtime)
    assert handled and prompt is None
    assert "Finder image.png" in renderer.notices[-1]
    handled, prompt = _dispatch_attachment_command("/send describe it", runtime)
    assert not handled and prompt == "describe it"
    assert len(drafts.consume()) == 1
