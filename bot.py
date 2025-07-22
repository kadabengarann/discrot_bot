"""
Discord “Last‑Game & Play‑Time” bot.
Requires Guild Members + Guild Presences privileged intents (enable in portal).
"""

import os, asyncio, sqlite3
import discord
from discord.ext import commands
from dotenv import load_dotenv

from datetime import datetime, timezone

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]

# --- persistent DB -----------------------------------------------------------
DB_PATH = "data/activity.db"
os.makedirs("data", exist_ok=True)
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.execute("""CREATE TABLE IF NOT EXISTS activity (
                user_id INTEGER,
                game     TEXT,
                started  TEXT,
                ended    TEXT,
                seconds  INTEGER
             )""")

# --- bot + intents -----------------------------------------------------------
intents = discord.Intents.default()
intents.presences = intents.members = True   # privileged ✓ :contentReference[oaicite:0]{index=0}
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

launch_time = datetime.now(timezone.utc)
active_sessions: dict[int, tuple[str, datetime.datetime]] = {}

# --------------------------------------------------------------------------- #
#  Presence tracking                                                          #
# --------------------------------------------------------------------------- #
@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    """Store start/stop times whenever a user’s game presence changes."""
    game = next((a for a in after.activities if isinstance(a, discord.Game)), None)

    uid = after.id
    # 1️⃣ Game started or changed --------------------------------------------
    if game:
        start_ts = getattr(game, "start", None) or datetime.now(timezone.utc)
        if uid not in active_sessions or active_sessions[uid][0] != game.name:
            active_sessions[uid] = (game.name, start_ts)
            db.execute("INSERT INTO activity (user_id, game, started) VALUES (?,?,?)",
                       (uid, game.name, start_ts.isoformat()))
    # 2️⃣ Game stopped --------------------------------------------------------
    else:
        if uid in active_sessions:
            g, start_ts = active_sessions.pop(uid)
            end_ts = datetime.now(timezone.utc)
            secs = int((end_ts - start_ts).total_seconds())
            db.execute("""UPDATE activity
                          SET ended=?, seconds=?
                          WHERE user_id=? AND game=? AND ended IS NULL""",
                       (end_ts.isoformat(), secs, uid, g))

# --------------------------------------------------------------------------- #
#  Helper to format seconds → HH h MM m                                        #
# --------------------------------------------------------------------------- #
def pretty_dur(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    return f"{h:02d}h {m:02d}m"

# --------------------------------------------------------------------------- #
#  Commands                                                                   #
# --------------------------------------------------------------------------- #
@bot.command(name="lastgame")
async def lastgame(ctx, member: discord.Member | None = None):
    """Show last game + duration."""
    member = member or ctx.author
    row = db.execute("""SELECT game, started, ended, seconds
                        FROM activity WHERE user_id=?
                        ORDER BY started DESC LIMIT 1""",
                     (member.id,)).fetchone()

    if not row:
        return await ctx.reply("No recorded sessions yet.")

    game, started, ended, secs = row
    if not ended:                                    # still playing
        secs = int((datetime.now(timezone.utc) -
                     datetime.datetime.fromisoformat(started)).total_seconds())
        status = " (still playing)"
    else:
        status = ""

    await ctx.reply(f"**{member.display_name}** played **{game}** "
                    f"for `{pretty_dur(secs)}`{status}")

# --- diagnostics ------------------------------------------------------------
@bot.command(name="ping")
async def ping(ctx):
    """Latency check."""
    await ctx.reply(f"Pong! `{round(bot.latency*1000)} ms`")  # :contentReference[oaicite:1]{index=1}

@bot.command(name="uptime")
async def uptime(ctx):
    delta = datetime.now(timezone.utc) - launch_time
    await ctx.reply(f"Uptime: `{pretty_dur(int(delta.total_seconds()))}`")

# --- optional slash mirrors -------------------------------------------------
@tree.command(name="ping", description="Test latency")
async def ping_slash(inter: discord.Interaction):
    await inter.response.send_message(f"Pong! `{round(bot.latency*1000)} ms`")

@tree.command(name="uptime", description="Bot uptime")
async def uptime_slash(inter: discord.Interaction):
    delta = datetime.now(timezone.utc) - launch_time
    await inter.response.send_message(f"`{pretty_dur(int(delta.total_seconds()))}`")

@tree.command(name="lastgame", description="Show last game & duration")
async def lastgame_slash(inter: discord.Interaction,
                         member: discord.Member | None = None):
    ctx = await commands.Context.from_interaction(inter)  # reuse prefix logic
    await lastgame(ctx, member)

# --- ready event: sync slash cmds once --------------------------------------
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user} (latency {round(bot.latency*1000)} ms)")

bot.run(TOKEN)
