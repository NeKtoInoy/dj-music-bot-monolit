import asyncio
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # DISCORD_TOKEN берётся из .env
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
if os.getenv("VOICE_DEBUG"):  # подробный лог голосового соединения: docker run -e VOICE_DEBUG=1 ...
    for name in ("discord.voice_state", "discord.gateway", "discord.player"):
        logging.getLogger(name).setLevel(logging.DEBUG)
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
# для плейлистов: только список треков, без загрузки каждого
YTDL_FLAT_OPTS = {**YTDL_OPTS, "noplaylist": False, "extract_flat": "in_playlist"}

def open_audio(url: str) -> tuple[subprocess.Popen, discord.FFmpegPCMAudio]:
    """yt-dlp качает звук кусками и отдаёт его ffmpeg через трубу.
    Прямую ссылку ffmpeg YouTube теперь часто режет ошибкой 403."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
         "--downloader", "native", "--http-chunk-size", "10M",
         "-f", "bestaudio/best", "-o", "-", url],
        stdout=subprocess.PIPE,
    )
    audio = discord.FFmpegPCMAudio(proc.stdout, pipe=True, before_options="-nostdin", options="-vn")
    return proc, audio

ALONE_TIMEOUT = 60      # сек: выйти, если в канале никого не осталось
PANEL_REFRESH = 15      # сек: как часто обновлять панель (чаще — упрёмся в лимиты Discord)
MAX_PLAYLIST = 100      # сколько треков максимум брать из плейлиста
PANEL_COLOR = 0x1D9E75

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)
ytdl_flat = yt_dlp.YoutubeDL(YTDL_FLAT_OPTS)

LOOP_NEXT = {"off": "queue", "queue": "track", "track": "off"}
LOOP_LABEL = {"off": "выкл", "queue": "очередь", "track": "трек"}

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
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def progress_bar(elapsed: float, duration, size: int = 16) -> str:
    if not duration:
        return "🔴 эфир"
    pos = min(int(elapsed / duration * (size - 1)), size - 1)
    return "▬" * pos + "🔘" + "▬" * (size - 1 - pos)


@dataclass
class Track:
    title: str
    page_url: str
    duration: int | None
    requester: str
    thumbnail: str | None = None

    def label(self) -> str:
        return f"**{self.title}** `{fmt_time(self.duration)}`"


def is_playlist(query: str) -> bool:
    return query.startswith("http") and ("list=" in query or "/playlist" in query or "/sets/" in query)


async def run_ytdl(ydl: yt_dlp.YoutubeDL, query: str) -> dict:
    """yt-dlp в отдельном потоке, чтобы не вешать бота."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
    if not data:
        raise LookupError("ничего не найдено")
    return data


async def extract(query: str) -> dict:
    data = await run_ytdl(ytdl, query)
    if "entries" in data:
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise LookupError("ничего не найдено")
        data = entries[0]
    return data


async def extract_playlist(query: str) -> tuple[str, list[dict]]:
    data = await run_ytdl(ytdl_flat, query)
    entries = [e for e in (data.get("entries") or []) if e][:MAX_PLAYLIST]
    return data.get("title") or "плейлист", entries


def queue_text(p: "GuildPlayer") -> str:
    if p.current is None and not p.queue:
        return "🔇 Очередь пуста."
    lines = []
    if p.current:
        lines.append(f"▶️ Сейчас: {p.current.label()}")
    for i, t in enumerate(list(p.queue)[:10], 1):
        lines.append(f"`{i}.` {t.label()} — {t.requester}")
    if len(p.queue) > 10:
        lines.append(f"…и ещё {len(p.queue) - 10}")
    return "\n".join(lines)


