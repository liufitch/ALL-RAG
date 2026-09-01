from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO


@dataclass(frozen=True)
class PreparedUpload:
    filename: str
    extension: str
    content_type: str
    size: int
    sha256: str
    stream: BinaryIO


class UploadValidationError(ValueError):
    """A rejected upload with a stable, client-safe reason code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
