# app/state.py
from typing import List, Dict
import asyncio

# ======================
# 全局状态（唯一数据源）
# ======================

CERT_LIST: List[Dict] = []

DOMAIN_GROUPS: List[Dict] = []

ECS_LIST: List[Dict] = []

LAST_UPDATED: str | None = None

DOMAIN_READY = asyncio.Event()
