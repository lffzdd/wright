"""Session-owned image attachment metadata. Bytes stay in infrastructure."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

ALLOWED_ATTACHMENT_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class AttachmentError(ValueError):
    """A submitted attachment is unsafe, unsupported, or unavailable."""


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    filename: str
    media_type: str
    size: int
    sha256: str
    width: int
    height: int
    storage_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> AttachmentRecord:
        try:
            record = cls(
                id=str(value["id"]),
                filename=str(value["filename"]),
                media_type=str(value["media_type"]),
                size=int(value["size"]),
                sha256=str(value["sha256"]),
                width=int(value["width"]),
                height=int(value["height"]),
                storage_path=str(value["storage_path"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AttachmentError("attachment metadata is invalid") from exc
        if (
            not record.id
            or record.media_type not in ALLOWED_ATTACHMENT_MEDIA_TYPES
            or record.size < 1
            or record.width < 1
            or record.height < 1
            or Path(record.storage_path).is_absolute()
            or ".." in Path(record.storage_path).parts
        ):
            raise AttachmentError("attachment metadata is invalid")
        return record
