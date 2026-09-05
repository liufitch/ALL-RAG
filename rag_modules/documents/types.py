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
    """上传被拒绝时的异常，包含稳定且可安全返回客户端的原因码。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
