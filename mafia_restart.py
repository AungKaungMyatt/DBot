"""A small durable journal for closing interrupted sessions, never resuming roles."""

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import discord

logger = logging.getLogger(__name__)
RESTART_NOTICE = (
    "**Previous Mafia session closed**\n"
    "The bot restarted. The previous lobby or game has ended. "
    "Discard its role DMs. Use `!mafia create` to start fresh."
)


class RestartJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions "
                "(id TEXT PRIMARY KEY, channel_id INTEGER NOT NULL, host_id INTEGER NOT NULL)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    def records(self):
        with self.connect() as db:
            return db.execute(
                "SELECT id, channel_id, host_id FROM sessions ORDER BY rowid"
            ).fetchall()

    def add(self, channel_id, host_id):
        session = uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (session, channel_id, host_id))
        return session

    def update_host(self, session, host_id):
        with self.connect() as db:
            db.execute("UPDATE sessions SET host_id = ? WHERE id = ?", (host_id, session))

    def remove(self, session):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (session,))


async def notify_interrupted(bot, journal, records, request_seconds=5):
    """Retain undelivered notices; never delete a new session in the same channel."""
    for session, channel_id, host_id in records:
        delivered = False
        try:
            channel = await asyncio.wait_for(bot.fetch_channel(channel_id), request_seconds)
            await asyncio.wait_for(channel.send(RESTART_NOTICE), request_seconds)
            delivered = True
        except (discord.HTTPException, TimeoutError):
            try:
                host = await asyncio.wait_for(bot.fetch_user(host_id), request_seconds)
                await asyncio.wait_for(host.send(RESTART_NOTICE), request_seconds)
                delivered = True
            except (discord.HTTPException, TimeoutError):
                logger.warning("Restart notice retained for inaccessible channel %s.", channel_id)
        if delivered:
            journal.remove(session)
