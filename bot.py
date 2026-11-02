import asyncio
import logging
import os
import random
import shutil
from collections import deque
from dataclasses import dataclass

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # DISCORD_TOKEN берётся из .env
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("dj-bot")

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_retries": 3,
    "socket_timeout": 10,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
    "options": "-vn",
}

ALONE_TIMEOUT = 60  # сек: выйти, если в канале никого не осталось

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

STALKER_QUOTES = [
    "Зона не прощает ошибок. Только — шансов.",
    "Снова слышу звон... Кажется, 'Грави' где-то рядом.",
    "Не верь Клыку — он с Монолитом.",
    "В Зоне выживает не сильнейший, а тот, кто не лезет без нужды.",
    "Артефакт — не цель. Выживание — цель.",
    "Тишина в Зоне страшнее выстрелов.",
    "Если видишь аномалию — бросай болт. Если не видишь — бросай два.",
    "Монолит зовёт... но я не пойду.",
    "Карта — ложь. Глаза — правда.",
    "Смерть в Зоне приходит тихо. И всегда — сзади.",
]


def fmt_time(seconds) -> str:
    if not seconds:
        return "??:??"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


@dataclass
class Track:
    title: str
    page_url: str
    duration: int | None
    requester: str

    def label(self) -> str:
        return f"**{self.title}** `{fmt_time(self.duration)}`"


async def extract(query: str) -> dict:
    """Ищет трек/ссылку через yt-dlp (в отдельном потоке, чтобы не вешать бота)."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    if data and "entries" in data:
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise LookupError("ничего не найдено")
        data = entries[0]
    if not data:
        raise LookupError("ничего не найдено")
    return data


class GuildPlayer:
    """Очередь и воспроизведение для одного сервера."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume = 0.5
        self.text_channel: discord.abc.Messageable | None = None
        self.task: asyncio.Task | None = None

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    async def send(self, text: str):
        if self.text_channel:
            try:
                await self.text_channel.send(text)
            except discord.HTTPException:
                pass

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while self.queue:
            vc = self.vc
            if vc is None or not vc.is_connected():
                break
            track = self.queue.popleft()
            self.current = track
            try:
                # ссылка на поток живёт недолго, поэтому берём её прямо перед игрой
                data = await extract(track.page_url)
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(data["url"], **FFMPEG_OPTS), volume=self.volume
                )
            except Exception as e:
                log.warning("Не загрузился %s: %s", track.title, e)
                await self.send(f"❌ Не смог загрузить {track.label()}: {e}")
                continue

            done = asyncio.Event()

            def after(err):
                if err:
                    log.error("Ошибка воспроизведения: %s", err)
                self.bot.loop.call_soon_threadsafe(done.set)

            vc.play(source, after=after)
            await self.send(f"▶️ Играет: {track.label()} — заказал {track.requester}")
            await done.wait()
        self.current = None

    def stop(self):
        self.queue.clear()
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()

    async def disconnect(self):
        self.stop()
        if self.vc:
            await self.vc.disconnect()


class DJBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.players: dict[int, GuildPlayer] = {}

    def player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    async def setup_hook(self):
        synced = await self.tree.sync()
        log.info("Синхронизировано слэш-команд: %d", len(synced))

    async def on_ready(self):
        if shutil.which("ffmpeg") is None:
            log.warning("ffmpeg не найден! Музыка работать не будет.")
        log.info("Бот %s запущен и готов к музыке!", self.user)

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        vc = guild.voice_client
        if vc is None:
            return
        # бота выкинули из канала — чистим очередь
        if member.id == self.user.id and after.channel is None:
            self.players.pop(guild.id, None)
            return
        if not any(not m.bot for m in vc.channel.members):
            await asyncio.sleep(ALONE_TIMEOUT)
            vc = guild.voice_client
            if vc and not any(not m.bot for m in vc.channel.members):
                p = self.player(guild)
                await p.send("👋 Все ушли — я тоже.")
                await p.disconnect()


bot = DJBot()


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    """Подключает бота к каналу пользователя. Требует уже сделанного defer()."""
    voice = interaction.user.voice
    if not voice or not voice.channel:
        await interaction.followup.send("❌ Сначала зайди в голосовой канал.")
        return None
    vc = interaction.guild.voice_client
    if vc is None:
        return await voice.channel.connect(self_deaf=True)
    if vc.channel != voice.channel:
        if vc.is_playing() or vc.is_paused():
            await interaction.followup.send(f"❌ Я уже играю в {vc.channel.mention}.")
            return None
        await vc.move_to(voice.channel)
    return vc


