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
from collections.abc import Coroutine
from typing import Callable, Awaitable
from discord.ext import commands
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ── logging setup ----------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("gamestat")

# ── config & DB setup ------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "123456789012345678"))

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
    # Safely fetch application_id if it exists, otherwise fallback to name
    app_id = getattr(a, "application_id", None)
    return f"{a.type}-{app_id or a.name}"

def interesting(acts: List[discord.Activity]) -> List[discord.Activity]:
    return [a for a in acts if a.type != discord.ActivityType.custom]

def pretty(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"

# ── presence tracking ------------------------------------------------------
@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    uid = after.id
    before_acts = interesting(before.activities)
    after_acts = interesting(after.activities)

    before_set = {activity_key(a) for a in before_acts}
    after_set = {activity_key(a) for a in after_acts}

    started = after_set - before_set
    ended = before_set - after_set

    if not started and not ended:
        return

    log.info("Raw activities  AFTER  %s → %s", after.display_name, after.activities)
    log.info("Presence diff for %s • started=%s ended=%s", after.display_name, started, ended)

    async with db_lock:
        ts_now = datetime.now(timezone.utc)

        # START sessions (map keys to activities to get proper name)
        rows = []
        for act in after_acts:
            k = activity_key(act)
            if k in started:
                name = act.name
                active_sessions.setdefault(uid, {})[k] = ts_now
                rows.append((uid, name, ts_now.isoformat(), None, None))
                log.info("🟢 START %s → %s", after.display_name, name)

        if rows:
            await bot.db.executemany("INSERT INTO activity VALUES (?,?,?,?,?)", rows)

        # END sessions (map keys to activities to get proper name)
        
        TZ = ZoneInfo("Asia/Singapore")

        for act in before_acts:
            k = activity_key(act)
            if k in ended:
                start_ts = active_sessions.get(uid, {}).pop(k, None)
                if start_ts:
                    name = act.name
                    secs = int((ts_now - start_ts).total_seconds())
                    await bot.db.execute(
                        """UPDATE activity
                           SET ended = ?, seconds = ?
                           WHERE user_id = ? AND game = ? AND ended IS NULL""",
                        (ts_now.isoformat(), secs, uid, name),
                    )
                    log.info("🔴 END %s • %s • %ss", after.display_name, name, secs)
                    # ── new: send announcement ───────────────────────────────
                    try:
                        channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                        if channel:
                            start_str = start_ts.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
                            end_str = ts_now.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
                            await channel.send(
                                f"**{after.display_name}** finished **{name}**.\n"
                                f"▶️ Duration: `{pretty(secs)}`\n"
                                f"🕒 From `{start_str}` to `{end_str}`"
                            )
                    except Exception:
                        log.exception("Failed to send announcement for end event")

        await bot.db.commit()

# ── slash commands logging wrapper -----------------------------------------
from discord.errors import InteractionResponded, NotFound

async def log_wrap(inter: discord.Interaction, name: str, coro_fn: Callable[[], Awaitable[None]]):
    user = inter.user.display_name
    log.info("/%s invoked by %s", name, user)

    try:
        try:
            if not inter.response.is_done():
                await inter.response.defer(ephemeral=True)
        except (InteractionResponded, NotFound):
            log.warning("Interaction already responded or expired for /%s", name)

        await coro_fn()
        log.info("/%s finished for %s", name, user)

    except Exception:
        log.exception("Error in /%s", name)
        try:
            await inter.followup.send(f"⚠️ Error running /{name}", ephemeral=True)
        except Exception:
            pass

@tree.command(name="ping", description="Latency check")
async def ping_slash(inter: discord.Interaction):
    async def _impl():
        await inter.followup.send(f"Pong! `{round(bot.latency * 1000)} ms`", ephemeral=True)

    await log_wrap(inter, "ping", _impl)


@tree.command(name="uptime", description="Bot uptime")
async def uptime_slash(inter: discord.Interaction):
    async def _impl():
        delta = datetime.now(timezone.utc) - launch_time
        await inter.followup.send(f"Uptime: `{pretty(int(delta.total_seconds()))}`", ephemeral=True)

    await log_wrap(inter, "uptime", _impl)


@tree.command(name="lastgame", description="Most‑recent activity & duration")
async def lastgame_slash(inter: discord.Interaction, member: discord.Member | None = None):
    async def _impl():
        target = member or inter.user

        async with bot.db.execute(
            """SELECT game, started, ended, seconds
               FROM activity
               WHERE user_id = ? AND ended IS NULL
               ORDER BY started DESC LIMIT 1""",
            (target.id,),
        ) as cur:
            row = await cur.fetchone()

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

        start_fmt = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        if ended_str:
            end_dt = datetime.fromisoformat(ended_str)
            end_fmt = end_dt.strftime("%Y-%m-%d %H:%M:%S")
            duration_msg = f"`{pretty(secs)}` from `{start_fmt}` to `{end_fmt}`"
        else:
            duration_msg = f"`{pretty(secs)}` since `{start_fmt}`"

        await inter.followup.send(
            f"**{target.display_name}** spent {duration_msg} in **{game}** {suffix}",
            ephemeral=True,
        )

    await log_wrap(inter, "lastgame", _impl)

# ── startup -----------------------------------------------------------------
@bot.event
async def on_ready():
    bot.db = await aiosqlite.connect(DB_PATH)
    await tree.sync()
    log.info("Logged in as %s • latency %.0f ms", bot.user, bot.latency * 1000)

bot.run(TOKEN)
