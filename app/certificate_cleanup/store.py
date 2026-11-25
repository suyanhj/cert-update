"""证书清理候选与审计状态存储。"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from app.utils.store import StoreBase


_SENSITIVE_KEYS = {
    "private_key",
    "key_pem",
    "cert_pem",
    "certificate_pem",
    "certificate_private_key",
    "access_key_secret",
    "secret_key",
    "api_token",
    "password",
}

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(access_key_secret|secret_key|api_token|password|private_key|key_pem|cert_pem)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_PEM_BLOCK = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
    re.DOTALL,
)
_SDK_RESPONSE_SUFFIX = re.compile(r"(?is)(?:^|\s)Response:\s*[\[{].*$")
_ENCODED_DIAGNOSTIC = re.compile(
    r"(?is)(['\"]?EncodedDiagnosticMessage['\"]?\s*[:=]\s*)(['\"])(.*?)\2"
)
_MAX_SANITIZED_TEXT_LENGTH = 2000


def sanitize_cleanup_text(value: str) -> str:
    """移除异常文本中的敏感字段、完整 SDK 响应和超长诊断串。"""
    text = _PEM_BLOCK.sub("[PEM_REDACTED]", str(value))
    text = _SDK_RESPONSE_SUFFIX.sub("", text)
    text = _ENCODED_DIAGNOSTIC.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}",
        text,
    )
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        text,
    ).strip()
    if len(text) > _MAX_SANITIZED_TEXT_LENGTH:
        return f"{text[:_MAX_SANITIZED_TEXT_LENGTH - 3]}..."
    return text


def sanitize_cleanup_data(value: Any) -> Any:
    """递归移除不允许写入状态、日志和通知的敏感字段。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.strip().lower() in _SENSITIVE_KEYS:
                continue
            result[key] = sanitize_cleanup_data(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_cleanup_data(item) for item in value]
    if isinstance(value, str):
        return sanitize_cleanup_text(value)
    return value


class CertificateCleanupStore(StoreBase):
    """持久化候选、上次运行时间和有限运行摘要。"""

    def __init__(
        self,
        path: str = "data/certificate_cleanup_state.json",
        max_run_history: int = 100,
    ) -> None:
        self.max_run_history = max_run_history
        self.state = self._empty_state()
        super().__init__(path)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": 1,
            "last_checked_at": None,
            "candidates": {},
            "runs": [],
            "notified_signature": "",
            "pending_notification": None,
        }

    def _load_from_raw(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            raise ValueError("证书清理状态必须是对象")
        state = self._empty_state()
        state.update(raw)
        if not isinstance(state.get("candidates"), dict):
            raise ValueError("证书清理 candidates 必须是对象")
        if not isinstance(state.get("runs"), list):
            raise ValueError("证书清理 runs 必须是数组")
        self.state = sanitize_cleanup_data(state)

    def _dump_to_raw(self) -> Any:
        return sanitize_cleanup_data(self.state)

    def save(self) -> None:
        self.state = sanitize_cleanup_data(self.state)
        self._persist()

    def get_candidates(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self.state["candidates"])

    def set_candidates(self, candidates: dict[str, dict[str, Any]]) -> None:
        self.state["candidates"] = sanitize_cleanup_data(candidates)

    def append_run(self, summary: dict[str, Any]) -> None:
        runs = [*self.state.get("runs", []), sanitize_cleanup_data(summary)]
        self.state["runs"] = runs[-self.max_run_history :]
