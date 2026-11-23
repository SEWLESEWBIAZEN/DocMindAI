"""Argon2id password hashing (argon2-cffi), run off the event loop."""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher as _Argon2
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class Argon2PasswordHasher:
    def __init__(self, *, time_cost: int, memory_kib: int, parallelism: int) -> None:
        self._hasher = _Argon2(
            time_cost=time_cost,
            memory_cost=memory_kib,
            parallelism=parallelism,
            type=Type.ID,
        )
        # Used to equalise timing when the account does not exist.
        self._dummy_hash = self._hasher.hash("documind-dummy-password")

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        return await asyncio.to_thread(self._verify_sync, password_hash, password)

    async def verify_dummy(self, password: str) -> None:
        await asyncio.to_thread(self._verify_sync, self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    def _verify_sync(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
