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
RANK_NAME = "Full Access"  # <-- rankens namn istället för bara ID
DATABASE_URL = os.getenv("DATABASE_URL")  # <-- lägg till denna i Railway

# Temporary in-memory data
applied_users = set()
user_links = {}

# Roblox API headers (without X-CSRF-TOKEN initially)
roblox_headers = {
    'Content-Type': 'application/json',
    'Cookie': f'.ROBLOSECURITY={ROBLOX_COOKIE}'
}

# PostgreSQL-funktioner
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

# Helper function to make PATCH requests with CSRF token handling
def patch_with_csrf(url, json_data):
    headers = roblox_headers.copy()
    # First PATCH attempt without token
    response = requests.patch(url, headers=headers, json=json_data)
    if response.status_code == 403:
        # Get token from response headers
        token = response.headers.get('x-csrf-token')
        if token:
            headers['X-CSRF-TOKEN'] = token
            # Retry PATCH with token
            response = requests.patch(url, headers=headers, json=json_data)
    return response

# Get Roblox user ID from username
def get_user_id(username):
    response = requests.post("https://users.roblox.com/v1/usernames/users", json={
        "usernames": [username],
        "excludeBannedUsers": True
    })
    if response.status_code == 200 and response.json()["data"]:
        return response.json()["data"][0]["id"]
    return None

# Get Roblox user profile to check display name
def get_user_profile(user_id):
    response = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
    if response.status_code == 200:
        return response.json()
    return None

# Check if user is in the group
def is_in_group(user_id):
    response = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    if response.status_code == 200:
        for group in response.json()["data"]:
            if group["group"]["id"] == GROUP_ID:
                return True
    return False

# Set rank with debug print and global response
response = None
def set_rank(user_id):
    global response
    url = f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}"
    json_data = {"roleId": RANK_ID}
    response = patch_with_csrf(url, json_data)
    print(f"[DEBUG] set_rank response: {response.status_code} - {response.text}")
    return response.status_code == 200

# Demote to rank 1 with debug print
def rank_down(user_id):
    url = f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}"
    json_data = {"roleId": 1}
    response = patch_with_csrf(url, json_data)
    print(f"[DEBUG] rank_down response: {response.status_code} - {response.text}")
    return response.status_code == 200

# Slash command: /turfapply
@bot.slash_command(name="turfapply", description="Apply for Turf by verifying your Roblox username.")
async def turfapply(ctx: discord.ApplicationContext, username: str):
    member = ctx.author

    # Role check
    if ALLOWED_ROLE_ID not in [role.id for role in member.roles]:
        embed = discord.Embed(
            title="Access Denied",
            description="You must have the required role to use this command.",
            color=discord.Color.red()
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    # Already applied check (med databas)
    if has_applied(member.id):
        embed = discord.Embed(
            title="Already Applied",
            description="You have already submitted your Turf application.",
            color=discord.Color.orange()
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    # Get Roblox user ID
    user_id = get_user_id(username)
    if not user_id:
        embed = discord.Embed(
            title="User Not Found",
            description=f"Could not find a Roblox user with the username `{username}`.",
            color=discord.Color.red()
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    # Get user profile to check display name
    profile = get_user_profile(user_id)
    if not profile:
        embed = discord.Embed(
            title="Error",
            description="Could not fetch Roblox user profile. Please try again later.",
            color=discord.Color.red()
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    display_name = profile.get("displayName", "").lower()
    if "fl13" not in display_name:
        embed = discord.Embed(
            title="Invalid Display Name",
            description="Your Roblox **display name** must contain **'fl13'** to apply.",
            color=discord.Color.red()
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    # Check if in group
    if not is_in_group(user_id):
        embed = discord.Embed(
            title="Not in Group",
            description="You must already be a member of the Roblox group to apply.",
            color=discord.Color.red()
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    # Set rank
    if set_rank(user_id):
        applied_users.add(member.id)
        user_links[member.id] = user_id
        save_application(member.id, user_id)  # <-- Lägg till i databasen

        embed = discord.Embed(
            title="Application Approved ✅",
            description=f"You have been ranked to **{RANK_NAME}** in the Roblox group.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Welcome to Turf.")
        await ctx.respond(embed=embed, ephemeral=False)
    else:
        embed = discord.Embed(
            title="Error",
            description=f"Something went wrong while trying to rank you.\n"
                        f"Status code: {response.status_code}\n"
                        f"Response: {response.text}",
            color=discord.Color.red()
        )
        await ctx.respond(embed=embed, ephemeral=True)

# Role removal = auto demotion
@bot.event
async def on_member_update(before, after):
    lost_role = ALLOWED_ROLE_ID in [r.id for r in before.roles] and ALLOWED_ROLE_ID not in [r.id for r in after.roles]
    if lost_role:
        discord_id = after.id
        if discord_id in user_links:
            roblox_id = user_links[discord_id]
            if rank_down(roblox_id):
                print(f"[INFO] {after} was auto-demoted in Roblox due to lost role.")

# Bot ready event
@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")

# Run the bot
bot.run(DISCORD_TOKEN)
