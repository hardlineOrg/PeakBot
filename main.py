import os
import re
import asyncio
from datetime import datetime, timedelta, timezone
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "peakbot")
PORT = int(os.getenv("PORT", 10000))

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en las variables de entorno")
if not MONGODB_URI:
    raise RuntimeError("Falta MONGODB_URI en las variables de entorno")

# ─────────────────────────────────────────────
# MongoDB
# ─────────────────────────────────────────────
client_db = MongoClient(MONGODB_URI)
db = client_db[DATABASE_NAME]
config_col = db["guild_config"]
temproles_col = db["temproles"]

# ─────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


def get_guild_config(guild_id: int) -> dict:
    doc = config_col.find_one({"guild_id": guild_id})
    if not doc:
        doc = {
            "guild_id": guild_id,
            "welcome_message": None,
            "welcome_channel": None,
            "autorole": None,
        }
        config_col.insert_one(doc)
    return doc


def update_guild_config(guild_id: int, **kwargs):
    config_col.update_one(
        {"guild_id": guild_id},
        {"$set": kwargs},
        upsert=True,
    )


def parse_duration(tiempo: str) -> int | None:
    """Convierte '10m', '2h', '30s' a segundos. Devuelve None si es inválido."""
    match = re.fullmatch(r"(\d+)\s*([smh])", tiempo.strip().lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    return None


def replace_variables(text: str, member: discord.Member) -> str:
    guild = member.guild
    replacements = {
        "{member}": member.mention,
        "{member.name}": member.name,
        "{member.tag}": str(member),
        "{member.id}": str(member.id),
        "{server}": guild.name,
        "{server.name}": guild.name,
        "{server.id}": str(guild.id),
        "{membercount}": str(guild.member_count),
        "{member.count}": str(guild.member_count),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


# ─────────────────────────────────────────────
# Eventos
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Conectado como {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Comandos sincronizados: {len(synced)}")
    except Exception as e:
        print(f"Error al sincronizar comandos: {e}")

    # Restaurar roles programados pendientes
    bot.loop.create_task(restore_scheduled_roles())


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return

    config = get_guild_config(member.guild.id)

    # ── Autorole ──
    if config.get("autorole"):
        role = member.guild.get_role(config["autorole"])
        if role and role < member.guild.me.top_role:
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.Forbidden:
                print(f"Sin permisos para dar autorole en {member.guild.name}")
            except Exception as e:
                print(f"Error autorole: {e}")

    # ── Bienvenida ──
    if config.get("welcome_message") and config.get("welcome_channel"):
        channel = member.guild.get_channel(config["welcome_channel"])
        if channel and isinstance(channel, discord.TextChannel):
            msg = replace_variables(config["welcome_message"], member)
            try:
                await channel.send(msg)
            except discord.Forbidden:
                print(f"Sin permisos para enviar bienvenida en {member.guild.name}")
            except Exception as e:
                print(f"Error bienvenida: {e}")


# ─────────────────────────────────────────────
# Roles programados (se asignan después de X tiempo)
# ─────────────────────────────────────────────
async def apply_scheduled_role(guild_id: int, user_id: int, role_id: int):
    """Asigna el rol cuando llega el momento."""
    guild = bot.get_guild(guild_id)
    if not guild:
        temproles_col.delete_one({"guild_id": guild_id, "user_id": user_id, "role_id": role_id})
        return

    member = guild.get_member(user_id)
    role = guild.get_role(role_id)

    if member and role:
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Rol programado (temprole)")
            except Exception as e:
                print(f"Error aplicando temprole: {e}")
    else:
        print(f"No se pudo aplicar temprole: member o role no encontrados (guild={guild_id})")

    temproles_col.delete_one({"guild_id": guild_id, "user_id": user_id, "role_id": role_id})


async def schedule_role(guild_id: int, user_id: int, role_id: int, apply_at: datetime):
    now = datetime.now(timezone.utc)
    delay = (apply_at - now).total_seconds()
    if delay <= 0:
        await apply_scheduled_role(guild_id, user_id, role_id)
        return
    await asyncio.sleep(delay)
    await apply_scheduled_role(guild_id, user_id, role_id)


async def restore_scheduled_roles():
    await bot.wait_until_ready()
    now = datetime.now(timezone.utc)
    for doc in temproles_col.find({}):
        apply_at = doc["apply_at"]
        if apply_at.tzinfo is None:
            apply_at = apply_at.replace(tzinfo=timezone.utc)

        if apply_at <= now:
            await apply_scheduled_role(doc["guild_id"], doc["user_id"], doc["role_id"])
        else:
            bot.loop.create_task(
                schedule_role(doc["guild_id"], doc["user_id"], doc["role_id"], apply_at)
            )


# ─────────────────────────────────────────────
# Slash Commands
# ─────────────────────────────────────────────
@bot.tree.command(name="welcomer", description="Configura el mensaje y canal de bienvenida")
@app_commands.describe(
    mensaje="Mensaje de bienvenida. Variables: {member} {member.name} {server} {membercount}",
    canal="Canal donde se enviará el mensaje",
)
@app_commands.default_permissions(manage_guild=True)
async def welcomer(interaction: discord.Interaction, mensaje: str, canal: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Este comando solo funciona en servidores.", ephemeral=True)
        return

    update_guild_config(
        interaction.guild.id,
        welcome_message=mensaje,
        welcome_channel=canal.id,
    )

    preview = replace_variables(mensaje, interaction.user)
    embed = discord.Embed(
        title="Bienvenida configurada",
        description=f"**Canal:** {canal.mention}\n**Mensaje:**\n{preview}",
        color=0x2ECC71,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="autorole", description="Configura el rol que se da automáticamente al unirse")
@app_commands.describe(rol="Rol que se asignará a los nuevos miembros")
@app_commands.default_permissions(manage_guild=True, manage_roles=True)
async def autorole(interaction: discord.Interaction, rol: discord.Role):
    if not interaction.guild:
        await interaction.response.send_message("Este comando solo funciona en servidores.", ephemeral=True)
        return

    if rol >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "No puedo asignar ese rol porque está por encima o al mismo nivel que mi rol más alto.",
            ephemeral=True,
        )
        return

    if rol.managed:
        await interaction.response.send_message(
            "No puedo asignar roles gestionados por integraciones (bots, boosts, etc.).",
            ephemeral=True,
        )
        return

    update_guild_config(interaction.guild.id, autorole=rol.id)

    embed = discord.Embed(
        title="Autorole configurado",
        description=f"Los nuevos miembros recibirán el rol {rol.mention} al unirse.",
        color=0x3498DB,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="temprole", description="Programa un rol para que se asigne después de un tiempo")
@app_commands.describe(
    miembro="Usuario al que se le dará el rol",
    rol="Rol que se asignará",
    tiempo="Tiempo de espera: ej. 30s, 10m, 2h",
)
@app_commands.default_permissions(manage_roles=True)
async def temprole(
    interaction: discord.Interaction,
    miembro: discord.Member,
    rol: discord.Role,
    tiempo: str,
):
    if not interaction.guild:
        await interaction.response.send_message("Este comando solo funciona en servidores.", ephemeral=True)
        return

    seconds = parse_duration(tiempo)
    if seconds is None or seconds <= 0:
        await interaction.response.send_message(
            "Formato de tiempo inválido. Usa por ejemplo: `30s`, `10m`, `2h`",
            ephemeral=True,
        )
        return

    if seconds > 30 * 24 * 3600:  # máximo 30 días
        await interaction.response.send_message(
            "El tiempo máximo es 30 días.",
            ephemeral=True,
        )
        return

    if rol >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "No puedo asignar ese rol porque está por encima o al mismo nivel que mi rol más alto.",
            ephemeral=True,
        )
        return

    if rol.managed:
        await interaction.response.send_message(
            "No puedo asignar roles gestionados por integraciones.",
            ephemeral=True,
        )
        return

    if rol in miembro.roles:
        await interaction.response.send_message(
            f"{miembro.mention} ya tiene el rol {rol.mention}.",
            ephemeral=True,
        )
        return

    apply_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    # Guardar en DB (sobrevive reinicios)
    temproles_col.update_one(
        {"guild_id": interaction.guild.id, "user_id": miembro.id, "role_id": rol.id},
        {
            "$set": {
                "guild_id": interaction.guild.id,
                "user_id": miembro.id,
                "role_id": rol.id,
                "apply_at": apply_at,
                "given_by": interaction.user.id,
            }
        },
        upsert=True,
    )

    # Programar la asignación
    bot.loop.create_task(
        schedule_role(interaction.guild.id, miembro.id, rol.id, apply_at)
    )

    embed = discord.Embed(
        title="Rol programado",
        description=(
            f"{miembro.mention} recibirá {rol.mention}\n"
            f"**En:** {tiempo}\n"
            f"**Se asigna:** <t:{int(apply_at.timestamp())}:R>"
        ),
        color=0xE67E22,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="quitar_autorole", description="Quita el autorole configurado")
@app_commands.default_permissions(manage_guild=True)
async def quitar_autorole(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Este comando solo funciona en servidores.", ephemeral=True)
        return

    update_guild_config(interaction.guild.id, autorole=None)
    await interaction.response.send_message("Autorole desactivado.", ephemeral=True)


@bot.tree.command(name="quitar_welcomer", description="Desactiva el mensaje de bienvenida")
@app_commands.default_permissions(manage_guild=True)
async def quitar_welcomer(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Este comando solo funciona en servidores.", ephemeral=True)
        return

    update_guild_config(
        interaction.guild.id,
        welcome_message=None,
        welcome_channel=None,
    )
    await interaction.response.send_message("Sistema de bienvenida desactivado.", ephemeral=True)


@bot.tree.command(name="config", description="Muestra la configuración actual del servidor")
@app_commands.default_permissions(manage_guild=True)
async def config(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Este comando solo funciona en servidores.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)

    welcome_ch = "No configurado"
    if cfg.get("welcome_channel"):
        ch = interaction.guild.get_channel(cfg["welcome_channel"])
        welcome_ch = ch.mention if ch else f"ID: {cfg['welcome_channel']} (canal no encontrado)"

    autorole_txt = "No configurado"
    if cfg.get("autorole"):
        role = interaction.guild.get_role(cfg["autorole"])
        autorole_txt = role.mention if role else f"ID: {cfg['autorole']} (rol no encontrado)"

    embed = discord.Embed(title=f"Configuración de {interaction.guild.name}", color=0x9B59B6)
    embed.add_field(name="Canal de bienvenida", value=welcome_ch, inline=False)
    embed.add_field(
        name="Mensaje de bienvenida",
        value=cfg.get("welcome_message") or "No configurado",
        inline=False,
    )
    embed.add_field(name="Autorole", value=autorole_txt, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
# Flask (UptimeRobot)
# ─────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def home():
    return "Peak Bot is online", 200


@app.route("/health")
def health():
    return {"status": "ok", "bot": str(bot.user) if bot.user else "starting"}, 200


def run_flask():
    app.run(host="0.0.0.0", port=PORT)


# ─────────────────────────────────────────────
# Inicio
# ─────────────────────────────────────────────
if __name__ == "__main__":
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(TOKEN)
