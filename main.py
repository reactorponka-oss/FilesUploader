
import os
import re
import time
import json
import sqlite3
import zipfile
import shutil
import subprocess
import requests

from pathlib import Path, PurePosixPath


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_USERNAME = os.environ["CHANNEL_USERNAME"]

LOCAL_API = os.getenv("LOCAL_API", "http://127.0.0.1:8081").rstrip("/")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
WORK_DIR = DATA_DIR / "work"
EXTRACT_DIR = WORK_DIR / "extracted"
DB_PATH = DATA_DIR / "bot.db"

# Telegram Local Bot API upload limit: approximately 2000 MB.
MAX_VIDEO_SIZE = 2_000_000_000

# Maximum compressed ZIP size accepted by this bot.
MAX_ZIP_SIZE = int(os.getenv("MAX_ZIP_SIZE", str(4 * 1024**3)))

# Maximum total extracted data to protect disk space.
MAX_EXTRACT_SIZE = int(os.getenv("MAX_EXTRACT_SIZE", str(50 * 1024**3)))

# Maximum number of files inside one ZIP.
MAX_FILES = int(os.getenv("MAX_FILES", "20000"))

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".m4v", ".ts", ".mpg", ".mpeg", ".3gp",
    ".wmv", ".flv", ".vob"
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "TelegramZipVideoBot/2.0"})


# ============================================================
# DIRECTORIES AND DATABASE
# ============================================================

for folder in (DATA_DIR, WORK_DIR, EXTRACT_DIR):
    folder.mkdir(parents=True, exist_ok=True)


def conn():
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                file_name TEXT,
                file_size INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at INTEGER
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        db.execute("""
            INSERT OR IGNORE INTO settings(key, value)
            VALUES('offset', '0')
        """)


# ============================================================
# TELEGRAM API
# ============================================================

def api(method, payload=None, timeout=120):
    url = f"{LOCAL_API}/bot{BOT_TOKEN}/{method}"

    try:
        response = SESSION.post(
            url,
            json=payload or {},
            timeout=timeout
        )
        response.raise_for_status()
        data = response.json()

    except Exception as exc:
        raise RuntimeError(
            f"Telegram API connection error ({method}): {exc}"
        )

    if not data.get("ok"):
        raise RuntimeError(
            f"Telegram API error ({method}): {data.get('description', data)}"
        )

    return data.get("result")


def say(chat_id, message):
    try:
        api("sendMessage", {
            "chat_id": chat_id,
            "text": str(message)[:4000]
        })
    except Exception as exc:
        print("Message send error:", exc)


def owner_chat(message):
    chat = message.get("chat", {})
    return chat.get("type") == "private" and chat.get("id") == OWNER_ID


# ============================================================
# ZIP DOWNLOAD AND VALIDATION
# ============================================================

def get_archive(job):
    """
    Download/locate the ZIP through the Local Bot API.
    Check its real size and ZIP signature before extraction.
    """

    expected_size = int(job["file_size"] or 0)
    file_id = job["file_id"]

    if expected_size > MAX_ZIP_SIZE:
        raise RuntimeError(
            f"ZIP bahut badi hai: {expected_size:,} bytes. "
            f"Allowed: {MAX_ZIP_SIZE:,} bytes."
        )

    last_error = None

    for attempt in range(1, 4):
        try:
            result = api("getFile", {"file_id": file_id}, timeout=300)

            file_path = result.get("file_path")
            if not file_path:
                raise RuntimeError("Telegram ne file_path return nahi kiya.")

            archive = Path(file_path)

            # Local Bot API normally returns an absolute path.
            # If a relative path is returned, try common data locations.
            if not archive.is_absolute():
                candidates = [
                    Path.cwd() / archive,
                    DATA_DIR / archive,
                    DATA_DIR / "botapi" / archive
                ]

                found = next(
                    (candidate for candidate in candidates if candidate.is_file()),
                    None
                )

                if found is None:
                    raise RuntimeError(
                        f"Telegram file path nahi mila: {file_path}"
                    )

                archive = found

            if not archive.is_file():
                raise RuntimeError(
                    f"Telegram file path exist nahi karta: {archive}"
                )

            actual_size = archive.stat().st_size

            print(
                f"ZIP DEBUG attempt={attempt} "
                f"path={archive} "
                f"actual_size={actual_size} "
                f"expected_size={expected_size}"
            )

            if actual_size <= 0:
                raise RuntimeError("Downloaded ZIP file empty hai.")

            if actual_size > MAX_ZIP_SIZE:
                raise RuntimeError(
                    f"Downloaded file limit se badi hai: {actual_size:,} bytes."
                )

            # Telegram's document file_size should match the local file.
            if expected_size and actual_size != expected_size:
                raise RuntimeError(
                    f"File size mismatch: Telegram ne {expected_size:,} bytes "
                    f"bataye, local file {actual_size:,} bytes ki hai."
                )

            with archive.open("rb") as f:
                header = f.read(32)

            print("ZIP DEBUG header:", header.hex())

            # ZIP signatures:
            # PK\x03\x04 = normal ZIP
            # PK\x05\x06 = empty ZIP
            # PK\x07\x08 = spanning ZIP
            if not (
                header.startswith(b"PK\x03\x04")
                or header.startswith(b"PK\x05\x06")
                or header.startswith(b"PK\x07\x08")
            ):
                raise RuntimeError(
                    "File ka ZIP signature galat hai. "
                    f"First bytes: {header[:16].hex()}. "
                    "Ho sakta hai file ZIP na ho, ya Telegram se "
                    "incomplete/galat file mili ho."
                )

            if not zipfile.is_zipfile(archive):
                raise RuntimeError(
                    "ZIP signature mila, lekin archive structure valid nahi hai. "
                    "ZIP corrupt ya incomplete ho sakti hai."
                )

            # Full archive integrity test.
            with zipfile.ZipFile(archive, "r") as z:
                bad_file = z.testzip()

                if bad_file:
                    raise RuntimeError(
                        f"ZIP ke andar corrupt file mili: {bad_file}"
                    )

            return archive

        except Exception as exc:
            last_error = exc
            print(f"ZIP DOWNLOAD/VALIDATION attempt {attempt} failed:", exc)

            if attempt < 3:
                time.sleep(3 * attempt)

    raise RuntimeError(
        f"ZIP download/validation fail hui after 3 attempts: {last_error}"
    )


