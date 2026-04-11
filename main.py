import discord
from discord.ext import commands
import os
import requests
import psycopg2

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

applied_users = set()
user_links = {}

roblox_headers = {
    'Content-Type': 'application/json',
    'Cookie': f'.ROBLOSECURITY={ROBLOX_COOKIE}'
}

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
                "INSERT INTO applications (discord_id, roblox_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (discord_id, roblox_id)
            )
            conn.commit()

def reset_application(discord_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM applications WHERE discord_id = %s", (discord_id,))
            conn.commit()

def patch_with_csrf(url, json_data):
    headers = roblox_headers.copy()
    response = requests.patch(url, headers=headers, json=json_data)

    if response.status_code == 403:
        token = response.headers.get('x-csrf-token')
        if token:
            headers['X-CSRF-TOKEN'] = token
            response = requests.patch(url, headers=headers, json=json_data)

    return response

def get_user_id(username):
    response = requests.post("https://users.roblox.com/v1/usernames/users", json={
        "usernames": [username],
        "excludeBannedUsers": True
    })
    if response.status_code == 200 and response.json()["data"]:
        return response.json()["data"][0]["id"]
    return None

def get_user_profile(user_id):
    response = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
    if response.status_code == 200:
        return response.json()
    return None

def is_in_group(user_id):
    response = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    if response.status_code == 200:
        for group in response.json()["data"]:
            if group["group"]["id"] == GROUP_ID:
                return True
    return False

def set_rank(user_id):
    url = f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}"
    return patch_with_csrf(url, {"roleId": RANK_ID}).status_code == 200

def rank_down(user_id):
    url = f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}"
    return patch_with_csrf(url, {"roleId": DEMOTE_RANK_ID}).status_code == 200

# LOG SYSTEM
async def send_log(guild, embed):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)

def add_footer(embed):
    embed.set_footer(text="Designed And Created By Murda")
    return embed


# /turfapply
@bot.slash_command(name="turfapply")
async def turfapply(ctx, username: str):

    member = ctx.author

    if ALLOWED_ROLE_ID not in [r.id for r in member.roles]:
        await ctx.respond(embed=add_footer(discord.Embed(
            title="❌ Access Denied",
            description="You don't have the required role.",
            color=discord.Color.red()
        )))
        return

    if has_applied(member.id):
        await ctx.respond(embed=add_footer(discord.Embed(
            title="⚠️ Already Applied",
            description="You have already applied.",
            color=discord.Color.orange()
        )))
        return

    user_id = get_user_id(username)
    profile = get_user_profile(user_id)

    if not user_id or not profile:
        await ctx.respond(embed=add_footer(discord.Embed(
            title="❌ Error",
            description="Failed to fetch Roblox user.",
            color=discord.Color.red()
        )))
        return

    if "fl13" not in profile.get("displayName", "").lower():
        await ctx.respond(embed=add_footer(discord.Embed(
            title="❌ Invalid Display Name",
            description="Must contain 'fl13'.",
            color=discord.Color.red()
        )))
        return

    if not is_in_group(user_id):
        await ctx.respond(embed=add_footer(discord.Embed(
            title="❌ Not in Group",
            color=discord.Color.red()
        )))
        return

    if set_rank(user_id):
        user_links[member.id] = user_id
        save_application(member.id, user_id)

        embed = add_footer(discord.Embed(
            title="✅ Application Approved",
            description=f"{member.mention} → **{RANK_NAME}**",
            color=discord.Color.green()
        ))
        await ctx.respond(embed=embed)

        await send_log(ctx.guild, add_footer(discord.Embed(
            title="📈 PROMOTION",
            description=f"**Discord:** {member}\n**Roblox:** {username} ({user_id})\n**Rank:** {RANK_NAME}",
            color=discord.Color.blue()
        )))


# /demote
@bot.slash_command(name="demote")
async def demote(ctx, username: str):

    admin = ctx.author

    if DEMOTE_ROLE_ID not in [r.id for r in admin.roles]:
        await ctx.respond(embed=add_footer(discord.Embed(
            title="❌ No Permission",
            color=discord.Color.red()
        )))
        return

    user_id = get_user_id(username)

    if not user_id:
        await ctx.respond(embed=add_footer(discord.Embed(
            title="❌ User Not Found",
            color=discord.Color.red()
        )))
        return

    if rank_down(user_id):
        embed = add_footer(discord.Embed(
            title="📉 User Demoted",
            description=f"{username} has been demoted.",
            color=discord.Color.orange()
        ))
        await ctx.respond(embed=embed)

        await send_log(ctx.guild, add_footer(discord.Embed(
            title="📉 MANUAL DEMOTE",
            description=f"**Admin:** {admin}\n**Roblox:** {username} ({user_id})",
            color=discord.Color.red()
        )))


# /reset
@bot.slash_command(name="reset")
async def reset(ctx, member: discord.Member):

    admin = ctx.author

    if ALLOWED_ROLE_ID not in [r.id for r in admin.roles]:
        await ctx.respond(embed=add_footer(discord.Embed(
            title="❌ No Permission",
            color=discord.Color.red()
        )))
        return

    reset_application(member.id)
    user_links.pop(member.id, None)

    embed = add_footer(discord.Embed(
        title="🔄 Application Reset",
        description=f"{member.mention} can now apply again.",
        color=discord.Color.blue()
    ))
    await ctx.respond(embed=embed)

    await send_log(ctx.guild, add_footer(discord.Embed(
        title="🔄 RESET",
        description=f"**Admin:** {admin}\n**User:** {member}",
        color=discord.Color.purple()
    )))


# AUTO DEMOTE
@bot.event
async def on_member_update(before, after):

    lost_role = ALLOWED_ROLE_ID in [r.id for r in before.roles] and ALLOWED_ROLE_ID not in [r.id for r in after.roles]

    if lost_role and after.id in user_links:
        roblox_id = user_links[after.id]
        rank_down(roblox_id)

        await send_log(after.guild, add_footer(discord.Embed(
            title="📉 AUTO DEMOTE",
            description=f"**User:** {after}\n**Roblox ID:** {roblox_id}",
            color=discord.Color.orange()
        )))


@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

bot.run(DISCORD_TOKEN)
