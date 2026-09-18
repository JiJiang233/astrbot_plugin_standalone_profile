"""三级锁：用户流程锁、Profile 锁、群锁。

设计文档第 11.2 节：多个锁同时需要时，固定顺序为"用户 → Profile → 群"（后台任务
没有用户锁时按"Profile → 群"），避免死锁。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyedLocks:
    """按 key 惰性创建的 asyncio 锁集合。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


class LockManager:
    """持有三类锁，并提供按时序固定顺序获取的组合入口。"""

    def __init__(self) -> None:
        self.user = KeyedLocks()
        self.profile = KeyedLocks()
        self.group = KeyedLocks()

    @asynccontextmanager
    async def user_scope(self, user_key: str) -> AsyncIterator[None]:
        async with await self.user.get(user_key):
            yield

    @asynccontextmanager
    async def profile_scope(self, profile_id: str) -> AsyncIterator[None]:
        async with await self.profile.get(profile_id):
            yield

    @asynccontextmanager
    async def group_scope(self, group_umo: str) -> AsyncIterator[None]:
        async with await self.group.get(group_umo):
            yield

    @asynccontextmanager
    async def ordered(
        self,
        *,
        user_key: str | None = None,
        profile_id: str | None = None,
        group_umo: str | None = None,
    ) -> AsyncIterator[None]:
        """按 用户 → Profile → 群 的固定顺序获取锁。

        后台任务不传 `user_key`，顺序退化为 Profile → 群，方向一致，不会死锁。
        """
        locks: list[asyncio.Lock] = []
        if user_key:
            locks.append(await self.user.get(user_key))
        if profile_id:
            locks.append(await self.profile.get(profile_id))
        if group_umo:
            locks.append(await self.group.get(group_umo))

        if not locks:
            yield
            return

        acquired = 0
        try:
            for lock in locks:
                await lock.acquire()
                acquired += 1
            yield
        finally:
            for lock in reversed(locks[:acquired]):
                lock.release()
