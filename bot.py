import discord
from discord.ext import commands
import yt_dlp
import shutil
import asyncio
import random
import os
from dotenv import load_dotenv

load_dotenv()  # загружает переменные из .env

# Настройки yt-dlp
YTDL_OPTS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_opts), data=data)

# Включаем нужные интенты
intents = discord.Intents.default()
intents.message_content = True

# Создаём бота
bot = commands.Bot(command_prefix="!", intents=intents)
@bot.event
async def on_ready():
    # Проверка ffmpeg
    if shutil.which("ffmpeg") is None:
        print("[WARN] ffmpeg не найден! Музыка работать не будет.")
    else:
        print("[OK] ffmpeg найден.")

    print(f"[OK] Бот {bot.user} запущен и готов к музыке!")
    try:
        synced = await bot.tree.sync()
        print(f"[INFO] Синхронизировано {len(synced)} слэш-команд.")
    except Exception as e:
        print(f"[ERROR] Синхронизация: {e}")

# Слэш-команды
@bot.tree.command(name="join", description="Подключиться к вашему голосовому каналу")
async def join(interaction: discord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if interaction.guild.voice_client:
            await interaction.response.send_message("❌ Я уже в голосовом канале!")
            return
        await channel.connect()
        await interaction.response.send_message(f"✅ Подключился к {channel}")
    else:
        await interaction.response.send_message("❌ Ты не в голосовом канале!")

# Кнопка выхода из канала
@bot.tree.command(name="leave", description="Отключиться от голосового канала")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Покинул канал.")
    else:
        await interaction.response.send_message("❌ Я не в голосовом канале.")

# Кнопка играть музыку
@bot.tree.command(name="play", description="Воспроизвести аудио по ссылке или названию")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if not interaction.guild.voice_client:
        if interaction.user.voice:
            channel = interaction.user.voice.channel
            await channel.connect()
        else:
            await interaction.followup.send("❌ Зайди в голосовой канал сначала!")
            return

    try:
        player = await YTDLSource.from_url(query, loop=bot.loop, stream=True)
        interaction.guild.voice_client.play(
            player,
            after=lambda e: print(f'[ERROR] Ошибка воспроизведения: {e}') if e else None
        )
        await interaction.followup.send(f'▶️ Играет: **{player.title}**')
    except Exception as e:
        await interaction.followup.send(f"❌ Не удалось воспроизвести: {str(e)}")

# Кнопка остановки музыки
@bot.tree.command(name="stop", description="Остановить воспроизведение")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏹️ Остановлено.")
    else:
        await interaction.response.send_message("🔇 Ничего не играет.")

# Рандомные сталкерские цитаты
@bot.tree.command(name="stalker", description="Получить мудрость из Зоны")
async def stalker(interaction: discord.Interaction):
    quotes = [
        "Зона не прощает ошибок. Только — шансов.",
        "Снова слышу звон... Кажется, 'Грави' где-то рядом.",
        "Не верь Клыку — он с Монолитом.",
        "В Зоне выживает не сильнейший, а тот, кто не лезет без нужды.",
        "Артефакт — не цель. Выживание — цель.",
        "Тишина в Зоне страшнее выстрелов.",
        "Если видишь аномалию — бросай болт. Если не видишь — бросай два.",
        "Монолит зовёт... но я не пойду.",
        "Карта — ложь. Глаза — правда.",
        "Смерть в Зоне приходит тихо. И всегда — сзади."
    ]
    quote = random.choice(quotes)
    await interaction.response.send_message(f"📜 *{quote}*")

# Простая очередь — пока только показывает текущий трек
@bot.tree.command(name="queue", description="Показать очередь воспроизведения")
async def queue(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        await interaction.response.send_message("🎵 Сейчас играет: *Неизвестный трек*\nОчередь: пусто (пока не реализована)")
    else:
        await interaction.response.send_message("🔇 Ничего не играет. Очередь пуста.")

# Запуск бота
if __name__ == "__main__":
    try:
        bot.run(os.getenv('DISCORD_TOKEN'))
    except KeyboardInterrupt:
        print("[INFO] Бот остановлен вручную.")
    except Exception as e:
        print(f"[CRITICAL] Критическая ошибка: {e}")
        raise