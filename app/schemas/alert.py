from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from app.utils import time


class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"





@dataclass
class AlertEvent:
    level: AlertLevel
    title: str
    message: str
    source: str  # 来源：cert / domain / record / deploy
    domain: Optional[str] = None
    provider: Optional[str] = None
    expires_at: Optional[datetime | str] = None
    days_remaining: Optional[int] = None
    timestamp: Optional[datetime | str] = None

    def __post_init__(self) -> None:
        if self.timestamp is None:
            util = time.TimeUtil()
            self.timestamp = util.to_local_tz(util.now())