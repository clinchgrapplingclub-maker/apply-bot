import discord
from discord.ext import commands, tasks
import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
import asyncio

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- ENV ----------------
def get_env(name, required=True, cast=None, default=None):
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    if cast and value is not None:
        try:
            return cast(value)
        except Exception:
            raise RuntimeError(f"Invalid value for environment variable: {name}")
    return value

DISCORD_TOKEN = get_env("DISCORD_BOT_TOKEN")
ALLOWED_ROLE_ID = get_env("ALLOWED_ROLE_ID", cast=int)
ROBLOX_COOKIE = get_env("ROBLOX_COOKIE")
GROUP_ID = get_env("GROUP_ID", cast=int)
RANK_ID = get_env("RANK_1", cast=int)
RANK_NAME = "Full Access"
DATABASE_URL = get_env("DATABASE_URL")

DEMOTE_ROLE_ID = get_env("DEMOTE_ROLE_ID", cast=int)
DEMOTE_RANK_ID = get_env("DEMOTE_RANK_ID", cast=int)
LOG_CHANNEL_ID = get_env("LOG_CHANNEL_ID", cast=int)

REQUEST_TIMEOUT = 10

roblox_headers = {
    "Content-Type": "application/json",
    "Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"
}

# In-memory cache, but DB is source of truth
user_links = {}

# ---------------- DB ----------------
def get_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    discord_id BIGINT PRIMARY KEY,
                    roblox_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS temp_demotions (
                    discord_id BIGINT,
                    roblox_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (discord_id, roblox_id)
                )
            """)
            conn.commit()

def has_applied(discord_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM applications WHERE discord_id = %s", (discord_id,))
            return cur.fetchone() is not None

def save_application(discord_id, roblox_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO applications (discord_id, roblox_id)
                VALUES (%s, %s)
                ON CONFLICT (discord_id)
                DO UPDATE SET roblox_id = EXCLUDED.roblox_id
            """, (discord_id, roblox_id))
            conn.commit()

