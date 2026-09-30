from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class CertParsedInfo:
    """证书解析后的通用结构。"""

    issuer: str
    subject: str
    key_length: str
    sans: list[str]
    not_before_utc: datetime
    not_after_utc: datetime
    profile: Optional[str]

