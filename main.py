import os, asyncio, logging, json, re
from io import BytesIO

from dotenv import load_dotenv
from bs4 import BeautifulSoup                       
from aiohttp import ClientSession, ClientTimeout
from telegram import (
    Update,
    constants,
    InputFile,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN  = os.getenv("TG_TOKEN")          
RAPID_KEY  = os.getenv("RAPID_KEY")         

YT_API_HOST   = "youtube-media-downloader.p.rapidapi.com"
YT_DETAILS_URL = f"https://{YT_API_HOST}/v2/video/details"
HEADERS_YT = {
    "x-rapidapi-host": YT_API_HOST,
    "x-rapidapi-key":  RAPID_KEY,
}

HEADERS_WEB = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def flatten_streams(videos_field: object) -> list[dict]:
    streams: list[dict] = []
    if isinstance(videos_field, list):
        streams.extend(videos_field)
    elif isinstance(videos_field, dict):
        for item in videos_field.values():
            if isinstance(item, list):
                streams.extend(item)
            elif isinstance(item, dict):
                streams.extend(item.values())
    return [s for s in streams if isinstance(s, dict) and s.get("url")]


def extract_youtube_id(text: str) -> str | None:
    text = text.strip()
    if "youtu" not in text:
        return None
    if "watch?v=" in text:
        return text.split("watch?v=")[1].split("&")[0]
    return text.split("/")[-1].split("?")[0]


async def process_youtube(update: Update, video_id: str):
    await update.message.reply_chat_action(constants.ChatAction.TYPING)

    async with ClientSession() as session:
        params = {
            "videoId":  video_id,
            "urlAccess": "normal",
            "videos":    "auto",
            "audios":    "auto",
        }
        async with session.get(YT_DETAILS_URL, headers=HEADERS_YT, params=params) as resp:
            if resp.status != 200:
                await update.message.reply_text(f"⚠️ YouTube API статус {resp.status}.")
                return
            data = await resp.json()

    streams = flatten_streams(data.get("videos"))
    if not streams:
        await update.message.reply_text("😕 Видео-потоки не найдены.")
        logging.warning("Empty streams for %s – raw=%s", video_id, json.dumps(data)[:300])
        return

    stream = streams[0]
    await update.message.reply_text(stream["url"])

TT_LINK_PATTERN = re.compile(r"https?://(?:vt\.|www\.)?tiktok\.com/\S+")

def extract_tiktok_url(text: str) -> str | None:
    m = TT_LINK_PATTERN.search(text.strip())
    return m.group(0) if m else None


async def extract_tiktok_media(tt_url: str) -> tuple[str | None, list[str]]:
    try:
        async with ClientSession() as session:
            async with session.get(
                "https://www.tikwm.com/api/",
                params={"url": tt_url, "hd": 1},
                headers={"User-Agent": HEADERS_WEB["User-Agent"]},
                timeout=ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 0 and isinstance(data.get("data"), dict):
                        d = data["data"]
                        video_url = d.get("play") or d.get("playwm")
                        photos = d.get("image", []) if isinstance(d.get("image"), list) else []
                        if video_url or photos:
                            return video_url, photos
    except Exception:
        logging.exception("tikwm.com API error")

    try:
        async with ClientSession() as session:
            async with session.post(
                "https://ssstik.io/abc?url=dl",
                headers=HEADERS_WEB,
                data={"id": tt_url, "locale": "en", "tt": "123"},
                timeout=ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logging.warning("ssstik.io status %s", resp.status)
                    return None, []
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        btn = soup.find("a", class_="pure-button")
        if btn and btn.get("href"):
            href = btn["href"]
            vid_url = "https://ssstik.io" + href if href.startswith("/") else href
            return vid_url, []

        img_div = soup.find("div", class_="image-cards")
        if img_div:
            photos = [img["src"] for img in img_div.find_all("img") if img.get("src")]
            return None, photos

    except Exception:
        logging.exception("TikTok fallback parse error")

    return None, []


async def send_tiktok_media(update: Update, video_url: str | None, photos: list[str]):
    if video_url:
        async with ClientSession() as sess, sess.get(
            video_url, headers=HEADERS_WEB, timeout=ClientTimeout(total=25)
        ) as resp:
            payload = await resp.read()
        await update.message.reply_video(
            video=InputFile(BytesIO(payload), filename="tiktok.mp4"),
            caption="@savetictokandyoutube_bot",
        )
    elif photos:
        media_group = []
        async with ClientSession() as sess:
            for url in photos:
                async with sess.get(url, headers=HEADERS_WEB, timeout=ClientTimeout(total=15)) as r:
                    media_group.append(
                        InputMediaPhoto(InputFile(BytesIO(await r.read()), filename="pic.jpg"))
                    )
        await update.message.reply_media_group(media_group)
        await update.message.reply_text("@savetictokandyoutube_bot")
    else:
        await update.message.reply_text("😕 Медиа не найдены.")


USERS_FILE = "users.json"

def load_users() -> set:
    if not os.path.exists(USERS_FILE):
        return set()
    try:
        with open(USERS_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_users(users: set):
    with open(USERS_FILE, "w") as f:
        json.dump(list(users), f)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users = load_users()
    if user_id not in users:
        users.add(user_id)
        save_users(users)
    await update.message.reply_text(
        "👋 Пришли ссылку YouTube или TikTok"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    yt_id   = extract_youtube_id(text)
    tt_link = extract_tiktok_url(text)

    if yt_id:
        await process_youtube(update, yt_id)
        return

    if tt_link:
        await update.message.reply_chat_action(constants.ChatAction.TYPING)
        video_url, photo_urls = await extract_tiktok_media(tt_link)
        if not video_url and not photo_urls:
            await update.message.reply_text("Не удалось найти медиа по ссылке.")
            return
        await send_tiktok_media(update, video_url, photo_urls)
        return

    await update.message.reply_text("Не распознал ссылку. Нужен YouTube или TikTok.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TG_TOKEN не задан в .env!")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logging.info("Bot is running…")
    app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
