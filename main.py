import discord
from discord.ext import commands, tasks
import os
import re
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone

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

REQUEST_TIMEOUT = 15
DISPLAY_REQUIRED_TEXT = "fl13"

roblox_headers = {
    "Content-Type": "application/json",
    "Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"
}

# Cache only, DB is source of truth
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
                    roblox_id BIGINT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS temp_demotions (
                    roblox_id BIGINT PRIMARY KEY,
                    discord_id BIGINT,
                    username TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    duration_text TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS manual_demotions (
                    roblox_id BIGINT PRIMARY KEY,
                    discord_id BIGINT,
                    username TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS member_state (
                    discord_id BIGINT PRIMARY KEY,
                    roblox_id BIGINT NOT NULL,
                    last_has_role BOOLEAN NOT NULL DEFAULT FALSE,
                    last_display_ok BOOLEAN NOT NULL DEFAULT FALSE,
                    last_group_ok BOOLEAN NOT NULL DEFAULT FALSE,
                    last_rank_state TEXT NOT NULL DEFAULT 'unknown',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
            return int(row[0]) if row else None

def get_discord_id_by_roblox(roblox_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_id FROM applications WHERE roblox_id = %s", (roblox_id,))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None

def load_user_links():
    global user_links
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_id, roblox_id FROM applications")
            rows = cur.fetchall()
            user_links = {int(d): int(r) for d, r in rows}

def save_temp_demote(discord_id, roblox_id, username, reason, duration_text, expires_at, created_by):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO temp_demotions (roblox_id, discord_id, username, reason, duration_text, expires_at, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (roblox_id)
                DO UPDATE SET
                    discord_id = EXCLUDED.discord_id,
                    username = EXCLUDED.username,
                    reason = EXCLUDED.reason,
                    duration_text = EXCLUDED.duration_text,
                    expires_at = EXCLUDED.expires_at,
                    created_by = EXCLUDED.created_by
            """, (roblox_id, discord_id, username, reason, duration_text, expires_at, created_by))
            conn.commit()

def get_temp_demote(roblox_id):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM temp_demotions WHERE roblox_id = %s", (roblox_id,))
            return cur.fetchone()

def delete_temp_demote(roblox_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM temp_demotions WHERE roblox_id = %s", (roblox_id,))
            conn.commit()

def get_expired_temp_demotions():
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM temp_demotions WHERE expires_at <= NOW()")
            return cur.fetchall()

def save_manual_demote(discord_id, roblox_id, username, reason, created_by):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO manual_demotions (roblox_id, discord_id, username, reason, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (roblox_id)
                DO UPDATE SET
                    discord_id = EXCLUDED.discord_id,
                    username = EXCLUDED.username,
                    reason = EXCLUDED.reason,
                    created_by = EXCLUDED.created_by
            """, (roblox_id, discord_id, username, reason, created_by))
            conn.commit()

def delete_manual_demote(roblox_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM manual_demotions WHERE roblox_id = %s", (roblox_id,))
            conn.commit()

def is_manual_demoted(roblox_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM manual_demotions WHERE roblox_id = %s", (roblox_id,))
            return cur.fetchone() is not None

def save_member_state(discord_id, roblox_id, has_role_flag, display_ok, group_ok, rank_state):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO member_state (discord_id, roblox_id, last_has_role, last_display_ok, last_group_ok, last_rank_state, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (discord_id)
                DO UPDATE SET
                    roblox_id = EXCLUDED.roblox_id,
                    last_has_role = EXCLUDED.last_has_role,
                    last_display_ok = EXCLUDED.last_display_ok,
                    last_group_ok = EXCLUDED.last_group_ok,
                    last_rank_state = EXCLUDED.last_rank_state,
                    updated_at = NOW()
            """, (discord_id, roblox_id, has_role_flag, display_ok, group_ok, rank_state))
            conn.commit()

def get_member_state(discord_id):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM member_state WHERE discord_id = %s", (discord_id,))
            return cur.fetchone()

def get_all_applications():
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT discord_id, roblox_id FROM applications")
            return cur.fetchall()

# ---------------- ROLE ----------------
def has_role(member, role_id):
    return any(r.id == role_id for r in member.roles)

# ---------------- HTTP / ROBLOX ----------------
def safe_request(method, url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as e:
        print(f"[HTTP ERROR] {method} {url} -> {e}")
        return None

def patch_with_csrf(url, json_data):
    headers = roblox_headers.copy()
    r = safe_request("PATCH", url, headers=headers, json=json_data)

    if r is None:
        print("[ROBLOX] first patch got no response")
        return None

    if r.status_code == 403:
        token = r.headers.get("x-csrf-token") or r.headers.get("X-CSRF-TOKEN")
        if token:
            headers["X-CSRF-TOKEN"] = token
            r = safe_request("PATCH", url, headers=headers, json=json_data)

    return r

def get_user_id(username):
    r = safe_request(
        "POST",
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": True}
    )
    if r and r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            return int(data[0]["id"])
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

def get_user_group_role(user_id):
    r = safe_request("GET", f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    if r and r.status_code == 200:
        for g in r.json().get("data", []):
            if g["group"]["id"] == GROUP_ID:
                return g.get("role")
    return None

def get_user_rank_in_group(user_id):
    role = get_user_group_role(user_id)
    if role:
        return int(role["id"])
    return None

def get_user_role_name_in_group(user_id):
    role = get_user_group_role(user_id)
    if role:
        return role.get("name", "Unknown")
    return None

def get_group_roles():
    r = safe_request("GET", f"https://groups.roblox.com/v1/groups/{GROUP_ID}/roles")
    if r and r.status_code == 200:
        roles = r.json().get("roles", [])
        return roles
    return []

def get_group_role_name_by_id(role_id):
    roles = get_group_roles()
    for role in roles:
        if int(role["id"]) == int(role_id):
            return role.get("name", "Unknown")
    return "Unknown"

def set_rank_to_role(user_id, role_id):
    r = patch_with_csrf(
        f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}",
        {"roleId": int(role_id)}
    )
    if r is None:
        print(f"[ROBLOX] set_rank_to_role failed for {user_id}: no response")
        return False
    print(f"[ROBLOX] set_rank_to_role status={r.status_code}")
    print(f"[ROBLOX] set_rank_to_role body={r.text[:300]}")
    return r.status_code in (200, 204)

def set_rank(user_id):
    return set_rank_to_role(user_id, RANK_ID)

def rank_down(user_id):
    return set_rank_to_role(user_id, DEMOTE_RANK_ID)

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
    if guild is None:
        print("[LOG] No guild provided")
        return

    ch = guild.get_channel(LOG_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            print(f"[LOG ERROR] fetch_channel failed: {e}")
            return

    try:
        await ch.send(embed=embed(title, desc, color))
    except Exception as e:
        print(f"[LOG ERROR] send failed: {e}")

async def send_dm(user, embed_msg):
    try:
        await user.send(embed=embed_msg)
    except Exception as e:
        print(f"[DM ERROR] Could not DM {getattr(user, 'id', 'unknown')}: {e}")

# ---------------- HELPERS ----------------
def get_cached_or_db_roblox_id(discord_id):
    roblox_id = user_links.get(discord_id)
    if roblox_id:
        return roblox_id

    roblox_id = get_roblox_id_by_discord(discord_id)
    if roblox_id:
        user_links[discord_id] = roblox_id
    return roblox_id

def get_member_from_any_guild(discord_id):
    for guild in bot.guilds:
        member = guild.get_member(discord_id)
        if member:
            return guild, member
    return None, None

def parse_duration(duration_text):
    text = duration_text.strip().lower()
    match = re.fullmatch(r"(\d+)\s*(min|m|h|d)", text)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    if amount <= 0:
        return None

    if unit in ("min", "m"):
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)

    return None

def format_dt(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def display_name_ok(profile):
    if not profile:
        return False
    return DISPLAY_REQUIRED_TEXT in profile.get("displayName", "").lower()

async def log_command(ctx, title, description, color):
    await send_log(ctx.guild, title, description, color)

async def dm_by_discord_id(discord_id, embed_msg):
    if discord_id is None:
        return
    try:
        user = await bot.fetch_user(discord_id)
        await send_dm(user, embed_msg)
    except Exception as e:
        print(f"[DM ERROR] fetch_user failed for {discord_id}: {e}")

# ---------------- RANK VIEW ----------------
class RankSelect(discord.ui.Select):
    def __init__(self, ctx, target_username, target_user_id, target_discord_id, options_data):
        self.ctx = ctx
        self.target_username = target_username
        self.target_user_id = target_user_id
        self.target_discord_id = target_discord_id

        options = []
        for role in options_data[:25]:
            options.append(
                discord.SelectOption(
                    label=role["name"][:100],
                    value=str(role["id"]),
                    description=f"Rank: {role.get('rank', 'N/A')}"[:100]
                )
            )

        super().__init__(
            placeholder="Choose a Roblox group rank",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message(
                embed=embed("❌ Not Allowed", "Only the command user can choose a rank.", discord.Color.red()),
                ephemeral=True
            )

        selected_role_id = int(self.values[0])
        selected_role_name = get_group_role_name_by_id(selected_role_id)
        current_role_id = get_user_rank_in_group(self.target_user_id)
        current_role_name = get_user_role_name_in_group(self.target_user_id)

        if current_role_id == selected_role_id:
            return await interaction.response.send_message(
                embed=embed(
                    "ℹ️ Already That Rank",
                    f"{self.target_username} is already ranked as {selected_role_name}.",
                    discord.Color.blue()
                ),
                ephemeral=True
            )

        ok = set_rank_to_role(self.target_user_id, selected_role_id)

        if not ok:
            await send_log(
                self.ctx.guild,
                "❌ /rank FAILED",
                f"Admin: {self.ctx.author} ({self.ctx.author.id})\nTarget: {self.target_username}\nRoblox ID: {self.target_user_id}\nRequested rank: {selected_role_name} ({selected_role_id})\nReason: Rank API failed",
                discord.Color.red()
            )
            return await interaction.response.send_message(
                embed=embed("❌ Rank Failed", "Could not set that Roblox rank.", discord.Color.red()),
                ephemeral=True
            )

        if self.target_discord_id is not None:
            save_member_state(self.target_discord_id, self.target_user_id, True, True, True, "ranked")

        delete_manual_demote(self.target_user_id)
        delete_temp_demote(self.target_user_id)

        await send_log(
            self.ctx.guild,
            "🟢 /rank SUCCESS",
            f"Admin: {self.ctx.author} ({self.ctx.author.id})\nTarget: {self.target_username}\nRoblox ID: {self.target_user_id}\nOld role: {current_role_name} ({current_role_id})\nNew role: {selected_role_name} ({selected_role_id})",
            discord.Color.green()
        )

        if self.target_discord_id is not None:
            await dm_by_discord_id(
                self.target_discord_id,
                embed(
                    "🟢 Your Roblox Group Rank Was Updated",
                    f"You have been ranked to: {selected_role_name}.",
                    discord.Color.green()
                )
            )

        await interaction.response.send_message(
            embed=embed(
                "✅ Rank Updated",
                f"{self.target_username} has been ranked to {selected_role_name}.",
                discord.Color.green()
            ),
            ephemeral=True
        )

class RankView(discord.ui.View):
    def __init__(self, ctx, target_username, target_user_id, target_discord_id, roles):
        super().__init__(timeout=120)
        self.add_item(RankSelect(ctx, target_username, target_user_id, target_discord_id, roles))

# ---------------- /turfapply ----------------
@bot.slash_command(name="turfapply")
async def turfapply(ctx, username: str):
    await ctx.defer()
    member = ctx.author

    if not has_role(member, ALLOWED_ROLE_ID):
        await log_command(
            ctx,
            "❌ /turfapply DENIED",
            f"User: {member} ({member.id})\nUsername: {username}\nReason: Missing role",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ Access Denied", "Missing role.", discord.Color.red()))

    if has_applied(member.id):
        await log_command(
            ctx,
            "⚠️ /turfapply BLOCKED",
            f"User: {member} ({member.id})\nUsername: {username}\nReason: Already applied",
            discord.Color.orange()
        )
        return await ctx.respond(embed=embed("⚠️ Already Applied", "You already applied.", discord.Color.orange()))

    user_id = get_user_id(username)
    if not user_id:
        await log_command(
            ctx,
            "❌ /turfapply FAILED",
            f"User: {member} ({member.id})\nUsername: {username}\nReason: Invalid Roblox username",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ User Not Found", "Invalid Roblox username.", discord.Color.red()))

    profile = get_user_profile(user_id)
    if not profile:
        await log_command(
            ctx,
            "❌ /turfapply FAILED",
            f"User: {member} ({member.id})\nUsername: {username}\nRoblox ID: {user_id}\nReason: Could not fetch profile",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ Error", "Could not fetch profile.", discord.Color.red()))

    if not display_name_ok(profile):
        await log_command(
            ctx,
            "❌ /turfapply FAILED",
            f"User: {member} ({member.id})\nUsername: {username}\nRoblox ID: {user_id}\nReason: Display name missing '{DISPLAY_REQUIRED_TEXT}'",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ Invalid Display Name", f"Your Roblox display must contain '{DISPLAY_REQUIRED_TEXT}'.", discord.Color.red()))

    if not is_in_group(user_id):
        await log_command(
            ctx,
            "❌ /turfapply FAILED",
            f"User: {member} ({member.id})\nUsername: {username}\nRoblox ID: {user_id}\nReason: Not in Roblox group",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ Not In Group", "You must be in the Roblox group.", discord.Color.red()))

    current_role_id = get_user_rank_in_group(user_id)
    current_role_name = get_user_role_name_in_group(user_id)

    if current_role_id == RANK_ID:
        await log_command(
            ctx,
            "ℹ️ /turfapply ALREADY RANKED",
            f"User: {member} ({member.id})\nUsername: {username}\nRoblox ID: {user_id}\nCurrent role: {current_role_name} ({current_role_id})",
            discord.Color.blue()
        )
        return await ctx.respond(
            embed=embed("ℹ️ Already Ranked", f"You are already ranked as {current_role_name}.", discord.Color.blue())
        )

    if current_role_id is not None and current_role_id != RANK_ID:
        group_roles = get_group_roles()
        target_role_data = next((r for r in group_roles if int(r["id"]) == int(RANK_ID)), None)
        current_role_data = next((r for r in group_roles if int(r["id"]) == int(current_role_id)), None)

        if current_role_data and target_role_data:
            if int(current_role_data.get("rank", 0)) > int(target_role_data.get("rank", 0)):
                await log_command(
                    ctx,
                    "ℹ️ /turfapply HIGHER RANK",
                    f"User: {member} ({member.id})\nUsername: {username}\nRoblox ID: {user_id}\nCurrent role: {current_role_name} ({current_role_id})",
                    discord.Color.blue()
                )
                return await ctx.respond(
                    embed=embed("ℹ️ Already Higher Rank", f"You already have a higher rank: {current_role_name}.", discord.Color.blue())
                )

    if set_rank(user_id):
        save_application(member.id, user_id)
        user_links[member.id] = user_id
        save_member_state(member.id, user_id, True, True, True, "ranked")

        await ctx.respond(embed=embed("✅ Accepted", f"You have been ranked as {RANK_NAME}!", discord.Color.green()))

        await log_command(
            ctx,
            "🟢 /turfapply SUCCESS",
            f"User: {member} ({member.id})\nRoblox Username: {username}\nRoblox ID: {user_id}",
            discord.Color.green()
        )

        await send_dm(
            member,
            embed(
                "🎉 Welcome To The Turf",
                f"You have been accepted and ranked as {RANK_NAME}.",
                discord.Color.green()
            )
        )
    else:
        await log_command(
            ctx,
            "❌ /turfapply FAILED",
            f"User: {member} ({member.id})\nUsername: {username}\nRoblox ID: {user_id}\nReason: Rank API failed",
            discord.Color.red()
        )
        await ctx.respond(embed=embed("❌ Rank Failed", "Could not set Roblox rank. Try again later.", discord.Color.red()))

# ---------------- /demote ----------------
@bot.slash_command(name="demote")
async def demote(ctx, username: str, reason: str):
    await ctx.defer()
    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        await log_command(
            ctx,
            "❌ /demote DENIED",
            f"Admin: {admin} ({admin.id})\nUsername: {username}\nReason: Missing permission role",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ No Permission", "Missing role.", discord.Color.red()))

    user_id = get_user_id(username)
    if not user_id:
        await log_command(
            ctx,
            "❌ /demote FAILED",
            f"Admin: {admin} ({admin.id})\nUsername: {username}\nReason: Invalid username",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ User Not Found", "Invalid username.", discord.Color.red()))

    discord_id = get_discord_id_by_roblox(user_id)
    current_role_id = get_user_rank_in_group(user_id)
    current_role_name = get_user_role_name_in_group(user_id)
    target_role_name = get_group_role_name_by_id(DEMOTE_RANK_ID)

    if current_role_id == DEMOTE_RANK_ID:
        await log_command(
            ctx,
            "ℹ️ /demote SKIPPED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nReason: Already demoted rank ({target_role_name})",
            discord.Color.blue()
        )
        return await ctx.respond(
            embed=embed("ℹ️ Already Demoted", f"{username} is already {target_role_name}.", discord.Color.blue())
        )

    if rank_down(user_id):
        delete_temp_demote(user_id)
        save_manual_demote(discord_id, user_id, username, reason, admin.id)

        if discord_id is not None:
            save_member_state(discord_id, user_id, True, True, True, "manual_demoted")

        await ctx.respond(embed=embed("📉 Demoted", f"{username}\nReason: {reason}", discord.Color.orange()))

        await log_command(
            ctx,
            "🔴 /demote SUCCESS",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nDiscord ID: {discord_id}\nOld role: {current_role_name} ({current_role_id})\nNew role: {target_role_name} ({DEMOTE_RANK_ID})\nReason: {reason}",
            discord.Color.red()
        )

        if discord_id is not None:
            await dm_by_discord_id(
                discord_id,
                embed(
                    "🔴 You Have Been Permanently Demoted",
                    f"You have been demoted from The Turf.\n\nReason: {reason}\nDuration: Permanent",
                    discord.Color.red()
                )
            )
    else:
        await log_command(
            ctx,
            "❌ /demote FAILED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nReason: Rank API failed",
            discord.Color.red()
        )
        await ctx.respond(embed=embed("❌ Demote Failed", "Could not change Roblox rank.", discord.Color.red()))

# ---------------- /tempdemote ----------------
@bot.slash_command(name="tempdemote")
async def tempdemote(ctx, username: str, duration: str, reason: str):
    await ctx.defer()
    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        await log_command(
            ctx,
            "❌ /tempdemote DENIED",
            f"Admin: {admin} ({admin.id})\nUsername: {username}\nReason: Missing permission role",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ No Permission", "Missing role.", discord.Color.red()))

    delta = parse_duration(duration)
    if delta is None:
        await log_command(
            ctx,
            "❌ /tempdemote FAILED",
            f"Admin: {admin} ({admin.id})\nUsername: {username}\nReason: Invalid duration '{duration}'",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ Invalid Duration", "Use for example: 1min, 5min, 1h, 12h, 1d", discord.Color.red()))

    user_id = get_user_id(username)
    if not user_id:
        await log_command(
            ctx,
            "❌ /tempdemote FAILED",
            f"Admin: {admin} ({admin.id})\nUsername: {username}\nReason: Invalid username",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ User Not Found", "Invalid username.", discord.Color.red()))

    discord_id = get_discord_id_by_roblox(user_id)
    expires_at = datetime.now(timezone.utc) + delta
    current_role_id = get_user_rank_in_group(user_id)
    current_role_name = get_user_role_name_in_group(user_id)
    target_role_name = get_group_role_name_by_id(DEMOTE_RANK_ID)

    if current_role_id == DEMOTE_RANK_ID:
        save_temp_demote(discord_id, user_id, username, reason, duration, expires_at, admin.id)

        await log_command(
            ctx,
            "ℹ️ /tempdemote UPDATED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nReason: Already on demoted rank, timer updated\nDuration: {duration}\nExpires: {format_dt(expires_at)}",
            discord.Color.blue()
        )

        if discord_id is not None:
            await dm_by_discord_id(
                discord_id,
                embed(
                    "🟡 Temporary Demotion Updated",
                    f"Your temporary demotion timer has been updated.\n\nReason: {reason}\nDuration: {duration}\nEnds: {format_dt(expires_at)}",
                    discord.Color.orange()
                )
            )

        return await ctx.respond(
            embed=embed("ℹ️ Temp Demotion Updated", f"{username} was already demoted. Timer updated to {duration}.", discord.Color.blue())
        )

    if rank_down(user_id):
        save_temp_demote(discord_id, user_id, username, reason, duration, expires_at, admin.id)

        if discord_id is not None:
            save_member_state(discord_id, user_id, True, True, True, "temp_demoted")

        await ctx.respond(embed=embed("⏳ Temp Demoted", f"{username}\nDuration: {duration}\nReason: {reason}", discord.Color.orange()))

        await log_command(
            ctx,
            "🟡 /tempdemote SUCCESS",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nDiscord ID: {discord_id}\nOld role: {current_role_name} ({current_role_id})\nNew role: {target_role_name} ({DEMOTE_RANK_ID})\nDuration: {duration}\nExpires: {format_dt(expires_at)}\nReason: {reason}",
            discord.Color.orange()
        )

        if discord_id is not None:
            await dm_by_discord_id(
                discord_id,
                embed(
                    "🟡 You Have Been Temporarily Demoted",
                    f"You have been temporarily demoted from The Turf.\n\nReason: {reason}\nDuration: {duration}\nEnds: {format_dt(expires_at)}",
                    discord.Color.orange()
                )
            )
    else:
        await log_command(
            ctx,
            "❌ /tempdemote FAILED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nReason: Rank API failed",
            discord.Color.red()
        )
        await ctx.respond(embed=embed("❌ Temp Demote Failed", "Could not change Roblox rank.", discord.Color.red()))

# ---------------- /rank ----------------
@bot.slash_command(name="rank")
async def rank(ctx, username: str):
    await ctx.defer(ephemeral=True)
    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        await log_command(
            ctx,
            "❌ /rank DENIED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nReason: Missing permission role",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ No Permission", "Missing role.", discord.Color.red()), ephemeral=True)

    user_id = get_user_id(username)
    if not user_id:
        await log_command(
            ctx,
            "❌ /rank FAILED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nReason: Invalid Roblox username",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ User Not Found", "Invalid Roblox username.", discord.Color.red()), ephemeral=True)

    if not is_in_group(user_id):
        await log_command(
            ctx,
            "❌ /rank FAILED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nReason: User not in Roblox group",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ Not In Group", "That user is not in the Roblox group.", discord.Color.red()), ephemeral=True)

    roles = get_group_roles()
    if not roles:
        await log_command(
            ctx,
            "❌ /rank FAILED",
            f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nReason: Could not fetch group roles",
            discord.Color.red()
        )
        return await ctx.respond(embed=embed("❌ Error", "Could not fetch group ranks.", discord.Color.red()), ephemeral=True)

    roles_sorted = sorted(roles, key=lambda r: int(r.get("rank", 0)), reverse=True)
    discord_id = get_discord_id_by_roblox(user_id)
    current_role_name = get_user_role_name_in_group(user_id)

    await log_command(
        ctx,
        "🟣 /rank OPENED",
        f"Admin: {admin} ({admin.id})\nTarget: {username}\nRoblox ID: {user_id}\nCurrent role: {current_role_name}",
        discord.Color.blurple()
    )

    await ctx.respond(
        embed=embed(
            "Select Rank",
            f"Choose a new Roblox rank for {username}.\nCurrent role: {current_role_name}",
            discord.Color.blurple()
        ),
        view=RankView(ctx, username, user_id, discord_id, roles_sorted),
        ephemeral=True
    )

# ---------------- TEMP DEMOTE EXPIRY ----------------
async def process_expired_temp_demotions():
    expired = get_expired_temp_demotions()
    if not expired:
        return

    for item in expired:
        roblox_id = int(item["roblox_id"])
        discord_id = item["discord_id"]
        username = item["username"]
        reason = item["reason"]
        duration_text = item["duration_text"]

        guild = None
        member = None

        if discord_id is not None:
            guild, member = get_member_from_any_guild(int(discord_id))

        profile = get_user_profile(roblox_id)
        display_ok = display_name_ok(profile)
        group_ok = is_in_group(roblox_id)
        has_role_flag = member is not None and has_role(member, ALLOWED_ROLE_ID)
        blocked = is_manual_demoted(roblox_id)

        if not blocked and has_role_flag and display_ok and group_ok and set_rank(roblox_id):
            if guild:
                await send_log(
                    guild,
                    "🟢 TEMP DEMOTE ENDED",
                    f"Target: {username}\nRoblox ID: {roblox_id}\nDiscord ID: {discord_id}\nOriginal reason: {reason}\nOriginal duration: {duration_text}",
                    discord.Color.green()
                )

            if discord_id is not None:
                await dm_by_discord_id(
                    int(discord_id),
                    embed(
                        "🟢 Temp Demotion Ended",
                        f"Your temporary demotion has ended and your access has been restored.\n\nPrevious reason: {reason}\nPrevious duration: {duration_text}",
                        discord.Color.green()
                    )
                )

            if discord_id is not None:
                save_member_state(int(discord_id), roblox_id, has_role_flag, display_ok, group_ok, "ranked")
        else:
            if guild:
                await send_log(
                    guild,
                    "🔴 TEMP DEMOTE ENDED - NO RE-RANK",
                    f"Target: {username}\nRoblox ID: {roblox_id}\nDiscord ID: {discord_id}\nBlocked manual demote: {blocked}\nHas role: {has_role_flag}\nDisplay ok: {display_ok}\nGroup ok: {group_ok}",
                    discord.Color.red()
                )

            if discord_id is not None:
                await dm_by_discord_id(
                    int(discord_id),
                    embed(
                        "🔴 Temp Demotion Ended - Access Not Restored",
                        "Your temporary demotion has ended, but your access was not restored because one or more requirements are not currently met.",
                        discord.Color.red()
                    )
                )

            if discord_id is not None:
                save_member_state(int(discord_id), roblox_id, has_role_flag, display_ok, group_ok, "deranked")

        delete_temp_demote(roblox_id)

@tasks.loop(minutes=1)
async def temp_demote_checker():
    await process_expired_temp_demotions()

# ---------------- MEMBER STATUS CHECKER ----------------
async def evaluate_member_access(discord_id, roblox_id):
    guild, member = get_member_from_any_guild(int(discord_id))
    if guild is None or member is None:
        return

    profile = get_user_profile(roblox_id)
    has_role_flag = has_role(member, ALLOWED_ROLE_ID)
    display_ok = display_name_ok(profile)
    group_ok = is_in_group(roblox_id)

    prev = get_member_state(int(discord_id))
    prev_rank_state = prev["last_rank_state"] if prev else "unknown"

    blocked_manual = is_manual_demoted(roblox_id)
    blocked_temp = get_temp_demote(roblox_id) is not None

    eligible_for_rank = has_role_flag and display_ok and group_ok and not blocked_manual and not blocked_temp

    if eligible_for_rank:
        if prev_rank_state != "ranked":
            if set_rank(roblox_id):
                await send_log(
                    guild,
                    "🟢 AUTO RE-RANK",
                    f"Member: {member} ({member.id})\nRoblox ID: {roblox_id}\nReason: Role/display/group requirements restored",
                    discord.Color.green()
                )
                await send_dm(
                    member,
                    embed(
                        "🎉 You Have Been Re-Ranked",
                        "Your required role/display conditions are now valid again, and your access has been restored in The Turf.",
                        discord.Color.green()
                    )
                )
                save_member_state(int(discord_id), roblox_id, has_role_flag, display_ok, group_ok, "ranked")
                return
            else:
                await send_log(
                    guild,
                    "❌ AUTO RE-RANK FAILED",
                    f"Member: {member} ({member.id})\nRoblox ID: {roblox_id}\nReason: Rank API failed",
                    discord.Color.red()
                )
                save_member_state(int(discord_id), roblox_id, has_role_flag, display_ok, group_ok, "deranked")
                return

        save_member_state(int(discord_id), roblox_id, has_role_flag, display_ok, group_ok, "ranked")
        return

    if prev_rank_state != "deranked" and prev_rank_state != "manual_demoted" and prev_rank_state != "temp_demoted":
        if rank_down(roblox_id):
            reason_parts = []
            if not has_role_flag:
                reason_parts.append("required Discord role missing")
            if not display_ok:
                reason_parts.append(f"Roblox display name missing '{DISPLAY_REQUIRED_TEXT}'")
            if not group_ok:
                reason_parts.append("not in Roblox group")
            if blocked_manual:
                reason_parts.append("manually demoted")
            if blocked_temp:
                reason_parts.append("temporary demotion active")

            reason_text = ", ".join(reason_parts) if reason_parts else "requirements not met"

            await send_log(
                guild,
                "🟠 AUTO DERANK",
                f"Member: {member} ({member.id})\nRoblox ID: {roblox_id}\nReason: {reason_text}",
                discord.Color.orange()
            )

            if not blocked_manual and not blocked_temp:
                await send_dm(
                    member,
                    embed(
                        "⚠️ Your Access Has Been Revoked",
                        f"You were deranked from The Turf because: {reason_text}. When the required conditions are restored, your access can be restored automatically.",
                        discord.Color.red()
                    )
                )

    new_state = "deranked"
    if blocked_manual:
        new_state = "manual_demoted"
    elif blocked_temp:
        new_state = "temp_demoted"

    save_member_state(int(discord_id), roblox_id, has_role_flag, display_ok, group_ok, new_state)

@tasks.loop(minutes=2)
async def member_status_checker():
    apps = get_all_applications()
    for item in apps:
        discord_id = int(item["discord_id"])
        roblox_id = int(item["roblox_id"])
        try:
            await evaluate_member_access(discord_id, roblox_id)
        except Exception as e:
            print(f"[STATUS CHECK ERROR] discord_id={discord_id}, roblox_id={roblox_id} -> {e}")

# ---------------- MEMBER UPDATE EVENT ----------------
@bot.event
async def on_member_update(before, after):
    before_roles = [r.id for r in before.roles]
    after_roles = [r.id for r in after.roles]

    if (ALLOWED_ROLE_ID in before_roles) != (ALLOWED_ROLE_ID in after_roles):
        roblox_id = get_cached_or_db_roblox_id(after.id)
        if roblox_id:
            try:
                await evaluate_member_access(after.id, roblox_id)
            except Exception as e:
                print(f"[on_member_update ERROR] {after.id} -> {e}")

# ---------------- READY ----------------
@bot.event
async def on_ready():
    try:
        init_db()
        load_user_links()

        if not temp_demote_checker.is_running():
            temp_demote_checker.start()

        if not member_status_checker.is_running():
            member_status_checker.start()

        print(f"Bot online: {bot.user}")
        print(f"Loaded {len(user_links)} user links from database.")
    except Exception as e:
        print(f"Startup error: {e}")

bot.run(DISCORD_TOKEN)
