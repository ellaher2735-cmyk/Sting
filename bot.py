import discord
from discord.ext import commands
from datetime import timedelta, timezone
import config
import json
import os
import random
import time
import asyncio
from collections import defaultdict

# --- File Setup ---
WARNINGS_FILE = "warnings.json"
ROLES_FILE    = "roles.json"
CHANNELS_FILE = "channels.json"
TOGGLES_FILE  = "antinuke_toggles.json"
WELCOME_FILE  = "welcome.json"

def load_warnings():
    if os.path.exists(WARNINGS_FILE):
        with open(WARNINGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_warnings(data):
    with open(WARNINGS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_roles():
    if os.path.exists(ROLES_FILE):
        with open(ROLES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_roles(data):
    with open(ROLES_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_channels(data):
    with open(CHANNELS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_toggles():
    if os.path.exists(TOGGLES_FILE):
        with open(TOGGLES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_toggles(data):
    with open(TOGGLES_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_welcome():
    if os.path.exists(WELCOME_FILE):
        with open(WELCOME_FILE, "r") as f:
            return json.load(f)
    return {}

def save_welcome(data):
    with open(WELCOME_FILE, "w") as f:
        json.dump(data, f, indent=4)

DEFAULT_TOGGLES = {
    "channels": True,
    "roles":    True,
    "bans":     True,
    "kicks":    True,
    "everyone": True,
    "botadd":   True,
    "webhooks": True,
}

def get_guild_toggles(guild_id: str) -> dict:
    guild_id = str(guild_id)
    if guild_id not in toggles_db:
        toggles_db[guild_id] = DEFAULT_TOGGLES.copy()
        save_toggles(toggles_db)
    changed = False
    for key, default in DEFAULT_TOGGLES.items():
        if key not in toggles_db[guild_id]:
            toggles_db[guild_id][key] = default
            changed = True
    if changed:
        save_toggles(toggles_db)
    return toggles_db[guild_id]

def is_enabled(guild_id, toggle_key: str) -> bool:
    return get_guild_toggles(str(guild_id)).get(toggle_key, True)


warnings_db = load_warnings()
roles_db    = load_roles()
channels_db = load_channels()
toggles_db  = load_toggles()
welcome_db  = load_welcome()

# FIX 4: AFK is now per-server → { guild_id: { user_id: reason } }
afk_db = {}

# --- Anti-Nuke Tracking ---
nuke_tracker     = defaultdict(list)
everyone_tracker = defaultdict(list)
webhook_tracker  = defaultdict(list)

NUKE_THRESHOLD     = 3
NUKE_TIMEFRAME     = 5
EVERYONE_THRESHOLD = 2
EVERYONE_TIMEFRAME = 10
WEBHOOK_THRESHOLD  = 3
WEBHOOK_TIMEFRAME  = 10

punished_users = set()

# FIX 3: SystemRandom for truly unbiased coin flips
_rng = random.SystemRandom()

# --- Bot Setup ---
intents = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.guild_messages  = True

bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def get_exact_level(ctx):
    """Returns 'admin', 'senior_mod', 'mod', 'trial_mod', or None."""
    if ctx.guild is None:
        return None
    if ctx.author.guild_permissions.administrator:
        return "admin"
    guild_id  = str(ctx.guild.id)
    if guild_id not in roles_db:
        return None
    roles_cfg = roles_db[guild_id]
    for level in ["admin", "senior_mod", "mod", "trial_mod"]:
        role_name = roles_cfg.get(level)
        if role_name:
            role = discord.utils.get(ctx.guild.roles, name=role_name)
            if role and role in ctx.author.roles:
                return level
    return None

def is_member_muted(member: discord.Member) -> bool:
    """Returns True if the member currently has an active timeout."""
    if member.timed_out_until is None:
        return False
    return member.timed_out_until > discord.utils.utcnow()


# ─────────────────────────────────────────
# CHANNEL SNAPSHOT SYSTEM
# ─────────────────────────────────────────

def snapshot_channels(guild):
    guild_id = str(guild.id)
    channels_db[guild_id] = {}
    for channel in guild.channels:
        overwrites = {}
        for target, overwrite in channel.overwrites.items():
            key = f"role_{target.id}" if isinstance(target, discord.Role) else f"member_{target.id}"
            allow, deny = overwrite.pair()
            overwrites[key] = {
                "type":  "role" if isinstance(target, discord.Role) else "member",
                "id":    target.id,
                "allow": allow.value,
                "deny":  deny.value,
            }
        channels_db[guild_id][str(channel.id)] = {
            "name":           channel.name,
            "type":           str(channel.type),
            "position":       channel.position,
            "category_id":    channel.category_id,
            "overwrites":     overwrites,
            "topic":          getattr(channel, "topic", None),
            "nsfw":           getattr(channel, "nsfw", False),
            "slowmode_delay": getattr(channel, "slowmode_delay", 0),
        }
    save_channels(channels_db)


async def restore_channel(guild, channel_data):
    try:
        overwrites = {}
        for key, data in channel_data["overwrites"].items():
            target = guild.get_role(data["id"]) if data["type"] == "role" else guild.get_member(data["id"])
            if target:
                allow = discord.Permissions(data["allow"])
                deny  = discord.Permissions(data["deny"])
                overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
        category = guild.get_channel(channel_data["category_id"]) if channel_data["category_id"] else None
        ch_type  = channel_data["type"]
        if "text" in ch_type:
            await guild.create_text_channel(
                name=channel_data["name"], overwrites=overwrites, category=category,
                topic=channel_data.get("topic"), nsfw=channel_data.get("nsfw", False),
                slowmode_delay=channel_data.get("slowmode_delay", 0),
                reason="Anti-Nuke: Channel recovery",
            )
        elif "voice" in ch_type:
            await guild.create_voice_channel(
                name=channel_data["name"], overwrites=overwrites, category=category,
                reason="Anti-Nuke: Channel recovery",
            )
        elif "category" in ch_type:
            await guild.create_category(
                name=channel_data["name"], overwrites=overwrites,
                reason="Anti-Nuke: Channel recovery",
            )
    except Exception as e:
        print(f"Failed to restore channel {channel_data['name']}: {e}")


# ─────────────────────────────────────────
# ANTI-NUKE CORE
# ─────────────────────────────────────────

async def handle_nuke_action(guild, user, action_type, deleted_channel_data=None):
    if user is None or user.bot:
        return
    if user.id == guild.owner_id:
        return
    if user.id in punished_users:
        if deleted_channel_data:
            await restore_channel(guild, deleted_channel_data)
        return
    now = time.time()
    nuke_tracker[user.id].append(now)
    nuke_tracker[user.id] = [t for t in nuke_tracker[user.id] if now - t <= NUKE_TIMEFRAME]
    if deleted_channel_data:
        await restore_channel(guild, deleted_channel_data)
    if len(nuke_tracker[user.id]) >= NUKE_THRESHOLD:
        punished_users.add(user.id)
        nuke_tracker[user.id] = []
        try:
            await guild.ban(user, reason=f"Anti-Nuke: Mass {action_type} detected")
        except Exception:
            pass
        try:
            embed = discord.Embed(title="🚨 Anti-Nuke Alert", description=f"A nuke attempt was detected and stopped in **{guild.name}**!", color=discord.Color.red())
            embed.add_field(name="👤 Attacker",         value=f"{user} ({user.id})", inline=False)
            embed.add_field(name="⚡ Action",           value=f"Mass {action_type}", inline=False)
            embed.add_field(name="⚖️ Punishment",       value="Banned",              inline=False)
            embed.add_field(name="🔄 Channel Recovery", value="Deleted channels have been restored", inline=False)
            embed.set_footer(text="Sting Bot Anti-Nuke System")
            await guild.owner.send(embed=embed)
        except Exception:
            pass
        await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=10))
        punished_users.discard(user.id)


# ─────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    for guild in bot.guilds:
        snapshot_channels(guild)
    print(f"📸 Snapshots saved for {len(bot.guilds)} server(s)")


@bot.event
async def on_guild_channel_delete(channel):
    if not is_enabled(channel.guild.id, "channels"):
        return
    guild        = channel.guild
    channel_data = channels_db.get(str(guild.id), {}).get(str(channel.id))
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete):
        if entry.target.id == channel.id:
            await handle_nuke_action(guild, entry.user, "channel deletion", channel_data)
            break


@bot.event
async def on_guild_channel_create(channel):
    if not is_enabled(channel.guild.id, "channels"):
        return
    guild = channel.guild
    snapshot_channels(guild)
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_create):
        if entry.target.id == channel.id:
            await handle_nuke_action(guild, entry.user, "channel creation")
            break


@bot.event
async def on_guild_channel_update(before, after):
    snapshot_channels(after.guild)


@bot.event
async def on_guild_role_delete(role):
    if not is_enabled(role.guild.id, "roles"):
        return
    guild = role.guild
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.role_delete):
        if entry.target.id == role.id:
            await handle_nuke_action(guild, entry.user, "role deletion")
            break


@bot.event
async def on_guild_role_create(role):
    if not is_enabled(role.guild.id, "roles"):
        return
    guild = role.guild
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.role_create):
        if entry.target.id == role.id:
            await handle_nuke_action(guild, entry.user, "role creation")
            break


@bot.event
async def on_member_ban(guild, user):
    if not is_enabled(guild.id, "bans"):
        return
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
        if entry.target.id == user.id:
            await handle_nuke_action(guild, entry.user, "mass ban")
            break


@bot.event
async def on_member_remove(member):
    if not is_enabled(member.guild.id, "kicks"):
        return
    guild = member.guild
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
        if entry.target.id == member.id:
            await handle_nuke_action(guild, entry.user, "mass kick")
            break


@bot.event
async def on_member_join(member):
    guild    = member.guild
    guild_id = str(guild.id)

    welcome = welcome_db.get(guild_id)
    if welcome and not member.bot:
        channel = guild.get_channel(int(welcome["channel_id"]))
        if channel:
            member_number = sum(1 for m in guild.members if not m.bot)
            embed = discord.Embed(
                title=f"👋 Welcome to {guild.name}!",
                description=welcome.get("message", f"Hey {member.mention}, welcome to **{guild.name}**!")
                    .replace("{user}", member.mention)
                    .replace("{server}", guild.name),
                color=discord.Color.green(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="👤 Member",          value=str(member),                             inline=True)
            embed.add_field(name="🆔 ID",              value=str(member.id),                          inline=True)
            embed.add_field(name="📅 Account Created", value=member.created_at.strftime("%d %b %Y"),  inline=True)
            embed.add_field(name="👥 Member Count",    value=f"You are member #{member_number}",       inline=False)
            embed.set_footer(text=f"{guild.name} • {guild.member_count} members total")
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass

    if not member.bot:
        return
    if not is_enabled(guild.id, "botadd"):
        return

    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
        if entry.target.id == member.id:
            inviter = entry.user
            if not inviter or inviter.id == guild.owner_id or inviter.bot:
                break
            bypass_role_name = roles_db.get(guild_id, {}).get("security_bypass")
            bypass_role      = discord.utils.get(guild.roles, name=bypass_role_name) if bypass_role_name else None
            has_bypass       = bypass_role is not None and bypass_role in inviter.roles
            if has_bypass:
                break
            try:
                await member.kick(reason="Anti-Nuke: Bot added by unauthorised user")
            except Exception:
                pass
            try:
                inviter_member = guild.get_member(inviter.id)
                if inviter_member:
                    await guild.ban(inviter_member, reason="Anti-Nuke: Unauthorised bot addition")
            except Exception:
                pass
            try:
                embed = discord.Embed(title="🚨 Unauthorised Bot Addition", description=f"An unauthorised bot was added to **{guild.name}**!", color=discord.Color.red())
                embed.add_field(name="🤖 Bot Added",    value=f"{member} ({member.id})",   inline=False)
                embed.add_field(name="👤 Added By",     value=f"{inviter} ({inviter.id})", inline=False)
                embed.add_field(name="⚖️ Action Taken", value="Bot kicked + user banned",  inline=False)
                embed.add_field(name="🛡️ Bypass Role",  value=bypass_role_name or "Not configured — use `?setbypass <role name>`", inline=False)
                embed.set_footer(text="Sting Bot Anti-Nuke System")
                await guild.owner.send(embed=embed)
            except Exception:
                pass
            break


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    guild_id = str(message.guild.id) if message.guild else None

    # FIX 4: AFK per-server
    if guild_id:
        guild_afk   = afk_db.get(guild_id, {})
        user_id_str = str(message.author.id)

        if user_id_str in guild_afk:
            del afk_db[guild_id][user_id_str]
            await message.channel.send(f"👋 Welcome back **{message.author.name}**! Your AFK has been removed.")

        for user in message.mentions:
            uid = str(user.id)
            if uid in guild_afk:
                await message.channel.send(f"💤 **{user.name}** is currently AFK: {guild_afk[uid]}")

    # @everyone protection
    if message.guild and message.mention_everyone and is_enabled(message.guild.id, "everyone"):
        user_id = message.author.id
        guild   = message.guild
        if user_id != guild.owner_id:
            now = time.time()
            everyone_tracker[user_id].append(now)
            everyone_tracker[user_id] = [t for t in everyone_tracker[user_id] if now - t <= EVERYONE_TIMEFRAME]
            if len(everyone_tracker[user_id]) >= EVERYONE_THRESHOLD:
                everyone_tracker[user_id] = []
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    await guild.ban(message.author, reason="Anti-Nuke: Multiple @everyone pings detected")
                except Exception:
                    pass
                kicked_bots = []
                async for entry in guild.audit_logs(limit=50, action=discord.AuditLogAction.bot_add):
                    if entry.user and entry.user.id == message.author.id:
                        bot_member = guild.get_member(entry.target.id) if hasattr(entry.target, "id") else None
                        if bot_member and bot_member.bot:
                            try:
                                await bot_member.kick(reason="Anti-Nuke: Bot invited by @everyone spammer")
                                kicked_bots.append(f"{bot_member} ({bot_member.id})")
                            except Exception:
                                pass
                try:
                    embed = discord.Embed(title="🚨 Mass @everyone Ping Alert", description=f"Multiple @everyone pings detected in **{guild.name}**!", color=discord.Color.red())
                    embed.add_field(name="👤 Attacker",   value=f"{message.author} ({message.author.id})", inline=False)
                    embed.add_field(name="📢 Pings",      value=f"{EVERYONE_THRESHOLD} pings in {EVERYONE_TIMEFRAME}s", inline=False)
                    embed.add_field(name="⚖️ Punishment", value="Banned + messages deleted", inline=False)
                    if kicked_bots:
                        embed.add_field(name="🤖 Bots Kicked", value="\n".join(kicked_bots), inline=False)
                    embed.set_footer(text="Sting Bot Anti-Nuke System")
                    await guild.owner.send(embed=embed)
                except Exception:
                    pass
            else:
                if not message.author.guild_permissions.administrator:
                    try:
                        await message.delete()
                        await message.channel.send(f"🚫 **{message.author.name}** you are not allowed to use @everyone.", delete_after=5)
                    except Exception:
                        pass

    await bot.process_commands(message)


# ─────────────────────────────────────────
# WEBHOOK PROTECTION
# ─────────────────────────────────────────

@bot.event
async def on_webhook_update(channel):
    if not is_enabled(channel.guild.id, "webhooks"):
        return
    guild    = channel.guild
    guild_id = str(guild.id)
    now      = time.time()
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.webhook_create):
        if not entry.user or entry.user.bot or entry.user.id == guild.owner_id:
            continue
        user_id = entry.user.id
        webhook_tracker[user_id].append(now)
        webhook_tracker[user_id] = [t for t in webhook_tracker[user_id] if now - t <= WEBHOOK_TIMEFRAME]
        if len(webhook_tracker[user_id]) >= WEBHOOK_THRESHOLD:
            webhook_tracker[user_id] = []
            try:
                webhook = await entry.target.fetch()
                await webhook.delete(reason="Anti-Raid: Mass webhook creation detected")
            except Exception:
                pass
            bypass_role_name = roles_db.get(guild_id, {}).get("security_bypass")
            bypass_role      = discord.utils.get(guild.roles, name=bypass_role_name) if bypass_role_name else None
            has_bypass       = bypass_role is not None and bypass_role in entry.user.roles
            if not has_bypass:
                try:
                    await guild.ban(entry.user, reason="Anti-Raid: Mass webhook creation")
                except Exception:
                    pass
            try:
                embed = discord.Embed(title="🚨 Mass Webhook Creation Detected", description=f"Mass webhook spam stopped in **{guild.name}**!", color=discord.Color.red())
                embed.add_field(name="👤 User",   value=f"{entry.user} ({entry.user.id})", inline=False)
                embed.add_field(name="⚖️ Action", value="Webhooks deleted + user banned",  inline=False)
                embed.set_footer(text="Sting Bot Anti-Raid Webhook Protection")
                await guild.owner.send(embed=embed)
            except Exception:
                pass
        break


# ─────────────────────────────────────────
# TOGGLE LABELS & ANTI-NUKE COMMANDS
# ─────────────────────────────────────────

TOGGLE_LABELS = {
    "channels": "Mass channel delete/create",
    "roles":    "Mass role delete/create",
    "bans":     "Mass ban",
    "kicks":    "Mass kick",
    "everyone": "Mass @everyone pings",
    "botadd":   "Unauthorised bot addition",
    "webhooks": "Mass webhook creation (Anti-Raid)",
}

@bot.command()
@commands.has_permissions(administrator=True)
async def antinuke(ctx, key: str = None):
    guild_id = str(ctx.guild.id)
    toggles  = get_guild_toggles(guild_id)
    if key is None:
        toggle_status = "\n".join(
            f"{'✅' if toggles.get(k, True) else '❌'} `{k}` — {label}"
            for k, label in TOGGLE_LABELS.items()
        )
        embed = discord.Embed(title="🛡️ Anti-Nuke Toggles", description=f"Use `?antinuke <key>` to toggle.\n\n{toggle_status}", color=discord.Color.orange())
        await ctx.send(embed=embed)
        return
    key = key.lower()
    if key == "all":
        all_on  = all(toggles.get(k, True) for k in TOGGLE_LABELS)
        new_val = not all_on
        for k in TOGGLE_LABELS:
            toggles_db[guild_id][k] = new_val
        save_toggles(toggles_db)
        state = "Enabled" if new_val else "Disabled"
        embed = discord.Embed(title="🛡️ Anti-Nuke — All Protections Updated", description=f"Every protection set to **{state}**.", color=discord.Color.green() if new_val else discord.Color.red())
        for k, label in TOGGLE_LABELS.items():
            embed.add_field(name=f"`{k}` — {label}", value=state, inline=False)
        await ctx.send(embed=embed)
        return
    if key not in TOGGLE_LABELS:
        await ctx.send(f"❌ Unknown key `{key}`. Valid: {', '.join(f'`{k}`' for k in TOGGLE_LABELS)}")
        return
    toggles[key] = not toggles.get(key, True)
    toggles_db[guild_id] = toggles
    save_toggles(toggles_db)
    state = "✅ Enabled" if toggles[key] else "❌ Disabled"
    await ctx.send(f"{state} **{TOGGLE_LABELS[key]}** protection.")


@bot.command()
@commands.has_permissions(administrator=True)
async def nukeinfo(ctx):
    toggles  = get_guild_toggles(ctx.guild.id)
    guild_id = str(ctx.guild.id)
    bypass   = roles_db.get(guild_id, {}).get("security_bypass", "Not set — use `?setbypass <role>`")
    embed = discord.Embed(title="🛡️ Anti-Nuke Settings", color=discord.Color.orange())
    embed.add_field(name="⚡ Nuke Trigger",      value=f"{NUKE_THRESHOLD} actions in {NUKE_TIMEFRAME}s",        inline=False)
    embed.add_field(name="📢 @everyone Trigger", value=f"{EVERYONE_THRESHOLD} pings in {EVERYONE_TIMEFRAME}s",  inline=False)
    embed.add_field(name="🌐 Webhook Trigger",   value=f"{WEBHOOK_THRESHOLD} webhooks in {WEBHOOK_TIMEFRAME}s", inline=False)
    embed.add_field(name="⚖️ Punishment",        value="Auto-ban + deletion",                                   inline=False)
    embed.add_field(name="🛡️ Security Bypass",   value=bypass,                                                  inline=False)
    toggle_status = "\n".join(f"{'✅' if toggles.get(k, True) else '❌'} {label}" for k, label in TOGGLE_LABELS.items())
    embed.add_field(name="🔘 Protections", value=toggle_status, inline=False)
    embed.set_footer(text="Sting Bot Anti-Nuke System")
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def snapshot(ctx):
    snapshot_channels(ctx.guild)
    await ctx.send(f"📸 Snapshot taken! **{len(ctx.guild.channels)}** channels saved.")


# ─────────────────────────────────────────
# ROLE SETUP COMMANDS
# ─────────────────────────────────────────

@bot.command()
@commands.has_permissions(administrator=True)
async def setrole(ctx, level: str, *, role_name: str):
    valid_levels = ["trial_mod", "mod", "senior_mod", "admin"]
    level = level.lower()
    if level not in valid_levels:
        await ctx.send("❌ Invalid level. Choose from: `trial_mod`, `mod`, `senior_mod`, `admin`")
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role **{role_name}** not found.")
        return
    guild_id = str(ctx.guild.id)
    if guild_id not in roles_db:
        roles_db[guild_id] = {}
    roles_db[guild_id][level] = role_name
    save_roles(roles_db)
    await ctx.send(f"✅ **{level.replace('_', ' ').title()}** set to role: **{role_name}**")


@bot.command()
@commands.has_permissions(administrator=True)
async def setbypass(ctx, *, role_name: str):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role **{role_name}** not found.")
        return
    guild_id = str(ctx.guild.id)
    if guild_id not in roles_db:
        roles_db[guild_id] = {}
    roles_db[guild_id]["security_bypass"] = role_name
    save_roles(roles_db)
    await ctx.send(f"✅ **Security Bypass** role set to: **{role_name}**\nOnly members with this role (or the server owner) can add bots.")


@bot.command()
@commands.has_permissions(administrator=True)
async def viewroles(ctx):
    guild_id    = str(ctx.guild.id)
    guild_roles = roles_db.get(guild_id, {})
    embed = discord.Embed(title="⚙️ Staff & Security Role Configuration", color=discord.Color.gold())
    hierarchy = {
        "trial_mod":       "🔰 Trial Mod",
        "mod":             "🛡️ Mod",
        "senior_mod":      "⭐ Senior Mod",
        "admin":           "👑 Admin",
        "security_bypass": "🔐 Security Bypass",
    }
    for key, label in hierarchy.items():
        embed.add_field(name=label, value=guild_roles.get(key, "❌ Not set"), inline=False)
    await ctx.send(embed=embed)


# ─────────────────────────────────────────
# ROLE MANAGEMENT
# ─────────────────────────────────────────

@bot.command()
@commands.has_permissions(administrator=True)
async def giverole(ctx, member: discord.Member, *, role_name: str):
    role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), ctx.guild.roles)
    if not role:
        role = discord.utils.find(lambda r: role_name.lower() in r.name.lower(), ctx.guild.roles)
    if not role:
        await ctx.send(f"❌ Role **{role_name}** not found.")
        return
    if role in member.roles:
        await ctx.send(f"❌ **{member.display_name}** already has **{role.name}**.")
        return
    try:
        await member.add_roles(role, reason=f"Given by {ctx.author}")
        await ctx.send(f"✅ **{role.name}** given to **{member.display_name}**.")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to assign that role.")


