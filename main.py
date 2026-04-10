import discord
from discord.ext import commands
import os
import requests
import psycopg2  # PostgreSQL-stöd

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Load environment variables (from Railway secrets)
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_ROLE_ID = int(os.getenv("ALLOWED_ROLE_ID"))
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")
GROUP_ID = int(os.getenv("GROUP_ID"))
RANK_ID = int(os.getenv("RANK_1"))
RANK_NAME = "Full Access"
DATABASE_URL = os.getenv("DATABASE_URL")

# NEW 🔥
DEMOTE_ROLE_ID = int(os.getenv("DEMOTE_ROLE_ID"))

# Temporary in-memory data
applied_users = set()
user_links = {}

# Roblox API headers
roblox_headers = {
    'Content-Type': 'application/json',
    'Cookie': f'.ROBLOSECURITY={ROBLOX_COOKIE}'
}

# PostgreSQL
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
    json_data = {"roleId": RANK_ID}
    response = patch_with_csrf(url, json_data)
    print(f"[DEBUG] set_rank response: {response.status_code} - {response.text}")
    return response.status_code == 200

def rank_down(user_id):
    url = f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}"
    json_data = {"roleId": 1}
    response = patch_with_csrf(url, json_data)
    print(f"[DEBUG] rank_down response: {response.status_code} - {response.text}")
    return response.status_code == 200

# /turfapply
@bot.slash_command(name="turfapply", description="Apply for Turf by verifying your Roblox username.")
async def turfapply(ctx, username: str):

    member = ctx.author

    if ALLOWED_ROLE_ID not in [role.id for role in member.roles]:
        await ctx.respond("Access Denied", ephemeral=True)
        return

    if has_applied(member.id):
        await ctx.respond("Already applied", ephemeral=True)
        return

    user_id = get_user_id(username)
    if not user_id:
        await ctx.respond("User not found", ephemeral=True)
        return

    profile = get_user_profile(user_id)
    if not profile:
        await ctx.respond("Error", ephemeral=True)
        return

    if "fl13" not in profile.get("displayName", "").lower():
        await ctx.respond("Invalid display name", ephemeral=True)
        return

    if not is_in_group(user_id):
        await ctx.respond("Not in group", ephemeral=True)
        return

    if set_rank(user_id):
        applied_users.add(member.id)
        user_links[member.id] = user_id
        save_application(member.id, user_id)

        await ctx.respond(f"Ranked to {RANK_NAME}")

    else:
        await ctx.respond("Error ranking user", ephemeral=True)


# ROLE LOSS → AUTO DEMOTE
@bot.event
async def on_member_update(before, after):

    lost_role = ALLOWED_ROLE_ID in [r.id for r in before.roles] and ALLOWED_ROLE_ID not in [r.id for r in after.roles]

    if lost_role:
        discord_id = after.id

        if discord_id in user_links:
            roblox_id = user_links[discord_id]

            if rank_down(roblox_id):
                print(f"{after} auto demoted")


# NEW 🔥 /demote command
@bot.slash_command(name="demote", description="Manually demote a user in Roblox group")
async def demote(ctx, username: str):

    member = ctx.author

    if DEMOTE_ROLE_ID not in [role.id for role in member.roles]:
        await ctx.respond("No permission", ephemeral=True)
        return

    user_id = get_user_id(username)

    if not user_id:
        await ctx.respond("User not found", ephemeral=True)
        return

    if rank_down(user_id):
        await ctx.respond(f"{username} demoted")

    else:
        await ctx.respond("Failed to demote", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")


bot.run(DISCORD_TOKEN)
