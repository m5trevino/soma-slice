"""Async SQLite connection for the Soma Session Picker."""

import os
from contextlib import asynccontextmanager

import aiosqlite


def _database_path() -> str:
    path = os.environ.get("DATABASE_PATH", "")
    if path:
        return path
    dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(dotenv_path):
        from dotenv import load_dotenv
        load_dotenv(dotenv_path)
        path = os.environ.get("DATABASE_PATH", "")
    return path or "./vault.db"


async def get_connection() -> aiosqlite.Connection:
    db_path = _database_path()
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    return conn


@asynccontextmanager
async def connection_context():
    conn = await get_connection()
    try:
        yield conn
    finally:
        await conn.close()