# ============================================================
# SAFE ZIP EXTRACTION
# ============================================================

def extract(job_id, archive):
    target = EXTRACT_DIR / str(job_id)

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    target.mkdir(parents=True, exist_ok=True)

    video_paths = []
    total_size = 0
    file_count = 0

    try:
        with zipfile.ZipFile(archive, "r") as z:
            entries = z.infolist()

            if not entries:
                raise RuntimeError("ZIP archive empty hai.")

            if len(entries) > MAX_FILES:
                raise RuntimeError(
                    f"ZIP mein {len(entries)} entries hain. "
                    f"Maximum allowed: {MAX_FILES}"
                )

            for info in entries:
                if info.is_dir():
                    continue

                file_count += 1
                total_size += info.file_size

                if total_size > MAX_EXTRACT_SIZE:
                    raise RuntimeError(
                        "ZIP extract size limit cross ho gayi. "
                        f"Maximum: {MAX_EXTRACT_SIZE:,} bytes."
                    )

                # ZIP path validation to prevent path traversal.
                member = info.filename.replace("\\", "/")
                pure = PurePosixPath(member)

                if pure.is_absolute() or ".." in pure.parts:
                    print("Skipping unsafe ZIP entry:", member)
                    continue

                if not pure.parts:
                    continue

                destination = target.joinpath(*pure.parts)

                try:
                    destination.resolve().relative_to(target.resolve())
                except ValueError:
                    print("Skipping unsafe path:", member)
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)

                # Stream extraction instead of loading the whole file in RAM.
                with z.open(info, "r") as source:
                    with destination.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)

                if destination.suffix.lower() in VIDEO_EXTENSIONS:
                    video_paths.append(destination)

        if not video_paths:
            raise RuntimeError(
                "ZIP extract ho gayi, lekin supported video files nahi mili."
            )

        return video_paths

    except zipfile.BadZipFile as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"ZIP corrupt hai ya incomplete hai: {exc}"
        )

    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


# ============================================================
# VIDEO CONVERSION
# ============================================================

def probe_video(path):
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "json",
        str(path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FFprobe error: {result.stderr[-1000:]}"
        )

    data = json.loads(result.stdout or "{}")

    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None
    )

    if not video:
        raise RuntimeError("Video stream nahi mili.")

    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"),
        None
    )

    return video.get("codec_name"), (
        audio.get("codec_name") if audio else None
    )


def make_telegram_mp4(path):
    """
    Keep MP4/H.264 files as-is.
    Convert other formats/codecs to MP4/H.264 for Telegram playback.
    """

    video_codec, audio_codec = probe_video(path)

    if (
        path.suffix.lower() == ".mp4"
        and video_codec == "h264"
        and audio_codec in (None, "aac")
    ):
        if path.stat().st_size > MAX_VIDEO_SIZE:
            raise RuntimeError(
                "Video 2000MB Telegram upload limit se badi hai."
            )

        return path, None

    output = path.with_name(path.stem + "_telegram.mp4")

    command = [
        "ffmpeg",
        "-y",
        "-i", str(path),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=24 * 60 * 60
    )

    if result.returncode != 0 or not output.is_file():
        raise RuntimeError(
            "FFmpeg conversion fail hui: " + result.stderr[-1500:]
        )

    if output.stat().st_size > MAX_VIDEO_SIZE:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "Converted video 2000MB Telegram upload limit se badi hai."
        )

    return output, output


