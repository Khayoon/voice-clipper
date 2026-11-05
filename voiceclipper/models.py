"""Portable project files and non-destructive clip selections.

The original media is never changed. Times refer to the source media timeline,
in milliseconds, regardless of playback rate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid


STATUSES = frozenset({"pending", "accepted", "rejected"})


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(
            f"{name} must be an integer greater than or equal to {minimum}."
        )
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    return value


@dataclass
class Clip:
    source: str
    start_ms: int
    end_ms: int
    audio_track: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    speaker: str = ""
    language: str = ""
    transcript: str = ""
    translation: str = ""
    status: str = "pending"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    processing: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not _text(self.source, "source").strip():
            raise ValueError("A clip must refer to a source file.")
        _integer(self.start_ms, "start_ms")
        _integer(self.end_ms, "end_ms", 1)
        if self.end_ms <= self.start_ms:
            raise ValueError("The clip end must be later than its start.")
        _integer(self.audio_track, "audio_track")
        if not isinstance(self.id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", self.id
        ):
            raise ValueError(
                "Clip IDs may only contain letters, numbers, hyphens and underscores."
            )
        for name in (
            "name",
            "speaker",
            "language",
            "transcript",
            "translation",
            "created_at",
        ):
            _text(getattr(self, name), name)
        if not isinstance(self.status, str) or self.status not in STATUSES:
            raise ValueError("Clip status must be pending, accepted, or rejected.")
        if not isinstance(self.processing, dict):
            raise ValueError("Clip processing information must be an object.")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def display_name(self) -> str:
        return self.name.strip() or f"Clip {self.id[:8]}"

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Clip":
        if not isinstance(data, dict):
            raise ValueError("Each clip must be an object.")
        allowed = {item.name for item in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"Unsupported clip fields: {', '.join(sorted(unknown))}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValueError(f"Invalid clip: {exc}") from exc


def _relative_path(source: str, directory: Path) -> str:
    if not source:
        return ""
    path = Path(source).expanduser().resolve()
    try:
        return os.path.relpath(path, directory)
    except ValueError:  # Windows paths on different drives cannot be relative.
        return str(path)


def _absolute_path(source: str, directory: Path) -> str:
    if not source:
        return ""
    path = Path(source).expanduser()
    return (
        str((directory / path).resolve())
        if not path.is_absolute()
        else str(path.resolve())
    )


def atomic_json(path: str | Path, data: Any) -> None:
    """Replace JSON only after the complete file is safely written."""
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@dataclass
class Project:
    media_path: str = ""
    clips: list[Clip] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def validate(self) -> None:
        _text(self.media_path, "media_path")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError(
                "This project version is not supported. Expected schema_version 1."
            )
        if not isinstance(self.settings, dict):
            raise ValueError("Project settings must be an object.")
        if not isinstance(self.clips, list) or not all(
            isinstance(clip, Clip) for clip in self.clips
        ):
            raise ValueError("Project clips must be a list of Clip objects.")
        seen: set[str] = set()
        for clip in self.clips:
            clip.validate()
            if clip.id in seen:
                raise ValueError(f"Duplicate clip ID: {clip.id}")
            seen.add(clip.id)

    def add_clip(
        self,
        source: str,
        start_ms: int,
        end_ms: int,
        audio_track: int = 0,
        **fields_: Any,
    ) -> Clip:
        clip = Clip(
            source=str(source),
            start_ms=start_ms,
            end_ms=end_ms,
            audio_track=audio_track,
            **fields_,
        )
        if any(item.id == clip.id for item in self.clips):
            raise ValueError(f"Duplicate clip ID: {clip.id}")
        self.clips.append(clip)
        return clip

    def save(self, path: str | Path) -> None:
        self.validate()
        destination = Path(path).expanduser().resolve()
        data = {
            "schema_version": self.schema_version,
            "media_path": _relative_path(self.media_path, destination.parent),
            "settings": self.settings,
            "clips": [],
        }
        for clip in self.clips:
            entry = clip.to_dict()
            entry["source"] = _relative_path(clip.source, destination.parent)
            data["clips"].append(entry)
        sources = {Path(clip.source).expanduser().resolve() for clip in self.clips}
        if self.media_path:
            sources.add(Path(self.media_path).expanduser().resolve())
        if destination in sources:
            raise ValueError("A project file cannot replace its source media.")
        atomic_json(destination, data)

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        source = Path(path).expanduser().resolve()
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError(
                f"The project file is not valid UTF-8 JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError("A project file must contain a JSON object.")
        unknown = set(data) - {"schema_version", "media_path", "settings", "clips"}
        if unknown:
            raise ValueError(
                f"Unsupported project fields: {', '.join(sorted(unknown))}"
            )
        version = data.get("schema_version")
        if type(version) is not int or version != 1:
            raise ValueError(
                "This project version is not supported. Expected schema_version 1."
            )
        raw_clips = data.get("clips", [])
        if not isinstance(raw_clips, list):
            raise ValueError("Project clips must be a list.")
        media_path = _text(data.get("media_path", ""), "media_path")
        clips = []
        for raw in raw_clips:
            clip = Clip.from_dict(raw)
            clip.source = _absolute_path(clip.source, source.parent)
            clips.append(clip)
        project = cls(
            media_path=_absolute_path(media_path, source.parent),
            clips=clips,
            settings=data.get("settings", {}),
            schema_version=version,
        )
        project.validate()
        return project
