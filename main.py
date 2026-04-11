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

# DB
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
            cur.execute("INSERT INTO applications (discord_id, roblox_id) VALUES (%s, %s)", (discord_id, roblox_id))
            conn.commit()

def reset_application(discord_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM applications WHERE discord_id = %s", (discord_id,))
            conn.commit()

# Roblox API
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
    r = requests.post("https://users.roblox.com/v1/usernames/users", json={
        "usernames": [username],
        "excludeBannedUsers": True
    })
    if r.status_code == 200 and r.json()["data"]:
        return r.json()["data"][0]["id"]
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

# Utils
def embed_base(title, desc, color):
    e = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.utcnow())
    e.set_footer(text="Designed And Created By Murda")
    return e

async def send_log(guild, embed):
    ch = guild.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(embed=embed)

async def send_dm(user, embed):
    try:
        await user.send(embed=embed)
    except:
        pass

# /turfapply
@bot.slash_command(name="turfapply")
async def turfapply(ctx, username: str):

    member = ctx.author

    if ALLOWED_ROLE_ID not in [r.id for r in member.roles]:
        await ctx.respond(embed=embed_base("❌ Access Denied", "Missing required role.", discord.Color.red()))
        return

    if has_applied(member.id):
        await ctx.respond(embed=embed_base("⚠️ Already Applied", "You already applied.", discord.Color.orange()))
        return

    user_id = get_user_id(username)
    profile = get_user_profile(user_id)

    if not user_id or not profile:
        await ctx.respond(embed=embed_base("❌ Error", "Roblox user not found.", discord.Color.red()))
        return

    if "fl13" not in profile.get("displayName", "").lower():
        await ctx.respond(embed=embed_base("❌ Invalid Display Name", "Must contain 'fl13'.", discord.Color.red()))
        return

    if not is_in_group(user_id):
        await ctx.respond(embed=embed_base("❌ Not in Group", "Join group first.", discord.Color.red()))
        return

    if set_rank(user_id):
        user_links[member.id] = user_id
        save_application(member.id, user_id)

        await ctx.respond(embed=embed_base("✅ Accepted", f"{member.mention} ranked to {RANK_NAME}", discord.Color.green()))

        # DM
        await send_dm(member, embed_base(
            "🎉 Welcome to Turf",
            f"You've been successfully ranked to **{RANK_NAME}**!",
            discord.Color.green()
        ))

        # LOG
        await send_log(ctx.guild, embed_base(
            "📈 PROMOTION LOG",
            f"""
**User:** {member} ({member.id})
**Roblox:** {username} ({user_id})
**Rank:** {RANK_NAME}
**Time:** {datetime.utcnow()}
""",
            discord.Color.blue()
        ))


# /demote
@bot.slash_command(name="demote")
async def demote(ctx, username: str, reason: str):

    admin = ctx.author

    if DEMOTE_ROLE_ID not in [r.id for r in admin.roles]:
        await ctx.respond(embed=embed_base("❌ No Permission", "Missing role.", discord.Color.red()))
        return

    user_id = get_user_id(username)

    if not user_id:
        await ctx.respond(embed=embed_base("❌ Not Found", "Roblox user not found.", discord.Color.red()))
        return

    if rank_down(user_id):

        await ctx.respond(embed=embed_base("📉 Demoted", f"{username} has been demoted.", discord.Color.orange()))

        # FIND DISCORD USER
        discord_user = None
        for k, v in user_links.items():
            if v == user_id:
                discord_user = ctx.guild.get_member(k)

        # DM
        if discord_user:
            await send_dm(discord_user, embed_base(
                "📉 You have been demoted",
                f"You were demoted in **Turf**.\n\n**Reason:** {reason}",
                discord.Color.red()
            ))

        # LOG
        await send_log(ctx.guild, embed_base(
            "📉 DEMOTE LOG",
            f"""
**Admin:** {admin} ({admin.id})
**User:** {username}
**Roblox ID:** {user_id}
**Reason:** {reason}
**Time:** {datetime.utcnow()}
""",
            discord.Color.red()
        ))


# /reset
@bot.slash_command(name="reset")
async def reset(ctx, member: discord.Member):

    admin = ctx.author

    if ALLOWED_ROLE_ID not in [r.id for r in admin.roles]:
        await ctx.respond(embed=embed_base("❌ No Permission", "", discord.Color.red()))
        return

    reset_application(member.id)
    user_links.pop(member.id, None)

    await ctx.respond(embed=embed_base("🔄 Reset Done", f"{member.mention} can apply again.", discord.Color.blue()))

    await send_log(ctx.guild, embed_base(
        "🔄 RESET LOG",
        f"""
**Admin:** {admin}
**User:** {member}
**Time:** {datetime.utcnow()}
""",
        discord.Color.purple()
    ))


# AUTO DEMOTE
@bot.event
async def on_member_update(before, after):

    if ALLOWED_ROLE_ID in [r.id for r in before.roles] and ALLOWED_ROLE_ID not in [r.id for r in after.roles]:
        if after.id in user_links:
            roblox_id = user_links[after.id]
            rank_down(roblox_id)

            await send_log(after.guild, embed_base(
                "📉 AUTO DEMOTE",
                f"**User:** {after}\n**Roblox ID:** {roblox_id}\nLost role",
                discord.Color.orange()
            ))


@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

bot.run(DISCORD_TOKEN)
