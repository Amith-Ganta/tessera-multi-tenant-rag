"""Async wrappers for CPU-bound PBKDF2 password operations.

The project uses hashlib.pbkdf2_hmac (200 000 iterations) for hashing and
verification. These are CPU-bound and block the event loop when called directly
from an async handler. This module offloads them to a dedicated thread pool so
concurrent requests are not serialised behind a single hash operation.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from src.auth.auth import hash_password as _hash_sync, verify_password as _verify_sync

# Bounded pool: 4 threads match the 4 uvicorn workers so each worker can
# service one concurrent hash without starving others.
_pbkdf2_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pbkdf2")


async def hash_password_async(password: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pbkdf2_pool, _hash_sync, password)


async def verify_password_async(password: str, hashed: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pbkdf2_pool, _verify_sync, password, hashed)
