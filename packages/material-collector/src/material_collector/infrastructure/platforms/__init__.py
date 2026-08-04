"""Domestic platform adapters."""

from material_collector.infrastructure.platforms.bilibili import BilibiliAdapter
from material_collector.infrastructure.platforms.douyin import DouyinAdapter
from material_collector.infrastructure.platforms.errors import PlatformAdapterError
from material_collector.infrastructure.platforms.transport import (
    PlatformTransport,
    PlaywrightPlatformTransport,
)
from material_collector.infrastructure.platforms.xiaohongshu import XiaohongshuAdapter

__all__ = [
    "BilibiliAdapter",
    "DouyinAdapter",
    "PlatformAdapterError",
    "PlatformTransport",
    "PlaywrightPlatformTransport",
    "XiaohongshuAdapter",
]