# ---------- команды ----------

@bot.tree.command(name="join", description="Подключиться к вашему голосовому каналу")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc:
        await interaction.followup.send(f"✅ Я в {vc.channel.mention}")


@bot.tree.command(name="leave", description="Отключиться от голосового канала")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not interaction.guild.voice_client:
        return await interaction.response.send_message("❌ Я не в голосовом канале.")
    await bot.player(interaction.guild).disconnect()
    await interaction.response.send_message("👋 Покинул канал.")


@bot.tree.command(name="play", description="Добавить трек по ссылке или названию")
@app_commands.describe(query="Ссылка или название трека")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if not vc:
        return
    try:
        data = await extract(query)
    except Exception as e:
        return await interaction.followup.send(f"❌ Не нашёл: {e}")

    track = Track(
        title=data.get("title") or "Без названия",
        page_url=data.get("webpage_url") or data.get("original_url") or query,
        duration=data.get("duration"),
        requester=interaction.user.display_name,
    )
    p = bot.player(interaction.guild)
    p.text_channel = interaction.channel
    p.queue.append(track)

    if p.current is None:
        await interaction.followup.send(f"🎵 Запускаю {track.label()}")
    else:
        await interaction.followup.send(f"➕ В очереди #{len(p.queue)}: {track.label()}")
    p.start()


@bot.tree.command(name="skip", description="Пропустить текущий трек")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # after() запустит следующий трек
        await interaction.response.send_message("⏭️ Пропущено.")
    else:
        await interaction.response.send_message("🔇 Ничего не играет.")


@bot.tree.command(name="pause", description="Пауза")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Пауза.")
    else:
        await interaction.response.send_message("🔇 Ничего не играет.")


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Продолжаю.")
    else:
        await interaction.response.send_message("❌ Нечего продолжать.")


@bot.tree.command(name="stop", description="Остановить музыку и очистить очередь")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    bot.player(interaction.guild).stop()
    await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")


@bot.tree.command(name="queue", description="Показать очередь")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if p.current is None and not p.queue:
        return await interaction.response.send_message("🔇 Очередь пуста.")
    lines = []
    if p.current:
        lines.append(f"▶️ Сейчас: {p.current.label()}")
    for i, t in enumerate(list(p.queue)[:10], 1):
        lines.append(f"`{i}.` {t.label()} — {t.requester}")
    if len(p.queue) > 10:
        lines.append(f"…и ещё {len(p.queue) - 10}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="nowplaying", description="Что сейчас играет")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if p.current:
        await interaction.response.send_message(f"▶️ {p.current.label()} — заказал {p.current.requester}")
    else:
        await interaction.response.send_message("🔇 Ничего не играет.")


@bot.tree.command(name="volume", description="Громкость 0–100")
@app_commands.describe(level="Громкость в процентах")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
    p = bot.player(interaction.guild)
    p.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p.volume
    await interaction.response.send_message(f"🔊 Громкость: {level}%")


@bot.tree.command(name="shuffle", description="Перемешать очередь")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if len(p.queue) < 2:
        return await interaction.response.send_message("❌ Нечего перемешивать.")
    items = list(p.queue)
    random.shuffle(items)
    p.queue = deque(items)
    await interaction.response.send_message("🔀 Очередь перемешана.")


@bot.tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер в /queue")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    p = bot.player(interaction.guild)
    if position > len(p.queue):
        return await interaction.response.send_message("❌ Нет такого номера.")
    track = p.queue[position - 1]
    del p.queue[position - 1]
    await interaction.response.send_message(f"🗑️ Убрал {track.label()}")


@bot.tree.command(name="stalker", description="Получить мудрость из Зоны")
async def stalker(interaction: discord.Interaction):
    await interaction.response.send_message(f"📜 *{random.choice(STALKER_QUOTES)}*")


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Ошибка команды", exc_info=error)
    msg = f"❌ Ошибка: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("[CRITICAL] Нет DISCORD_TOKEN в .env")
    bot.run(TOKEN, log_handler=None)
