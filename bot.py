"""
Discord “Game & Activity” tracker – slash‑only, async DB, multi‑activity aware.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List

import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands, TextChannel
from discord.interactions import Interaction
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
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS activity(
              user_id INTEGER,
              game    TEXT,
              started TEXT,
              ended   TEXT,
              seconds INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_user ON activity(user_id);

            CREATE TABLE IF NOT EXISTS guild_settings(
              guild_id          INTEGER PRIMARY KEY,
              notify_channel_id INTEGER,
              timezone_offset   INTEGER DEFAULT 0
            );
        """)
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
    ended   = before_set - after_set
    if not started and not ended:
        return

    async with db_lock:
        ts_now = datetime.now(timezone.utc)

        # START sessions
        rows = []
        for act in after_acts:
            if activity_key(act) in started:
                active_sessions.setdefault(uid, {})[activity_key(act)] = ts_now
                rows.append((uid, act.name, ts_now.isoformat(), None, None))
        if rows:
            await bot.db.executemany("INSERT INTO activity VALUES (?,?,?,?,?)", rows)

        # END sessions & announce
        for act in before_acts:
            key = activity_key(act)
            if key in ended:
                start_ts = active_sessions.get(uid, {}).pop(key, None)
                if not start_ts:
                    continue
                secs = int((ts_now - start_ts).total_seconds())
                await bot.db.execute(
                    """
                    UPDATE activity
                       SET ended = ?, seconds = ?
                     WHERE user_id = ? AND game = ? AND ended IS NULL
                    """,
                    (ts_now.isoformat(), secs, uid, act.name),
                )

                # fetch this guild's settings
                guild_id = after.guild.id
                async with bot.db.execute(
                    "SELECT notify_channel_id, timezone_offset FROM guild_settings WHERE guild_id=?",
                    (guild_id,),
                ) as cur:
                    row = await cur.fetchone()
                if row and row[0]:
                    channel_id, offset = row
                    channel = bot.get_channel(channel_id)
                    if channel:
                        local_tz = timezone(timedelta(hours=offset))
                        start_local = start_ts.replace(tzinfo=timezone.utc).astimezone(local_tz)
                        end_local   = ts_now.replace(tzinfo=timezone.utc).astimezone(local_tz)
                        await channel.send(
                            f"**{after.display_name}** finished **{act.name}**.\n"
                            f"▶️ Duration: `{pretty(secs)}`\n"
                            f"🕒 From `{start_local:%Y-%m-%d %H:%M:%S}` to `{end_local:%Y-%m-%d %H:%M:%S}` (UTC{offset:+d})"
                        )

        await bot.db.commit()

# ── /setting group ---------------------------------------------------------
class SettingGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="setting", description="Configure this server’s settings")

    @app_commands.command(name="channel", description="Notification channel")
    @app_commands.describe(channel="Where to post end‑of‑activity notices")
    @app_commands.checks.has_permissions(administrator=True)
    async def channel(self, inter: Interaction, channel: TextChannel):
        await inter.response.defer(ephemeral=True)
        await bot.db.execute(
            """
            INSERT INTO guild_settings(guild_id, notify_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE
              SET notify_channel_id = excluded.notify_channel_id
            """,
            (inter.guild.id, channel.id),
        )
        await bot.db.commit()
        await inter.followup.send(f"✅ Notifications will post in {channel.mention}", ephemeral=True)

    @app_commands.command(name="timezone", description="UTC offset in hours")
    @app_commands.describe(offset="Hours from UTC, e.g. +8 or -5")
    @app_commands.checks.has_permissions(administrator=True)
    async def timezone(self, inter: Interaction, offset: int):
        await inter.response.defer(ephemeral=True)
        await bot.db.execute(
            """
            INSERT INTO guild_settings(guild_id, timezone_offset)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE
              SET timezone_offset = excluded.timezone_offset
            """,
            (inter.guild.id, offset),
        )
        await bot.db.commit()
        await inter.followup.send(f"✅ Timezone offset set to UTC{offset:+d}", ephemeral=True)

bot.tree.add_command(SettingGroup())

# ── simple slash commands --------------------------------------------------
@tree.command(name="ping", description="Latency check")
async def ping_slash(inter: discord.Interaction):
    try:
        await inter.response.send_message(f"Pong! `{round(bot.latency * 1000)} ms`", ephemeral=True)
    except:
        pass

@tree.command(name="uptime", description="Bot uptime")
async def uptime_slash(inter: discord.Interaction):
    try:
        delta = datetime.now(timezone.utc) - launch_time
        await inter.response.send_message(f"Uptime: `{pretty(int(delta.total_seconds()))}`", ephemeral=True)
    except:
        pass

@tree.command(name="lastgame", description="Most‑recent activity & duration")
async def lastgame_slash(inter: discord.Interaction, member: discord.Member | None = None):
    try:
        target = member or inter.user
        # fetch timezone offset
        async with bot.db.execute(
            "SELECT timezone_offset FROM guild_settings WHERE guild_id=?",
            (inter.guild.id,),
        ) as cur:
            tz_row = await cur.fetchone()
        offset = tz_row[0] if tz_row else 0
        local_tz = timezone(timedelta(hours=offset))

        async with bot.db.execute(
            """
            SELECT game, started, ended, seconds
              FROM activity
             WHERE user_id = ? AND ended IS NULL
          ORDER BY started DESC LIMIT 1
            """,
            (target.id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            async with bot.db.execute(
                """
                SELECT game, started, ended, seconds
                  FROM activity
                 WHERE user_id = ?
              ORDER BY started DESC LIMIT 1
                """,
                (target.id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return await inter.response.send_message("No sessions recorded yet.", ephemeral=True)

        game, started_str, ended_str, secs = row
        start_utc = datetime.fromisoformat(started_str).replace(tzinfo=timezone.utc)
        start_local = start_utc.astimezone(local_tz)

        if ended_str is None:
            secs = int((datetime.now(timezone.utc) - start_utc).total_seconds())
            suffix = " (still running)"
            duration_msg = f"`{pretty(secs)}` since `{start_local:%Y-%m-%d %H:%M:%S}`"
        else:
            end_utc   = datetime.fromisoformat(ended_str).replace(tzinfo=timezone.utc)
            end_local = end_utc.astimezone(local_tz)
            suffix = ""
            duration_msg = f"`{pretty(secs)}` from `{start_local:%Y-%m-%d %H:%M:%S}` to `{end_local:%Y-%m-%d %H:%M:%S}`"

        await inter.response.send_message(
            f"**{target.display_name}** spent {duration_msg} in **{game}** {suffix}",
            ephemeral=True,
        )
    except:
        pass

# ── startup -----------------------------------------------------------------
@bot.event
async def on_ready():
    bot.db = await aiosqlite.connect(DB_PATH)
    await tree.sync()
    log.info("Logged in as %s • latency %.0f ms", bot.user, bot.latency * 1000)

bot.run(TOKEN)