def reset_application(discord_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM applications WHERE discord_id = %s", (discord_id,))
            conn.commit()

def get_roblox_id_by_discord(discord_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roblox_id FROM applications WHERE discord_id = %s", (discord_id,))
            row = cur.fetchone()
            return row[0] if row else None

def load_user_links():
    global user_links
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_id, roblox_id FROM applications")
            rows = cur.fetchall()
            user_links = {int(discord_id): int(roblox_id) for discord_id, roblox_id in rows}

def save_temp_demote(discord_id, roblox_id, username, expires_at, created_by):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO temp_demotions (discord_id, roblox_id, username, expires_at, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (discord_id, roblox_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    expires_at = EXCLUDED.expires_at,
                    created_by = EXCLUDED.created_by
            """, (discord_id, roblox_id, username, expires_at, created_by))
            conn.commit()

def delete_temp_demote(roblox_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM temp_demotions WHERE roblox_id = %s", (roblox_id,))
            conn.commit()

def get_expired_temp_demotions():
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT discord_id, roblox_id, username, expires_at, created_by
                FROM temp_demotions
                WHERE expires_at <= NOW()
            """)
            return cur.fetchall()

# ---------------- ROLE ----------------
def has_role(member, role_id):
    return any(r.id == role_id for r in member.roles)

# ---------------- ROBLOX HTTP ----------------
def safe_request(method, url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    try:
        response = requests.request(method, url, **kwargs)
        return response
    except requests.RequestException:
        return None

def patch_with_csrf(url, json_data):
    headers = roblox_headers.copy()
    r = safe_request("PATCH", url, headers=headers, json=json_data)

    if r is None:
        return None

    if r.status_code == 403:
        token = r.headers.get("x-csrf-token")
        if token:
            headers["X-CSRF-TOKEN"] = token
            r = safe_request("PATCH", url, headers=headers, json=json_data)

    return r

def get_user_id(username):
    r = safe_request(
        "POST",
        "https://users.roblox.com/v1/usernames/users",
        json={
            "usernames": [username],
            "excludeBannedUsers": True
        }
    )
    if r and r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            return data[0]["id"]
    return None

def get_user_profile(user_id):
    if not user_id:
        return None
    r = safe_request("GET", f"https://users.roblox.com/v1/users/{user_id}")
    return r.json() if r and r.status_code == 200 else None

def is_in_group(user_id):
    r = safe_request("GET", f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    if r and r.status_code == 200:
        return any(g["group"]["id"] == GROUP_ID for g in r.json().get("data", []))
    return False

def set_rank(user_id):
    r = patch_with_csrf(
        f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}",
        {"roleId": RANK_ID}
    )
    return r is not None and r.status_code == 200

def rank_down(user_id):
    r = patch_with_csrf(
        f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}",
        {"roleId": DEMOTE_RANK_ID}
    )
    return r is not None and r.status_code == 200

# ---------------- EMBED ----------------
def embed(title, desc, color):
    e = discord.Embed(
        title=title,
        description=desc,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text="Designed And Created By @fntsheetz")
    return e

async def send_log(guild, title, desc, color):
    if not guild:
        return
    ch = guild.get_channel(LOG_CHANNEL_ID)
    if ch:
        try:
            await ch.send(embed=embed(title, desc, color))
        except Exception:
            pass

async def send_dm(user, embed_msg):
    try:
        await user.send(embed=embed_msg)
    except Exception:
        pass

# ---------------- HELPERS ----------------
def get_cached_or_db_roblox_id(discord_id):
    roblox_id = user_links.get(discord_id)
    if roblox_id:
        return roblox_id

    roblox_id = get_roblox_id_by_discord(discord_id)
    if roblox_id:
        user_links[discord_id] = roblox_id
    return roblox_id

def get_guild_member_by_discord_id(guild, discord_id):
    if not guild:
        return None
    return guild.get_member(discord_id)

# ---------------- /turfapply ----------------
@bot.slash_command(name="turfapply")
async def turfapply(ctx, username: str):
    await ctx.defer()
    member = ctx.author

    if not has_role(member, ALLOWED_ROLE_ID):
        return await ctx.respond(
            embed=embed("❌ Access Denied", "Missing role.", discord.Color.red())
        )

    if has_applied(member.id):
        return await ctx.respond(
            embed=embed("⚠️ Already Applied", "You already applied.", discord.Color.orange())
        )

    user_id = get_user_id(username)
    if not user_id:
        return await ctx.respond(
            embed=embed("❌ User Not Found", "Invalid Roblox username.", discord.Color.red())
        )

    profile = get_user_profile(user_id)
    if not profile:
        return await ctx.respond(
            embed=embed("❌ Error", "Could not fetch profile.", discord.Color.red())
        )

    if "fl13" not in profile.get("displayName", "").lower():
        return await ctx.respond(
            embed=embed("❌ Invalid Display Name", "Your Roblox display must contain 'fl13'.", discord.Color.red())
        )

    if not is_in_group(user_id):
        return await ctx.respond(
            embed=embed("❌ Not In Group", "You must be in the Roblox group.", discord.Color.red())
        )

    if set_rank(user_id):
        user_links[member.id] = user_id
        save_application(member.id, user_id)

        await ctx.respond(
            embed=embed("✅ Accepted", f"You have been ranked as {RANK_NAME}!", discord.Color.green())
        )

        await send_log(
            ctx.guild,
            "🟢 APPLICATION APPROVED",
            f"Discord: {member} ({member.id})\nRoblox ID: {user_id}\nUsername: {username}",
            discord.Color.green()
        )

        await send_dm(
            member,
            embed("🎉 Welcome To The Turf", "You've been successfully ranked!", discord.Color.green())
        )
    else:
        await ctx.respond(
            embed=embed("❌ Rank Failed", "Could not set Roblox rank. Try again later.", discord.Color.red())
        )

# ---------------- /demote ----------------
@bot.slash_command(name="demote")
async def demote(ctx, username: str, reason: str):
    await ctx.defer()
    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        return await ctx.respond(
            embed=embed("❌ No Permission", "Missing role.", discord.Color.red())
        )

    user_id = get_user_id(username)
    if not user_id:
        return await ctx.respond(
            embed=embed("❌ User Not Found", "Invalid username.", discord.Color.red())
        )

    if rank_down(user_id):
        delete_temp_demote(user_id)

        await ctx.respond(
            embed=embed("📉 Demoted", f"{username}\nReason: {reason}", discord.Color.orange())
        )

        await send_log(
            ctx.guild,
            "🔴 MANUAL DEMOTE",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nReason: {reason}",
            discord.Color.red()
        )
    else:
        await ctx.respond(
            embed=embed("❌ Demote Failed", "Could not change Roblox rank.", discord.Color.red())
        )

# ---------------- /tempdemote ----------------
@bot.slash_command(name="tempdemote")
async def tempdemote(ctx, username: str, minutes: int):
    await ctx.defer()
    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        return await ctx.respond(
            embed=embed("❌ No Permission", "Missing role.", discord.Color.red())
        )

    if minutes <= 0:
        return await ctx.respond(
            embed=embed("❌ Invalid Duration", "Minutes must be greater than 0.", discord.Color.red())
        )

    user_id = get_user_id(username)
    if not user_id:
        return await ctx.respond(
            embed=embed("❌ User Not Found", "Invalid username.", discord.Color.red())
        )

    profile = get_user_profile(user_id)
    discord_id = None

    # Try to find matching discord_id from existing applications
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_id FROM applications WHERE roblox_id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                discord_id = int(row[0])

    if rank_down(user_id):
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        save_temp_demote(discord_id, user_id, username, expires_at, admin.id)

        await ctx.respond(
            embed=embed("⏳ Temp Demoted", f"{username} for {minutes} minutes", discord.Color.orange())
        )

        await send_log(
            ctx.guild,
            "🟡 TEMP DEMOTE",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nDuration: {minutes} min\nExpires: {expires_at.isoformat()}",
            discord.Color.orange()
        )
    else:
        await ctx.respond(
            embed=embed("❌ Temp Demote Failed", "Could not change Roblox rank.", discord.Color.red())
        )

async def process_expired_temp_demotions():
    expired = get_expired_temp_demotions()
    if not expired:
        return

    for item in expired:
        discord_id = item["discord_id"]
        roblox_id = item["roblox_id"]
        username = item["username"]

        should_rerank = True
        guilds = bot.guilds

        member = None
        guild_found = None

        if discord_id is not None:
            for guild in guilds:
                member = get_guild_member_by_discord_id(guild, discord_id)
                if member:
                    guild_found = guild
                    break

            if member is None:
                should_rerank = False
            elif not has_role(member, ALLOWED_ROLE_ID):
                should_rerank = False

        profile = get_user_profile(roblox_id)
        if not profile or "fl13" not in profile.get("displayName", "").lower():
            should_rerank = False

        if should_rerank and set_rank(roblox_id):
            if guild_found:
                await send_log(
                    guild_found,
                    "🟢 TEMP DEMOTE EXPIRED",
                    f"{username} has been re-ranked",
                    discord.Color.green()
                )
            if member:
                await send_dm(
                    member,
                    embed(
                        "🎉 Temp Demote Expired",
                        "Your access has been restored.",
                        discord.Color.green()
                    )
                )
        else:
            if guild_found:
                await send_log(
                    guild_found,
                    "🔴 TEMP RE-RANK DENIED",
                    f"{username} was not re-ranked because one or more checks failed.",
                    discord.Color.red()
                )
            if member:
                await send_dm(
                    member,
                    embed(
                        "❌ Temp Re-Rank Failed",
                        "Your temp demotion expired, but you no longer meet the requirements for restoration.",
                        discord.Color.red()
                    )
                )

        delete_temp_demote(roblox_id)

@tasks.loop(minutes=1)
async def temp_demote_checker():
    await process_expired_temp_demotions()

# ---------------- AUTO DEMOTE + AUTO RE-RANK ----------------
@bot.event
async def on_member_update(before, after):
    before_roles = [r.id for r in before.roles]
    after_roles = [r.id for r in after.roles]

    # AUTO DEMOTE
    if ALLOWED_ROLE_ID in before_roles and ALLOWED_ROLE_ID not in after_roles:
        roblox_id = get_cached_or_db_roblox_id(after.id)

        if roblox_id:
            if rank_down(roblox_id):
                await send_log(
                    after.guild,
                    "🟠 AUTO DEMOTE",
                    f"{after} lost role\nRoblox ID: {roblox_id}",
                    discord.Color.orange()
                )

                user_obj = await bot.fetch_user(after.id)
                await send_dm(
                    user_obj,
                    embed(
                        "⚠️ You Have Been Demoted From The Turf",
                        "Due to losing your required Discord role, your access has been revoked.",
                        discord.Color.red()
                    )
                )

    # AUTO RE-RANK WITH DISPLAY CHECK
    if ALLOWED_ROLE_ID not in before_roles and ALLOWED_ROLE_ID in after_roles:
        roblox_id = get_cached_or_db_roblox_id(after.id)

        if roblox_id:
            profile = get_user_profile(roblox_id)

            if profile and "fl13" in profile.get("displayName", "").lower():
                if set_rank(roblox_id):
                    await send_log(
                        after.guild,
                        "🟢 AUTO RE-RANK",
                        f"{after} regained role and passed display check\nRoblox ID: {roblox_id}",
                        discord.Color.green()
                    )

                    user_obj = await bot.fetch_user(after.id)
                    await send_dm(
                        user_obj,
                        embed(
                            "🎉 You Have Been Re-Ranked",
                            "Your access has been restored in The Turf.",
                            discord.Color.green()
                        )
                    )
                else:
                    await send_log(
                        after.guild,
                        "🔴 AUTO RE-RANK FAILED",
                        f"{after} regained role and passed checks, but rank update failed.\nRoblox ID: {roblox_id}",
                        discord.Color.red()
                    )
            else:
                await send_log(
                    after.guild,
                    "🔴 RE-RANK DENIED",
                    f"{after} regained role but failed display check\nRoblox ID: {roblox_id}",
                    discord.Color.red()
                )

                user_obj = await bot.fetch_user(after.id)
                await send_dm(
                    user_obj,
                    embed(
                        "❌ Re-Rank Failed",
                        "Your Roblox display name must contain 'fl13' to regain access.",
                        discord.Color.red()
                    )
                )

# ---------------- READY ----------------
@bot.event
async def on_ready():
    try:
        init_db()
        load_user_links()

        if not temp_demote_checker.is_running():
            temp_demote_checker.start()

        print(f"Bot online: {bot.user}")
        print(f"Loaded {len(user_links)} user links from database.")
    except Exception as e:
        print(f"Startup error: {e}")

bot.run(DISCORD_TOKEN)