class GuildPlayer:
    """Очередь, воспроизведение и панель для одного сервера."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume = 0.5
        self.loop_mode = "off"
        self.text_channel: discord.abc.Messageable | None = None
        self.task: asyncio.Task | None = None
        self.updater: asyncio.Task | None = None
        self.panel: discord.Message | None = None
        self.view: PlayerView | None = None
        self.skip_requested = False
        self.stop_requested = False
        self.started_at = 0.0
        self.paused_at: float | None = None
        self.paused_total = 0.0

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def is_paused(self) -> bool:
        return bool(self.vc and self.vc.is_paused())

    def elapsed(self) -> float:
        end = self.paused_at or time.monotonic()
        return max(0.0, end - self.started_at - self.paused_total)

    async def send(self, text: str):
        if self.text_channel:
            try:
                await self.text_channel.send(text)
            except discord.HTTPException:
                pass

    # ---------- управление ----------

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    def pause(self) -> bool:
        if self.vc and self.vc.is_playing():
            self.vc.pause()
            self.paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        if self.vc and self.vc.is_paused():
            self.vc.resume()
            if self.paused_at:
                self.paused_total += time.monotonic() - self.paused_at
            self.paused_at = None
            return True
        return False

    def skip(self) -> bool:
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.skip_requested = True
            self.vc.stop()  # after() запустит следующий трек
            return True
        return False

    def stop(self):
        self.queue.clear()
        if self.skip():
            self.stop_requested = True

    def shuffle(self) -> bool:
        if len(self.queue) < 2:
            return False
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)
        return True

    async def disconnect(self):
        self.stop()
        if self.vc:
            await self.vc.disconnect()

    # ---------- цикл воспроизведения ----------

    async def _loop(self):
        while self.queue:
            vc = self.vc
            if vc is None or not vc.is_connected():
                break
            track = self.queue.popleft()
            self.current = track
            try:
                proc, audio = open_audio(track.page_url)
                source = discord.PCMVolumeTransformer(audio, volume=self.volume)
            except Exception as e:
                log.warning("Не загрузился %s: %s", track.title, e)
                await self.send(f"❌ Не смог загрузить {track.label()}: {e}")
                continue

            done = asyncio.Event()

            def after(err):
                if err:
                    log.error("Ошибка воспроизведения: %s", err)
                self.bot.loop.call_soon_threadsafe(done.set)

            self.skip_requested = False
            self.started_at = time.monotonic()
            self.paused_at = None
            self.paused_total = 0.0
            vc.play(source, after=after)
            await self.show_panel()
            await done.wait()
            rc = proc.poll()  # код выхода yt-dlp (None — ещё работает, например при пропуске)
            if rc is None:
                proc.kill()
            await asyncio.get_running_loop().run_in_executor(None, proc.wait)

            # yt-dlp упал, а трек никто не пропускал — значит YouTube не отдал звук
            if rc not in (None, 0) and not self.skip_requested and not self.stop_requested:
                await self.send(f"❌ YouTube не отдал звук для {track.label()} (подробности в консоли бота).")
                continue

            if self.stop_requested:
                self.stop_requested = False
                continue
            if self.loop_mode == "track" and not self.skip_requested:
                self.queue.appendleft(track)
            elif self.loop_mode == "queue":
                self.queue.append(track)

        self.current = None
        await self.finish_panel()

    # ---------- панель ----------

    def build_embed(self) -> discord.Embed:
        t = self.current
        e = discord.Embed(title=t.title[:256], url=t.page_url, color=PANEL_COLOR)
        e.set_author(name="⏸️ Пауза" if self.is_paused else "▶️ Сейчас играет")
        el = self.elapsed()
        e.description = f"`{fmt_time(el)}` {progress_bar(el, t.duration)} `{fmt_time(t.duration)}`"
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        upcoming = list(self.queue)[:3]
        value = "\n".join(f"`{i}.` {x.title[:60]}" for i, x in enumerate(upcoming, 1)) or "пусто"
        if len(self.queue) > 3:
            value += f"\n…и ещё {len(self.queue) - 3}"
        e.add_field(name="Далее", value=value, inline=False)
        e.set_footer(
            text=f"Заказал {t.requester} · громкость {round(self.volume * 100)}% · повтор: {LOOP_LABEL[self.loop_mode]}"
        )
        return e

    async def show_panel(self):
        """Новая панель под каждый трек — чтобы она всегда была внизу чата."""
        if not self.text_channel:
            return
        if self.panel:
            try:
                await self.panel.delete()
            except discord.HTTPException:
                pass
        self.view = PlayerView(self)
        try:
            self.panel = await self.text_channel.send(embed=self.build_embed(), view=self.view)
        except discord.HTTPException:
            self.panel = None
        if self.updater is None or self.updater.done():
            self.updater = asyncio.create_task(self._update_panel())

    async def refresh_panel(self):
        if not (self.panel and self.current and self.view):
            return
        self.view.sync()
        try:
            await self.panel.edit(embed=self.build_embed(), view=self.view)
        except discord.NotFound:
            self.panel = None
        except discord.HTTPException:
            pass

    async def _update_panel(self):
        while self.current:
            await asyncio.sleep(PANEL_REFRESH)
            await self.refresh_panel()

    async def finish_panel(self):
        if self.panel:
            try:
                await self.panel.edit(
                    embed=discord.Embed(description="⏹️ Очередь закончилась. `/play`, чтобы продолжить.", color=PANEL_COLOR),
                    view=None,
                )
            except discord.HTTPException:
                pass
            self.panel = None


class PlayerView(discord.ui.View):
    """Кнопки под панелью."""

    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player
        self.sync()

    def sync(self):
        self.pause_btn.emoji = "▶️" if self.player.is_paused else "⏸️"
        self.loop_btn.emoji = "🔂" if self.player.loop_mode == "track" else "🔁"
        self.loop_btn.style = (
            discord.ButtonStyle.secondary if self.player.loop_mode == "off" else discord.ButtonStyle.success
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = self.player.vc
        voice = interaction.user.voice
        if not vc or not voice or voice.channel != vc.channel:
            await interaction.response.send_message("❌ Зайди в мой голосовой канал.", ephemeral=True)
            return False
        return True

    async def redraw(self, interaction: discord.Interaction):
        self.sync()
        if self.player.current:
            await interaction.response.edit_message(embed=self.player.build_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.resume():
            self.player.pause()
        await self.redraw(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.skip()
        await interaction.response.defer()  # новая панель появится сама

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.stop()
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.loop_mode = LOOP_NEXT[self.player.loop_mode]
        await self.redraw(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.shuffle()
        await self.redraw(interaction)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(queue_text(self.player), ephemeral=True)


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
        if not discord.opus.is_loaded():
            discord.opus._load_default()
        log.info("opus загружен: %s", discord.opus.is_loaded())
        if not discord.opus.is_loaded():
            log.warning("Нет libopus! Звук работать не будет (apt install libopus0).")
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
        return await voice.channel.connect()
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


@bot.tree.command(name="play", description="Добавить трек или плейлист по ссылке или названию")
@app_commands.describe(query="Ссылка (видео или плейлист) или название трека")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if not vc:
        return
    p = bot.player(interaction.guild)
    p.text_channel = interaction.channel
    requester = interaction.user.display_name

    try:
        if is_playlist(query):
            title, entries = await extract_playlist(query)
            tracks = [
                Track(
                    title=e.get("title") or "Без названия",
                    page_url=e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    duration=e.get("duration"),
                    requester=requester,
                    thumbnail=(e.get("thumbnails") or [{}])[-1].get("url"),
                )
                for e in entries
            ]
            if not tracks:
                return await interaction.followup.send("❌ Плейлист пустой или закрытый.")
            p.queue.extend(tracks)
            await interaction.followup.send(f"📃 Плейлист **{title}**: добавил {len(tracks)} треков")
        else:
            data = await extract(query)
            track = Track(
                title=data.get("title") or "Без названия",
                page_url=data.get("webpage_url") or data.get("original_url") or query,
                duration=data.get("duration"),
                requester=requester,
                thumbnail=data.get("thumbnail"),
            )
            p.queue.append(track)
            if p.current is None:
                await interaction.followup.send(f"🎵 Запускаю {track.label()}")
            else:
                await interaction.followup.send(f"➕ В очереди #{len(p.queue)}: {track.label()}")
    except Exception as e:
        return await interaction.followup.send(f"❌ Не нашёл: {e}")

    if p.current:
        await p.refresh_panel()
    p.start()


@bot.tree.command(name="skip", description="Пропустить текущий трек")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    ok = bot.player(interaction.guild).skip()
    await interaction.response.send_message("⏭️ Пропущено." if ok else "🔇 Ничего не играет.")


@bot.tree.command(name="pause", description="Пауза")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.pause()
    await interaction.response.send_message("⏸️ Пауза." if ok else "🔇 Ничего не играет.")
    await p.refresh_panel()


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.resume()
    await interaction.response.send_message("▶️ Продолжаю." if ok else "❌ Нечего продолжать.")
    await p.refresh_panel()


@bot.tree.command(name="stop", description="Остановить музыку и очистить очередь")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    bot.player(interaction.guild).stop()
    await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")


@bot.tree.command(name="loop", description="Режим повтора")
@app_commands.describe(mode="Что повторять")
@app_commands.choices(mode=[
    app_commands.Choice(name="выкл", value="off"),
    app_commands.Choice(name="текущий трек", value="track"),
    app_commands.Choice(name="всю очередь", value="queue"),
])
@app_commands.guild_only()
async def loop_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    p = bot.player(interaction.guild)
    p.loop_mode = mode.value
    await interaction.response.send_message(f"🔁 Повтор: {LOOP_LABEL[mode.value]}")
    await p.refresh_panel()


@bot.tree.command(name="queue", description="Показать очередь")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(queue_text(bot.player(interaction.guild)))


@bot.tree.command(name="nowplaying", description="Показать панель плеера заново")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if not p.current:
        return await interaction.response.send_message("🔇 Ничего не играет.")
    p.text_channel = interaction.channel
    await interaction.response.send_message("👇", ephemeral=True)
    await p.show_panel()


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
    await p.refresh_panel()


@bot.tree.command(name="shuffle", description="Перемешать очередь")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.shuffle()
    await interaction.response.send_message("🔀 Очередь перемешана." if ok else "❌ Нечего перемешивать.")
    await p.refresh_panel()


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
    await p.refresh_panel()


@bot.tree.command(name="testtone", description="Тест звука: 5 секунд писка без YouTube")
@app_commands.guild_only()
async def testtone(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if not vc:
        return
    if vc.is_playing() or vc.is_paused():
        return await interaction.followup.send("❌ Сначала /stop.")
    src = discord.FFmpegPCMAudio("sine=frequency=440:duration=5", before_options="-f lavfi")
    vc.play(src, after=lambda e: log.info("testtone закончился, ошибка: %s", e))
    log.info("testtone: opus=%s, играет=%s", discord.opus.is_loaded(), vc.is_playing())
    await interaction.followup.send("🔊 Пищу 5 секунд. Слышно?")


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
