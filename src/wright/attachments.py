"""Durable, session-scoped image attachments.

The conversation keeps only small attachment references.  Original bytes live
outside the workspace under Wright's state directory, so a resumed session can
rebuild provider requests without putting base64 data in a checkpoint.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_TURN = 10
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class AttachmentError(ValueError):
    """A submitted attachment is unsafe, unsupported, or unavailable."""


def inspect_image(path: Path | BinaryIO) -> tuple[str, str, int, int]:
    """Validate uploads and tool artifacts with the same format/decode limits."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise AttachmentError("image exceeds 40 megapixel limit")
                image.verify()
            with Image.open(path) as image:
                image.load()
                if image.format not in _FORMATS:
                    raise AttachmentError("only PNG, JPEG, and WebP images are supported")
                if getattr(image, "is_animated", False):
                    raise AttachmentError("animated images are not supported")
                media_type, suffix = _FORMATS[image.format]
                return media_type, suffix, image.width, image.height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise AttachmentError("image is unsafe to decode") from exc
    except UnidentifiedImageError as exc:
        raise AttachmentError("file is not a valid image") from exc
    except OSError as exc:
        raise AttachmentError("image cannot be decoded") from exc


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
            or record.media_type not in {item[0] for item in _FORMATS.values()}
            or record.size < 1
            or record.width < 1
            or record.height < 1
            or Path(record.storage_path).is_absolute()
            or ".." in Path(record.storage_path).parts
        ):
            raise AttachmentError("attachment metadata is invalid")
        return record


class AttachmentStore:
    """Own image bytes for one project; records remain owned by a session."""

    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root.resolve()
        self.session_id = session_id
        self.session_root = self.root / session_id

    def register_path(
        self, source: Path, records: dict[str, AttachmentRecord]
    ) -> AttachmentRecord:
        source = source.expanduser().resolve(strict=True)
        if not source.is_file():
            raise AttachmentError(f"attachment is not a file: {source}")
        if source.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("image exceeds 20 MiB limit")
        with source.open("rb") as handle:
            return self._register_stream(source.name, handle, records)

    def register_bytes(
        self,
        filename: str,
        data: bytes,
        records: dict[str, AttachmentRecord],
    ) -> AttachmentRecord:
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("image exceeds 20 MiB limit")
        from io import BytesIO

        return self._register_stream(filename, BytesIO(data), records)

    def _register_stream(
        self,
        filename: str,
        source: BinaryIO,
        records: dict[str, AttachmentRecord],
    ) -> AttachmentRecord:
        self.session_root.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=self.session_root)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ATTACHMENT_BYTES:
                        raise AttachmentError("image exceeds 20 MiB limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            media_type, suffix, width, height = inspect_image(temporary)
            checksum = digest.hexdigest()
            for record in records.values():
                if record.sha256 == checksum and record.size == size:
                    temporary.unlink(missing_ok=True)
                    return record
            attachment_id = f"att_{uuid4().hex}"
            destination = self.session_root / f"{attachment_id}{suffix}"
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            self._write_thumbnail(destination, attachment_id)
            return AttachmentRecord(
                id=attachment_id,
                filename=Path(filename).name or f"image{suffix}",
                media_type=media_type,
                size=size,
                sha256=checksum,
                width=width,
                height=height,
                storage_path=str(destination.relative_to(self.root)),
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _write_thumbnail(self, source: Path, attachment_id: str) -> None:
        """Create a small local preview; the original is never rewritten."""
        target = self.session_root / f"{attachment_id}.thumb.webp"
        try:
            with Image.open(source) as image:
                image.thumbnail((320, 320))
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.save(target, format="WEBP", quality=80, method=4)
            os.chmod(target, 0o600)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise AttachmentError("image thumbnail could not be created") from exc

    def _record_path(self, record: AttachmentRecord) -> Path:
        path = (self.root / record.storage_path).resolve()
        if self.session_root not in path.parents:
            raise AttachmentError("attachment belongs to another session")
        return path

    def read_bytes(self, record: AttachmentRecord) -> bytes:
        path = self._record_path(record)
        if not path.is_file():
            raise AttachmentError("attachment bytes are unavailable")
        data = path.read_bytes()
        if len(data) != record.size or hashlib.sha256(data).hexdigest() != record.sha256:
            raise AttachmentError("attachment bytes failed integrity check")
        return data

    def path_for(self, record: AttachmentRecord) -> Path:
        path = self._record_path(record)
        if not path.is_file():
            raise AttachmentError("attachment bytes are unavailable")
        return path

    def thumbnail_path_for(self, record: AttachmentRecord) -> Path:
        self._record_path(record)
        path = self.session_root / f"{record.id}.thumb.webp"
        if not path.is_file():
            raise AttachmentError("attachment thumbnail is unavailable")
        return path

    def remove(self, record: AttachmentRecord) -> None:
        try:
            path = self._record_path(record)
        except AttachmentError:
            return
        path.unlink(missing_ok=True)
        (self.session_root / f"{record.id}.thumb.webp").unlink(missing_ok=True)
        if self.session_root.is_dir() and not any(self.session_root.iterdir()):
            self.session_root.rmdir()


class DraftAttachments:
    """Host-local attachment chips that are consumed by the next user turn."""

    def __init__(self, store: AttachmentStore, records: dict[str, AttachmentRecord]) -> None:
        self.store = store
        self.records = records
        self.ids: list[str] = []

    def attach_paths(self, paths: list[str]) -> list[AttachmentRecord]:
        if not paths:
            raise AttachmentError("usage: /attach PATH...")
        added: list[AttachmentRecord] = []
        for raw_path in paths:
            if len(self.ids) >= MAX_ATTACHMENTS_PER_TURN:
                raise AttachmentError("a turn may include at most 10 images")
            record = self.store.register_path(Path(raw_path), self.records)
            self.records[record.id] = record
            if record.id not in self.ids:
                self.ids.append(record.id)
                added.append(record)
        return added

    def summaries(self) -> list[AttachmentRecord]:
        return [self.records[item] for item in self.ids if item in self.records]

    def detach(self, value: str) -> list[AttachmentRecord]:
        if value.strip().lower() == "all":
            indexes = list(range(len(self.ids)))
        else:
            try:
                indexes = [int(value) - 1]
            except ValueError as exc:
                raise AttachmentError("usage: /detach INDEX|all") from exc
        removed: list[AttachmentRecord] = []
        for index in sorted(set(indexes), reverse=True):
            if not 0 <= index < len(self.ids):
                raise AttachmentError("attachment index is out of range")
            attachment_id = self.ids.pop(index)
            record = self.records.pop(attachment_id, None)
            if record is not None:
                self.store.remove(record)
                removed.append(record)
        return list(reversed(removed))

    def consume(self) -> list[str]:
        ids = list(self.ids)
        self.ids.clear()
        return ids