@bot.command()
@commands.has_permissions(administrator=True)
async def removerole(ctx, member: discord.Member, *, role_name: str):
    role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), ctx.guild.roles)
    if not role:
        role = discord.utils.find(lambda r: role_name.lower() in r.name.lower(), ctx.guild.roles)
    if not role:
        await ctx.send(f"❌ Role **{role_name}** not found.")
        return
    if role not in member.roles:
        await ctx.send(f"❌ **{member.display_name}** doesn't have **{role.name}**.")
        return
    try:
        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.send(f"✅ **{role.name}** removed from **{member.display_name}**.")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to remove that role.")


# ─────────────────────────────────────────
# MODERATION COMMANDS
# ─────────────────────────────────────────

@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if member == ctx.author:
        await ctx.send("❌ You cannot kick yourself.")
        return
    try:
        await member.kick(reason=reason)
        await ctx.send(f"👢 **{member}** has been kicked. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to kick that member.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if member == ctx.author:
        await ctx.send("❌ You cannot ban yourself.")
        return
    try:
        await member.ban(reason=reason)
        await ctx.send(f"🔨 **{member}** has been banned. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to ban that member.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, *, user_tag: str):
    banned_users = [entry async for entry in ctx.guild.bans()]
    for ban_entry in banned_users:
        if str(ban_entry.user) == user_tag or str(ban_entry.user.id) == user_tag:
            await ctx.guild.unban(ban_entry.user)
            await ctx.send(f"✅ **{ban_entry.user}** has been unbanned.")
            return
    await ctx.send(f"❌ User **{user_tag}** not found in the ban list.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 5):
    if amount < 1 or amount > 100:
        await ctx.send("❌ Please specify a number between 1 and 100.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🗑️ Deleted **{len(deleted) - 1}** messages.", delete_after=3)


# FIX 1 & 2: Can't mute yourself, checks if already muted
@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, duration: str = "10min", *, reason: str = "No reason provided"):
    if member == ctx.author:
        await ctx.send("❌ You cannot mute yourself.")
        return
    if is_member_muted(member):
        await ctx.send(f"❌ **{member.display_name}** is already muted.")
        return
    time_units = {"min": 1, "hr": 60, "d": 1440}
    amount = ""
    unit   = ""
    for char in duration:
        if char.isdigit():
            amount += char
        else:
            unit += char
    if not amount or unit not in time_units:
        await ctx.send("❌ Invalid format. Examples: `?mute @user 10min` / `?mute @user 2hr` / `?mute @user 1d`")
        return
    total_minutes = int(amount) * time_units[unit]
    if total_minutes > 40320:
        await ctx.send("❌ Max mute duration is 28 days.")
        return
    display = (
        f"{amount} day(s)"    if unit == "d"  else
        f"{amount} hour(s)"   if unit == "hr" else
        f"{amount} minute(s)"
    )
    try:
        await member.timeout(timedelta(minutes=total_minutes), reason=reason)
        await ctx.send(f"🔇 **{member}** muted for **{display}**. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to timeout that member.")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    if not is_member_muted(member):
        await ctx.send(f"❌ **{member.display_name}** is not currently muted.")
        return
    try:
        await member.timeout(None)
        await ctx.send(f"🔊 **{member}** has been unmuted.")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to unmute that member.")


@bot.command()
@commands.has_permissions(administrator=True)
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    guild_id = str(ctx.guild.id)
    user_id  = str(member.id)
    if guild_id not in warnings_db:
        warnings_db[guild_id] = {}
    if user_id not in warnings_db[guild_id]:
        warnings_db[guild_id][user_id] = []
    warnings_db[guild_id][user_id].append({"reason": reason, "by": str(ctx.author)})
    save_warnings(warnings_db)
    count = len(warnings_db[guild_id][user_id])
    await ctx.send(f"⚠️ **{member.display_name}** warned. Total warnings: **{count}**\nReason: {reason}")


@bot.command()
@commands.has_permissions(administrator=True)
async def warnings(ctx, member: discord.Member):
    guild_id      = str(ctx.guild.id)
    user_id       = str(member.id)
    user_warnings = warnings_db.get(guild_id, {}).get(user_id, [])
    if not user_warnings:
        await ctx.send(f"✅ **{member.display_name}** has no warnings.")
        return
    embed = discord.Embed(title=f"⚠️ Warnings for {member.display_name}", color=discord.Color.yellow())
    for i, w in enumerate(user_warnings, 1):
        embed.add_field(name=f"Warning {i}", value=f"**Reason:** {w['reason']}\n**By:** {w['by']}", inline=False)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def clearwarnings(ctx, member: discord.Member):
    guild_id = str(ctx.guild.id)
    user_id  = str(member.id)
    if guild_id in warnings_db and user_id in warnings_db[guild_id]:
        warnings_db[guild_id][user_id] = []
        save_warnings(warnings_db)
    await ctx.send(f"✅ Cleared all warnings for **{member.display_name}**.")


@bot.command()
@commands.has_permissions(administrator=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel **locked**.")


@bot.command()
@commands.has_permissions(administrator=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel **unlocked**.")


# NEW: Clean bot messages only
@bot.command()
@commands.has_permissions(manage_messages=True)
async def clean(ctx, amount: int = 50):
    if amount < 1 or amount > 200:
        await ctx.send("❌ Please specify a number between 1 and 200.")
        return
    deleted = await ctx.channel.purge(
        limit=amount,
        check=lambda m: m.author == bot.user
    )
    await ctx.send(f"🧹 Cleaned **{len(deleted)}** bot message(s).", delete_after=4)


# ─────────────────────────────────────────
# WELCOME SYSTEM
# ─────────────────────────────────────────

@bot.command()
@commands.has_permissions(administrator=True)
async def setwelcome(ctx, channel: discord.TextChannel = None, *, message: str = None):
    if channel is None:
        await ctx.send("❌ Please specify a channel.\nUsage: `?setwelcome #channel [custom message]`\nPlaceholders: `{user}` = member mention, `{server}` = server name")
        return
    guild_id = str(ctx.guild.id)
    welcome_db[guild_id] = {
        "channel_id": str(channel.id),
        "message":    message or "Hey {user}, welcome to **{server}**! We're glad to have you here. 🎉",
    }
    save_welcome(welcome_db)
    embed = discord.Embed(title="✅ Welcome System Configured", color=discord.Color.green())
    embed.add_field(name="📢 Channel", value=channel.mention,                 inline=False)
    embed.add_field(name="💬 Message", value=welcome_db[guild_id]["message"], inline=False)
    embed.set_footer(text="Use ?welcometest to preview  |  ?setwelcome to change")
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def welcometest(ctx):
    guild_id = str(ctx.guild.id)
    welcome  = welcome_db.get(guild_id)
    if not welcome:
        await ctx.send("❌ No welcome channel set. Use `?setwelcome #channel [message]` first.")
        return
    channel = ctx.guild.get_channel(int(welcome["channel_id"]))
    if not channel:
        await ctx.send("❌ The saved welcome channel no longer exists. Please run `?setwelcome` again.")
        return
    member        = ctx.author
    member_number = sum(1 for m in ctx.guild.members if not m.bot)
    embed = discord.Embed(
        title=f"👋 Welcome to {ctx.guild.name}!",
        description=welcome["message"].replace("{user}", member.mention).replace("{server}", ctx.guild.name),
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 Member",          value=str(member),                            inline=True)
    embed.add_field(name="🆔 ID",              value=str(member.id),                         inline=True)
    embed.add_field(name="📅 Account Created", value=member.created_at.strftime("%d %b %Y"), inline=True)
    embed.add_field(name="👥 Member Count",    value=f"You are member #{member_number}",      inline=False)
    embed.set_footer(text=f"{ctx.guild.name} • {ctx.guild.member_count} members total")
    await channel.send(embed=embed)
    await ctx.send(f"✅ Test welcome message sent to {channel.mention}!")


@bot.command()
@commands.has_permissions(administrator=True)
async def welcomeinfo(ctx):
    guild_id = str(ctx.guild.id)
    welcome  = welcome_db.get(guild_id)
    if not welcome:
        await ctx.send("❌ No welcome system configured. Use `?setwelcome #channel [message]`.")
        return
    channel = ctx.guild.get_channel(int(welcome["channel_id"]))
    embed = discord.Embed(title="👋 Welcome System Info", color=discord.Color.blurple())
    embed.add_field(name="📢 Channel",      value=channel.mention if channel else "❌ Channel deleted", inline=False)
    embed.add_field(name="💬 Message",      value=welcome["message"],                                   inline=False)
    embed.add_field(name="💡 Placeholders", value="`{user}` — member mention\n`{server}` — server name", inline=False)
    embed.set_footer(text="?setwelcome to change  |  ?welcometest to preview")
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def disablewelcome(ctx):
    guild_id = str(ctx.guild.id)
    if guild_id in welcome_db:
        del welcome_db[guild_id]
        save_welcome(welcome_db)
        await ctx.send("✅ Welcome system has been disabled.")
    else:
        await ctx.send("❌ Welcome system is not enabled.")


# ─────────────────────────────────────────
# UTILITY COMMANDS
# ─────────────────────────────────────────

@bot.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! Latency: **{round(bot.latency * 1000)}ms**")


@bot.command()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=f"📊 {guild.name}", color=discord.Color.blurple())
    embed.add_field(name="👥 Members",  value=guild.member_count,                    inline=True)
    embed.add_field(name="💬 Channels", value=len(guild.channels),                   inline=True)
    embed.add_field(name="🎭 Roles",    value=len(guild.roles),                      inline=True)
    embed.add_field(name="👑 Owner",    value=str(guild.owner),                      inline=True)
    embed.add_field(name="📅 Created",  value=guild.created_at.strftime("%d %b %Y"), inline=True)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await ctx.send(embed=embed)


@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed  = discord.Embed(title=f"👤 {member}", color=discord.Color.blue())
    embed.add_field(name="🆔 ID",              value=member.id,                               inline=True)
    embed.add_field(name="📅 Joined Server",   value=member.joined_at.strftime("%d %b %Y"),   inline=True)
    embed.add_field(name="📅 Account Created", value=member.created_at.strftime("%d %b %Y"),  inline=True)
    embed.add_field(name="🎭 Top Role",        value=member.top_role.mention,                 inline=True)
    embed.add_field(name="🔇 Muted",           value="Yes" if is_member_muted(member) else "No", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


# ─────────────────────────────────────────
# FUN COMMANDS
# ─────────────────────────────────────────

@bot.command()
async def flip(ctx):
    spinning_frames = [
        ("🪙", "Flipping..."),
        ("🌀", "Spinning..."),
        ("⚡", "Almost there..."),
        ("✨", "And the result is..."),
    ]
    embed = discord.Embed(title="🪙 Coin Flip", description="🪙 **Flipping...**", color=discord.Color.yellow())
    embed.set_footer(text=f"Flipped by {ctx.author.name}")
    msg = await ctx.send(embed=embed)
    for emoji, text in spinning_frames:
        await asyncio.sleep(0.6)
        embed.description = f"{emoji} **{text}**"
        await msg.edit(embed=embed)
    await asyncio.sleep(0.6)

    # FIX 3: SystemRandom for truly unbiased result
    result = "Heads" if _rng.randint(0, 1) == 0 else "Tails"

    if result == "Heads":
        embed = discord.Embed(title="🟡 HEADS!", description=f"{ctx.author.mention} flipped the coin and got **Heads**!", color=discord.Color.gold())
    else:
        embed = discord.Embed(title="⚫ TAILS!", description=f"{ctx.author.mention} flipped the coin and got **Tails**!", color=discord.Color.dark_grey())
    embed.set_footer(text=f"Flipped by {ctx.author.name}")
    await msg.edit(embed=embed)


# FIX 4: AFK is now per-server
@bot.command()
async def afk(ctx, *, reason: str = "AFK"):
    guild_id    = str(ctx.guild.id)
    user_id_str = str(ctx.author.id)
    if guild_id not in afk_db:
        afk_db[guild_id] = {}
    afk_db[guild_id][user_id_str] = reason
    await ctx.send(f"💤 **{ctx.author.name}** is now AFK in this server: {reason}")


# ─────────────────────────────────────────
# HELP COMMANDS  (3 clean menus)
# ─────────────────────────────────────────

@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🤖 Sting Bot",
        description=f"Hey **{ctx.author.name}**! Here's what you can do.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🎮 General Commands",
        value=(
            "`?ping` — Check bot latency\n"
            "`?flip` — Flip a coin\n"
            "`?afk [reason]` — Set AFK status (per server)\n"
            "`?serverinfo` — Show server info\n"
            "`?userinfo [@user]` — Show user info\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="📖 More Commands",
        value=(
            "`?modhelp` — Moderation commands (staff only)\n"
            "`?setuphelp` — Setup & config commands (admin only)\n"
        ),
        inline=False,
    )
    embed.set_footer(text="[ ] = optional  |  < > = required  |  Sting Bot")
    await ctx.send(embed=embed)


@bot.command()
async def modhelp(ctx):
    level = get_exact_level(ctx)
    if level is None:
        await ctx.send("❌ You need a staff role to view moderation commands. Ask an admin to run `?setrole`.")
        return

    embed = discord.Embed(
        title="🛡️ Sting Bot — Moderation Commands",
        description=f"Hey **{ctx.author.name}**! Here are your mod tools.",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="🔰 Trial Mod",
        value=(
            "`?warn @user [reason]` — Warn a member\n"
            "`?warnings @user` — View warnings\n"
            "`?clearwarnings @user` — Clear warnings\n"
        ),
        inline=False,
    )
    if level in ("mod", "senior_mod", "admin"):
        embed.add_field(
            name="🛡️ Mod",
            value=(
                "`?mute @user <duration> [reason]` — Mute (e.g. `10min` / `2hr` / `1d`)\n"
                "`?unmute @user` — Unmute a member\n"
                "`?kick @user [reason]` — Kick a member\n"
                "`?clean [amount]` — Delete bot messages in channel\n"
            ),
            inline=False,
        )
    if level in ("senior_mod", "admin"):
        embed.add_field(
            name="⭐ Senior Mod",
            value=(
                "`?ban @user [reason]` — Ban a member\n"
                "`?unban <id or user#tag>` — Unban a user\n"
                "`?clear [amount]` — Bulk delete messages (max 100)\n"
                "`?lock` — Lock the current channel\n"
                "`?unlock` — Unlock the current channel\n"
            ),
            inline=False,
        )
    if level == "admin":
        embed.add_field(
            name="👑 Admin",
            value=(
                "`?giverole @user <role>` — Give a role\n"
                "`?removerole @user <role>` — Remove a role\n"
            ),
            inline=False,
        )
    embed.set_footer(text="[ ] = optional  |  < > = required  |  Sting Bot")
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setuphelp(ctx):
    embed = discord.Embed(
        title="⚙️ Sting Bot — Setup Commands",
        description="All commands for configuring Sting Bot in this server.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="👥 Staff Roles",
        value=(
            "`?setrole <level> <role>` — Assign a staff role\n"
            "  Levels: `trial_mod` `mod` `senior_mod` `admin`\n"
            "`?setbypass <role>` — Set the bot-add bypass role\n"
            "`?viewroles` — View all current role assignments\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Anti-Nuke",
        value=(
            "`?nukeinfo` — View all anti-nuke settings & toggles\n"
            "`?antinuke` — List all protection toggles\n"
            "`?antinuke <key>` — Toggle a specific protection on/off\n"
            "`?antinuke all` — Toggle all protections at once\n"
            "`?snapshot` — Manually save channel snapshot\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="👋 Welcome System",
        value=(
            "`?setwelcome #channel [msg]` — Set welcome channel & message\n"
            "  Placeholders: `{user}` = mention, `{server}` = server name\n"
            "`?welcometest` — Preview the welcome message\n"
            "`?welcomeinfo` — View current welcome config\n"
            "`?disablewelcome` — Disable welcome messages\n"
        ),
        inline=False,
    )
    embed.set_footer(text="[ ] = optional  |  < > = required  |  Sting Bot")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────
# ERROR HANDLING
# ─────────────────────────────────────────

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found. Try mentioning them or using their ID.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Bad argument: {error}")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"Unhandled error in {ctx.command}: {error}")
        raise error


# --- Run the bot ---
bot.run(config.TOKEN)