# ============================================================
# SEND VIDEO TO CHANNEL
# ============================================================

def send_video(path):
    size = path.stat().st_size

    if size > MAX_VIDEO_SIZE:
        raise RuntimeError(
            f"Video {size:,} bytes ki hai. "
            "Telegram Local Bot API limit approximately 2000MB hai."
        )

    # Local Bot API supports local file paths using file:// URLs.
    local_uri = path.resolve().as_uri()

    payload = {
        "chat_id": CHANNEL_USERNAME,
        "video": local_uri,
        "supports_streaming": True,
        "caption": path.name[:900]
    }

    result = api("sendVideo", payload, timeout=24 * 60 * 60)

    if not result:
        raise RuntimeError("Telegram ne sendVideo ka result nahi diya.")

    return result


# ============================================================
# JOB PROCESSING
# ============================================================

def update_job(job_id, status, error=None):
    with conn() as db:
        db.execute(
            "UPDATE jobs SET status=?, error=? WHERE id=?",
            (status, error, job_id)
        )


def process_job(job_id, chat_id):
    with conn() as db:
        job = db.execute(
            "SELECT * FROM jobs WHERE id=?",
            (job_id,)
        ).fetchone()

    if not job:
        say(chat_id, f"Job #{job_id} nahi mili.")
        return

    update_job(job_id, "processing")

    say(
        chat_id,
        f"ZIP #{job_id} process ho rahi hai: {job['file_name']}"
    )

    try:
        archive = get_archive(job)

        say(
            chat_id,
            f"ZIP #{job_id} verify ho gayi. Ab extract kar raha hoon."
        )

        video_paths = extract(job_id, archive)

        with conn() as db:
            db.execute(
                "DELETE FROM videos WHERE job_id=?",
                (job_id,)
            )

            for path in video_paths:
                db.execute(
                    """
                    INSERT INTO videos(job_id, path, status)
                    VALUES(?, ?, 'pending')
                    """,
                    (job_id, str(path))
                )

        say(
            chat_id,
            f"ZIP #{job_id} extract complete.\n"
            f"Videos found: {len(video_paths)}\n"
            "Ab channel par upload shuru."
        )

        with conn() as db:
            videos = db.execute(
                """
                SELECT * FROM videos
                WHERE job_id=? AND status IN ('pending', 'sending')
                ORDER BY id
                """,
                (job_id,)
            ).fetchall()

        sent = 0
        failed = 0

        for video in videos:
            video_id = video["id"]
            source = Path(video["path"])

            if not source.is_file():
                with conn() as db:
                    db.execute(
                        "UPDATE videos SET status='failed', error=? WHERE id=?",
                        ("Video file missing", video_id)
                    )
                failed += 1
                continue

            with conn() as db:
                db.execute(
                    "UPDATE videos SET status='sending' WHERE id=?",
                    (video_id,)
                )

            converted = None

            try:
                upload_path, converted = make_telegram_mp4(source)

                send_video(upload_path)

                with conn() as db:
                    db.execute(
                        """
                        UPDATE videos
                        SET status='sent', error=NULL
                        WHERE id=?
                        """,
                        (video_id,)
                    )

                sent += 1
                print(f"Uploaded video {video_id}: {source.name}")

            except Exception as exc:
                failed += 1

                with conn() as db:
                    db.execute(
                        """
                        UPDATE videos
                        SET status='failed', error=?
                        WHERE id=?
                        """,
                        (str(exc)[:1500], video_id)
                    )

                print(f"Video upload failed: {source.name}", exc)

            finally:
                if converted and converted.exists():
                    converted.unlink(missing_ok=True)

        final_status = "completed" if failed == 0 else "completed_with_errors"
        update_job(job_id, final_status)

        say(
            chat_id,
            f"ZIP #{job_id} ka kaam complete.\n\n"
            f"Total videos: {len(videos)}\n"
            f"Uploaded: {sent}\n"
            f"Failed: {failed}"
        )

    except Exception as exc:
        error = str(exc)
        update_job(job_id, "failed", error)

        print(f"JOB #{job_id} ERROR:", error)

        say(
            chat_id,
            f"ZIP #{job_id} process nahi hui.\n\n"
            f"Reason:\n{error}"
        )


