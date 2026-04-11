import discord
from discord.ext import commands
import os
import requests
import psycopg2
from datetime import datetime

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

# ---------------- ROLE CHECK ----------------
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
    try:
        r = requests.post("https://users.roblox.com/v1/usernames/users", json={
            "usernames": [username],
            "excludeBannedUsers": True
        })
        if r.status_code == 200 and r.json().get("data"):
            return r.json()["data"][0]["id"]
    except:
        pass
    return None

def get_user_profile(user_id):
    r = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
    return r.json() if r.status_code == 200 else None

def is_in_group(user_id):
    r = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    if r.status_code == 200:
        return any(g["group"]["id"] == GROUP_ID for g in r.json()["data"])
    return False

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

# ---------------- EMBEDS ----------------
def embed(title, desc, color):
    e = discord.Embed(
        title=title,
        description=desc,
        color=color,
        timestamp=datetime.utcnow()
    )
    e.set_footer(text="Designed And Created By @fntsheetz")
    return e

async def send_log(guild, e):
    ch = guild.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(embed=e)

async def send_dm(user, e):
    try:
        await user.send(embed=e)
    except:
        pass

# ---------------- /turfapply ----------------
@bot.slash_command(name="turfapply")
async def turfapply(ctx, username: str):

    member = ctx.author

    if not has_role(member, ALLOWED_ROLE_ID):
        return await ctx.respond(embed=embed("❌ Access Denied", "Missing role", discord.Color.red()))

    if has_applied(member.id):
        return await ctx.respond(embed=embed("⚠️ Already Applied", "", discord.Color.orange()))

    user_id = get_user_id(username)
    profile = get_user_profile(user_id)

    if not user_id or not profile:
        return await ctx.respond(embed=embed("❌ Error", "User not found", discord.Color.red()))

    if "fl13" not in profile.get("displayName", "").lower():
        return await ctx.respond(embed=embed("❌ Invalid Name", "", discord.Color.red()))

    if not is_in_group(user_id):
        return await ctx.respond(embed=embed("❌ Not In Group", "", discord.Color.red()))

    if set_rank(user_id):
        user_links[member.id] = user_id
        save_application(member.id, user_id)

        await ctx.respond(embed=embed("✅ Accepted", f"{member.mention}", discord.Color.green()))

        await send_dm(member, embed(
            "🎉 Welcome To The Turf",
            "You've been successfully ranked!",
            discord.Color.green()
        ))

# ---------------- /demote ----------------
@bot.slash_command(name="demote")
async def demote(ctx, username: str, reason: str):

    admin = ctx.author

    if not has_role(admin, DEMOTE_ROLE_ID):
        return await ctx.respond(embed=embed("❌ No Permission", "", discord.Color.red()))

    user_id = get_user_id(username)

    if not user_id:
        return await ctx.respond(embed=embed("❌ Username Not Found", "", discord.Color.red()))

    if rank_down(user_id):

        await ctx.respond(embed=embed("📉 Demoted", username, discord.Color.orange()))

        discord_user = None
        for k, v in user_links.items():
            if v == user_id:
                discord_user = ctx.guild.get_member(k)

        if discord_user:
            await send_dm(discord_user, embed(
                "📉 You Have Been Demoted",
                f"Reason: {reason}",
                discord.Color.red()
            ))

        await send_log(ctx.guild, embed(
            "📉 DEMOTE LOG",
            f"Admin: {admin}\nUser: {username}\nReason: {reason}",
            discord.Color.red()
        ))

# ---------------- /reset (FIXED) ----------------
@bot.slash_command(name="reset")
async def reset(ctx, member: discord.Member):

    admin = ctx.author

    # ONLY DEMOTE ROLE CAN USE RESET
    if not has_role(admin, DEMOTE_ROLE_ID):
        return await ctx.respond(embed=embed("❌ No Permission", "", discord.Color.red()))

    reset_application(member.id)
    user_links.pop(member.id, None)

    await ctx.respond(embed=embed("🔄 Reset Done", member.mention, discord.Color.blue()))

    await send_log(ctx.guild, embed(
        "🔄 RESET LOG",
        f"Admin: {admin}\nUser: {member}",
        discord.Color.purple()
    ))

# ---------------- AUTO DEMOTE ----------------
@bot.event
async def on_member_update(before, after):

    if ALLOWED_ROLE_ID in [r.id for r in before.roles] and ALLOWED_ROLE_ID not in [r.id for r in after.roles]:
        if after.id in user_links:
            rank_down(user_links[after.id])

            await send_log(after.guild, embed(
                "📉 AUTO DEMOTE",
                f"User: {after}",
                discord.Color.orange()
            ))

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

bot.run(DISCORD_TOKEN)
