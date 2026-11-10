from __future__ import annotations

from tests.runtime_helpers import tool_runtime_for_session
from tests.test_attachments import _png_bytes
from wright.application.execution.services import RuntimeServices
from wright.domain.model.session import Session
from wright.infrastructure.storage.artifacts import ArtifactStore
from wright.infrastructure.tools.file import read_file


def _pdf(pages: list[str]) -> bytes:
    """Smallest valid PDF with one text line per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    font = 3 + 2 * len(pages)
    for index, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Contents {4 + 2 * index} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def _runtime(tmp_path, *, artifacts=True):
    services = RuntimeServices(artifact_store=ArtifactStore(tmp_path / "artifacts")) if artifacts else None
    return tool_runtime_for_session(
        Session.create("media", tmp_path), services=services, workspace_dir=tmp_path,
        tool_call_id="call_img",
    )


def test_read_registers_an_image_for_the_model(tmp_path):
    (tmp_path / "shot.png").write_bytes(_png_bytes(size=(24, 12)))

    result = read_file("shot.png", runtime=_runtime(tmp_path))

    assert result.ok
    assert result.data["media_type"] == "image/png"
    assert (result.data["width"], result.data["height"]) == (24, 12)
    (ref,) = result.artifacts
    # The provider adapter only shows images that belong to this call.
    assert ref.call_id == "call_img"
    assert ref.media_type == "image/png"


def test_read_image_without_artifact_storage_fails_cleanly(tmp_path):
    (tmp_path / "shot.png").write_bytes(_png_bytes())

    result = read_file("shot.png", runtime=_runtime(tmp_path, artifacts=False))

    assert not result.ok
    assert "not available" in result.err


def test_corrupt_image_is_rejected(tmp_path):
    (tmp_path / "bad.png").write_bytes(_png_bytes()[:-12])

    assert not read_file("bad.png", runtime=_runtime(tmp_path)).ok


def test_read_extracts_pdf_text_by_page(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(_pdf(["Alpha page", "Beta page", "Gamma page"]))
    runtime = _runtime(tmp_path)

    whole = read_file("doc.pdf", runtime=runtime)
    second = read_file("doc.pdf", pages="2", runtime=runtime)

    assert whole.ok and whole.data["page_count"] == 3
    assert "--- page 1 ---\nAlpha page" in whole.data["content"]
    assert "Gamma page" in whole.data["content"]
    assert second.data["content"] == "--- page 2 ---\nBeta page"
    assert second.data["next_pages"] == "3-3"
    assert not read_file("doc.pdf", pages="3-1", runtime=runtime).ok


def test_text_files_are_still_read_as_text(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    result = read_file("a.txt", runtime=_runtime(tmp_path))

    assert result.ok and result.data["content"].endswith("hello\n")
    assert not result.artifacts