# ============================================================
# COMMANDS AND MESSAGE HANDLING
# ============================================================

def handle(message):
    if not owner_chat(message):
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/start"):
        say(
            chat_id,
            "Bot ready hai.\n\n"
            "Mujhe ZIP file document ke roop mein bhejo.\n"
            "Main ZIP ke andar ke videos extract karke channel par "
            "Telegram playable videos ke roop mein upload karunga.\n\n"
            "Commands:\n"
            "/status - jobs ki status\n"
            "/retry JOB_ID - failed job dobara chalaye"
        )
        return

    if text.startswith("/status"):
        with conn() as db:
            jobs = db.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 10"
            ).fetchall()

        if not jobs:
            say(chat_id, "Abhi koi ZIP job nahi hai.")
            return

        lines = ["Recent jobs:"]

        for job in jobs:
            lines.append(
                f"#{job['id']} | {job['file_name']} | "
                f"{job['status']}"
            )

            if job["error"]:
                lines.append(f"  Error: {job['error'][:300]}")

        say(chat_id, "\n".join(lines))
        return

    if text.startswith("/retry"):
        parts = text.split()

        if len(parts) != 2 or not parts[1].isdigit():
            say(chat_id, "Use: /retry JOB_ID")
            return

        job_id = int(parts[1])

        with conn() as db:
            job = db.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,)
            ).fetchone()

            if not job:
                say(chat_id, "Job nahi mili.")
                return

            db.execute(
                """
                UPDATE jobs
                SET status='pending', error=NULL
                WHERE id=?
                """,
                (job_id,)
            )

            db.execute(
                """
                UPDATE videos
                SET status='pending', error=NULL
                WHERE job_id=? AND status!='sent'
                """,
                (job_id,)
            )

        process_job(job_id, chat_id)
        return

    document = message.get("document")

    if document:
        file_name = document.get("file_name", "archive.zip")
        file_id = document.get("file_id")
        file_size = int(document.get("file_size", 0))

        if not file_name.lower().endswith(".zip"):
            say(chat_id, "Please ZIP file document ke roop mein bhejo.")
            return

        if file_size > MAX_ZIP_SIZE:
            say(
                chat_id,
                f"ZIP size limit se badi hai. "
                f"Maximum: {MAX_ZIP_SIZE:,} bytes."
            )
            return

        with conn() as db:
            cursor = db.execute(
                """
                INSERT INTO jobs(
                    file_id, file_name, file_size, status, created_at
                )
                VALUES(?, ?, ?, 'pending', ?)
                """,
                (file_id, file_name, file_size, int(time.time()))
            )

            job_id = cursor.lastrowid

        process_job(job_id, chat_id)
        return

    if text.startswith("/"):
        say(chat_id, "Unknown command. /start bhejo.")
        return

    say(chat_id, "ZIP file ko document ke roop mein bhejo.")


# ============================================================
# RECOVERY
# ============================================================

def recover():
    """
    Recover jobs interrupted while processing.
    Sent videos are not uploaded again.
    """

    with conn() as db:
        jobs = db.execute(
            """
            SELECT id FROM jobs
            WHERE status IN ('processing', 'pending')
            ORDER BY id
            """
        ).fetchall()

    for job in jobs:
        job_id = job["id"]

        print("Recovering job:", job_id)

        try:
            process_job(job_id, OWNER_ID)
        except Exception as exc:
            print("Recovery error:", job_id, exc)


# ============================================================
# MAIN LONG POLLING LOOP
# ============================================================

def main():
    init_db()

    print("Local Bot API:", LOCAL_API)
    print("Bot starting...")

    # Check bot token and API connectivity.
    me = api("getMe")
    print("Bot connected:", me.get("username"))

    # Check channel access.
    try:
        api("getChat", {"chat_id": CHANNEL_USERNAME})
        print("Channel connected:", CHANNEL_USERNAME)
    except Exception as exc:
        print("WARNING: Channel access check failed:", exc)

    with conn() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key='offset'"
        ).fetchone()

        offset = int(row["value"]) if row else 0

    recover()

    while True:
        try:
            updates = api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": ["message"]
                },
                timeout=65
            )

            for update in updates:
                update_id = update["update_id"]

                try:
                    message = update.get("message")

                    if message:
                        handle(message)

                except Exception as exc:
                    print("Update handling error:", exc)

                offset = update_id + 1

                with conn() as db:
                    db.execute(
                        """
                        INSERT INTO settings(key, value)
                        VALUES('offset', ?)
                        ON CONFLICT(key)
                        DO UPDATE SET value=excluded.value
                        """,
                        (str(offset),)
                    )

        except KeyboardInterrupt:
            print("Bot stopped.")
            break

        except Exception as exc:
            print("Polling error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
