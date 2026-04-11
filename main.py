import discord
from discord.ext import commands
import os
import requests
import psycopg2
from datetime import datetime
import asyncio
import re

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ENV
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_ROLE_ID = int(os.getenv("ALLOWED_ROLE_ID"))
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")
GROUP_ID = int(os.getenv("GROUP_ID"))
RANK_ID = int(os.getenv("RANK_1"))
RANK_NAME = "Full Access"
DATABASE_URL = os.getenv("DATABASE_URL")

DEMOTE_ROLE_ID = int(os.getenv("DEMOTE_ROLE_ID"))
DEMOTE_RANK_ID = int(os.getenv("DEMOTE_RANK_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

user_links = {}

roblox_headers = {
    'Content-Type': 'application/json',
    'Cookie': f'.ROBLOSECURITY={ROBLOX_COOKIE}'
}

# ---------------- DB ----------------
def get_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def has_applied(discord_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM applications WHERE discord_id = %s", (discord_id,))
            return cur.fetchone() is not None

def save_application(discord_id, roblox_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications (discord_id, roblox_id) VALUES (%s, %s)",
                (discord_id, roblox_id)
            )
            conn.commit()

def reset_application(discord_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM applications WHERE discord_id = %s", (discord_id,))
            conn.commit()

# ---------------- ROLE ----------------
def has_role(member, role_id):
    return any(r.id == role_id for r in member.roles)

# ---------------- ROBLOX ----------------
def patch_with_csrf(url, json_data):
    headers = roblox_headers.copy()
    r = requests.patch(url, headers=headers, json=json_data)

    if r.status_code == 403:
        token = r.headers.get('x-csrf-token')
        if token:
            headers['X-CSRF-TOKEN'] = token
            r = requests.patch(url, headers=headers, json=json_data)

    return r

def get_user_id(username):
    r = requests.post("https://users.roblox.com/v1/usernames/users",
                      json={"usernames": [username]})
    if r.status_code == 200 and r.json().get("data"):
        return r.json()["data"][0]["id"]
    return None

def get_user_profile(user_id):
    r = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
    return r.json() if r.status_code == 200 else None

def is_in_group(user_id):
    r = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    return r.status_code == 200 and any(g["group"]["id"] == GROUP_ID for g in r.json()["data"])

def set_rank(user_id):
    return patch_with_csrf(
        f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}",
        {"roleId": RANK_ID}
    ).status_code == 200

def rank_down(user_id):
    return patch_with_csrf(
        f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}",
        {"roleId": DEMOTE_RANK_ID}
    ).status_code == 200

# ---------------- EMBED ----------------
def embed(title, desc, color):
    e = discord.Embed(
        title=title,
        description=desc,
        color=color,
        timestamp=datetime.utcnow()
    )
    e.set_footer(text="Designed And Created By @fntsheetz")
    return e

async def send_log(guild, title, desc, color):
    ch = guild.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(embed=embed(title, desc, color))

async def send_dm(user, embed_msg):
    try:
        await user.send(embed=embed_msg)
    except:
        pass

# ---------------- TIME PARSER (NEW) ----------------
def parse_time(text: str):
    match = re.match(r"(\d+)(min|h|d)", text)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)

    if unit == "min":
        return value * 60
    if unit == "h":
        return value * 3600
    if unit == "d":
        return value * 86400
    return None

# ---------------- /turfapply (UNCHANGED) ----------------
@bot.slash_command(name="turfapply")
async def turfapply(ctx, username: str):
    await ctx.defer()
    member = ctx.author

    if not has_role(member, ALLOWED_ROLE_ID):
        return await ctx.respond(embed=embed("❌ Access Denied", "Missing role.", discord.Color.red()))

    if has_applied(member.id):
        return await ctx.respond(embed=embed("⚠️ Already Applied", "You already applied.", discord.Color.orange()))

    user_id = get_user_id(username)
    if not user_id:
        return await ctx.respond(embed=embed("❌ User Not Found", "Invalid Roblox username.", discord.Color.red()))

    profile = get_user_profile(user_id)
    if not profile:
        return await ctx.respond(embed=embed("❌ Error", "Could not fetch profile.", discord.Color.red()))

    if "fl13" not in profile.get("displayName", "").lower():
        return await ctx.respond(embed=embed("❌ Invalid Display Name", "Your Roblox display must contain 'fl13'.", discord.Color.red()))

    if not is_in_group(user_id):
        return await ctx.respond(embed=embed("❌ Not In Group", "You must be in the Roblox group.", discord.Color.red()))

    if set_rank(user_id):
        user_links[member.id] = user_id
        save_application(member.id, user_id)

        await ctx.respond(embed=embed("✅ Accepted", "You have been ranked!", discord.Color.green()))
        await send_log(ctx.guild, "🟢 APPLICATION", f"{member} → {username}", discord.Color.green())

        await send_dm(member, embed(
            "🎉 Welcome To The Turf",
            "You've been successfully ranked!",
            discord.Color.green()
        ))

# ---------------- /demote (UNCHANGED) ----------------
@bot.slash_command(name="demote")
async def demote(ctx, username: str, reason: str):
    await ctx.defer()

    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        return await ctx.respond(embed=embed("❌ No Permission", "Missing role.", discord.Color.red()))

    user_id = get_user_id(username)
    if not user_id:
        return await ctx.respond(embed=embed("❌ User Not Found", "Invalid username.", discord.Color.red()))

    if rank_down(user_id):
        await ctx.respond(embed=embed("📉 Demoted", username, discord.Color.orange()))
        await send_log(ctx.guild, "🔴 MANUAL DEMOTE",
            f"Admin: {admin}\nUser: {username}\nReason: {reason}",
            discord.Color.red()
        )

# ---------------- /tempdemote (UPDATED) ----------------
@bot.slash_command(name="tempdemote")
async def tempdemote(ctx, username: str, duration: str):
    await ctx.defer()

    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        return await ctx.respond(embed=embed("❌ No Permission", "Missing role.", discord.Color.red()))

    seconds = parse_time(duration)
    if not seconds:
        return await ctx.respond(embed=embed("❌ Invalid Time", "Use 1min / 1h / 1d", discord.Color.red()))

    user_id = get_user_id(username)
    if not user_id:
        return await ctx.respond(embed=embed("❌ User Not Found", "Invalid username.", discord.Color.red()))

    if rank_down(user_id):
        await ctx.respond(embed=embed("⏳ Temp Demoted", f"{username} for {duration}", discord.Color.orange()))

        await send_log(ctx.guild,
            "🟡 TEMP DEMOTE",
            f"Admin: {admin}\nUser: {username}\nDuration: {duration}",
            discord.Color.orange()
        )

        target_user = None
        for d, r in user_links.items():
            if r == user_id:
                target_user = await bot.fetch_user(d)
                break

        if target_user:
            await send_dm(target_user, embed(
                "⚠️ Temp Demoted",
                f"You have been temp demoted for {duration}.",
                discord.Color.orange()
            ))

        asyncio.create_task(temp_restore(ctx.guild, user_id, username, seconds))

async def temp_restore(guild, user_id, username, seconds):
    await asyncio.sleep(seconds)

    if set_rank(user_id):
        await send_log(guild,
            "🟢 TEMP RESTORED",
            f"{username} has been re-ranked after tempdemote",
            discord.Color.green()
        )

        for d, r in user_links.items():
            if r == user_id:
                user_obj = await bot.fetch_user(d)
                await send_dm(user_obj, embed(
                    "🎉 Temp Demote Ended",
                    "You have been re-ranked in The Turf.",
                    discord.Color.green()
                ))
                break

# ---------------- /reset (NEW) ----------------
@bot.slash_command(name="reset")
async def reset(ctx, member: discord.Member):
    await ctx.defer()

    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        return await ctx.respond(embed=embed("❌ No Permission", "Missing role.", discord.Color.red()))

    reset_application(member.id)
    user_links.pop(member.id, None)

    await ctx.respond(embed=embed("🔄 Reset Done", f"{member}", discord.Color.blue()))

    await send_log(ctx.guild,
        "🔵 RESET",
        f"Admin: {admin}\nTarget: {member}",
        discord.Color.blue()
    )

# ---------------- AUTO SYSTEM (UNCHANGED) ----------------
@bot.event
async def on_member_update(before, after):
    before_roles = [r.id for r in before.roles]
    after_roles = [r.id for r in after.roles]

    if ALLOWED_ROLE_ID in before_roles and ALLOWED_ROLE_ID not in after_roles:
        if after.id in user_links:
            rank_down(user_links[after.id])
            await send_log(after.guild, "🔴 AUTO DEMOTE", f"{after}", discord.Color.orange())

    if ALLOWED_ROLE_ID not in before_roles and ALLOWED_ROLE_ID in after_roles:
        if after.id in user_links:
            profile = get_user_profile(user_links[after.id])

            if profile and "fl13" in profile.get("displayName", "").lower():
                set_rank(user_links[after.id])

                await send_log(after.guild, "🟢 AUTO RE-RANK", f"{after}", discord.Color.green())

                user_obj = await bot.fetch_user(after.id)
                await send_dm(user_obj, embed(
                    "🎉 Re-Ranked",
                    "Access restored.",
                    discord.Color.green()
                ))

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

bot.run(DISCORD_TOKEN)
