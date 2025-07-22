"""
Discord “Game & Activity” tracker – slash‑only, async DB, multi‑activity aware.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List

import aiosqlite
import discord
from discord.ext import commands
from dotenv import load_dotenv

# ── logging setup ----------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("gamestat")

# ── config & DB setup ------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DB_PATH = pathlib.Path("data/activity.db")
DB_PATH.parent.mkdir(exist_ok=True)
if not DB_PATH.exists():
    with sqlite3.connect(DB_PATH) as _db:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS activity(
              user_id INTEGER,
              game    TEXT,
              started TEXT,
              ended   TEXT,
              seconds INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_user ON activity(user_id);
            """
        )
log.info("DB initialized at %s", DB_PATH)

# ── bot & intents ----------------------------------------------------------
intents = discord.Intents.default()
intents.presences = intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

launch_time = datetime.now(timezone.utc)
db_lock = asyncio.Lock()
active_sessions: Dict[int, Dict[str, datetime]] = {}

# ── helper functions -------------------------------------------------------
def activity_key(a: discord.Activity) -> str:
    return f"{a.type}-{a.application_id or a.name}"

def interesting(acts: List[discord.Activity]) -> List[discord.Activity]:
    return [a for a in acts if a.type != discord.ActivityType.custom]

def pretty(secs: int) -> str:
    h, m = divmod(secs // 60, 60)
    return f"{h:02d}h {m:02d}m"

# ── presence tracking ------------------------------------------------------
@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    uid = after.id
    before_set = {activity_key(a) for a in interesting(before.activities)}
    after_set = {activity_key(a) for a in interesting(after.activities)}

    started = after_set - before_set
    ended = before_set - after_set

    if not started and not ended:
        return

    log.info("Raw activities  AFTER  kadabengaran → %s", after.activities)
    log.info("Presence diff for %s • started=%s ended=%s", after.display_name, started, ended)

    async with db_lock:
        ts_now = datetime.now(timezone.utc)

        # start sessions
        rows = []
        for k in started:
            name = k.split("-", 1)[1]
            active_sessions.setdefault(uid, {})[k] = ts_now
            rows.append((uid, name, ts_now.isoformat(), None, None))
            log.info("START %s • %s", after.display_name, name)
        if rows:
            await bot.db.executemany("INSERT INTO activity VALUES (?,?,?,?,?)", rows)

        # end sessions
        for k in ended:
            start_ts = active_sessions.get(uid, {}).pop(k, None)
            if start_ts:
                name = k.split("-", 1)[1]
                secs = int((ts_now - start_ts).total_seconds())
                await bot.db.execute(
                    """UPDATE activity
                       SET ended = ?, seconds = ?
                       WHERE user_id = ? AND game = ? AND ended IS NULL""",
                    (ts_now.isoformat(), secs, uid, name),
                )
                log.info("END   %s • %s • %ss", after.display_name, name, secs)

        await bot.db.commit()

# ── slash commands logging wrapper -----------------------------------------
async def log_wrap(inter: discord.Interaction, name: str, coro):
    log.info("/%s invoked by %s", name, inter.user.display_name)
    try:
        await coro
        log.info("/%s finished for %s", name, inter.user.display_name)
    except Exception:
        log.exception("Error in /%s", name)
        raise

@tree.command(name="ping", description="Latency check")
async def ping_slash(inter: discord.Interaction):
    async def _impl():
        await inter.response.send_message(f"Pong! `{round(bot.latency*1000)} ms`", ephemeral=True)
    await log_wrap(inter, "ping", _impl())

@tree.command(name="uptime", description="Bot uptime")
async def uptime_slash(inter: discord.Interaction):
    async def _impl():
        delta = datetime.now(timezone.utc) - launch_time
        await inter.response.send_message(f"Uptime: `{pretty(int(delta.total_seconds()))}`", ephemeral=True)
    await log_wrap(inter, "uptime", _impl())

@tree.command(name="lastgame", description="Most‑recent activity & duration")
async def lastgame_slash(inter: discord.Interaction, member: discord.Member | None = None):
    async def _impl():
        await inter.response.defer(ephemeral=True)
        target = member or inter.user

        # active session query
        async with bot.db.execute(
            """SELECT game, started, ended, seconds
                 FROM activity
                WHERE user_id = ? AND ended IS NULL
             ORDER BY started DESC LIMIT 1""",
            (target.id,),
        ) as cur:
            row = await cur.fetchone()

        # fallback to most recent finished
        if row is None:
            async with bot.db.execute(
                """SELECT game, started, ended, seconds
                     FROM activity
                    WHERE user_id = ?
                 ORDER BY started DESC LIMIT 1""",
                (target.id,),
            ) as cur:
                row = await cur.fetchone()

        if row is None:
            return await inter.followup.send("No sessions recorded yet.", ephemeral=True)

        game, started_str, ended_str, secs = row
        start_dt = datetime.fromisoformat(started_str)
        if ended_str is None:
            secs = int((datetime.now(timezone.utc) - start_dt).total_seconds())
            suffix = " (still running)"
        else:
            suffix = ""
        await inter.followup.send(
            f"**{target.display_name}** spent `{pretty(secs)}` in **{game}** {suffix}",
            ephemeral=True,
        )
    await log_wrap(inter, "lastgame", _impl())

# ── startup -----------------------------------------------------------------
@bot.event
async def on_ready():
    bot.db = await aiosqlite.connect(DB_PATH)
    await tree.sync()
    log.info("Logged in as %s • latency %.0f ms", bot.user, bot.latency * 1000)

bot.run(TOKEN)
