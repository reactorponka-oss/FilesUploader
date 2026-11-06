import os
import re
import time
import json
import uuid
import shutil
import sqlite3
import zipfile
import logging

from pathlib import Path
from requests import Session
from requests_toolbelt.multipart.encoder import MultipartEncoder

# ---------------- CONFIG ----------------

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL = os.environ["CHANNEL_ID"]

API = os.getenv("BOT_API_URL", "http://127.0.0.1:8081")
DATA = Path(os.getenv("DATA_DIR", "/app/data"))

MAX_ZIP = 4 * 1024**3
MAX_VIDEO = 1_999_000_000
MAX_EXTRACTED = int(os.getenv("MAX_EXTRACTED_SIZE", str(20 * 1024**3)))

VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".m4v", ".mpeg", ".mpg", ".wmv", ".ts", ".3gp"
}

DATA.mkdir(parents=True, exist_ok=True)

DB = DATA / "bot.sqlite3"
ZIP_DIR = DATA / "zips"
EXTRACT_DIR = DATA / "extracted"

ZIP_DIR.mkdir(exist_ok=True)
EXTRACT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("uploader")
session = Session()


# ---------------- DATABASE ----------------

def connect():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            zip_path TEXT,
            status TEXT
        );

        CREATE TABLE IF NOT EXISTS videos (
            job_id TEXT,
            path TEXT,
            status TEXT DEFAULT 'pending',
            PRIMARY KEY(job_id, path)
        );
        """)


def get_offset():
    with connect() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key='offset'"
        ).fetchone()
        return int(row[0]) if row else 0


def save_offset(offset):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings VALUES('offset',?)",
            (str(offset),)
        )


# ---------------- TELEGRAM API ----------------

def api(method, data=None, timeout=120):
    response = session.post(
        f"{API}/bot{TOKEN}/{method}",
        json=data or {},
        timeout=timeout
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))

    return result["result"]


def send_message(text):
    try:
        api("sendMessage", {
            "chat_id": OWNER_ID,
            "text": text[:4000]
        })
    except Exception as error:
        log.error("Message error: %s", error)


# ---------------- ZIP DOWNLOAD ----------------

def download_zip(file_id, job_id, expected_size):
    if expected_size and expected_size > MAX_ZIP:
        raise ValueError("ZIP file 4GB limit se badi hai")

    info = api("getFile", {"file_id": file_id})

    # Local Bot API returns the absolute file path.
    source = Path(info["file_path"])

    if not source.is_absolute() or not source.is_file():
        raise RuntimeError("Local Bot API file path unavailable")

    if source.stat().st_size > MAX_ZIP:
        raise ValueError("ZIP exceeds 4GB")

    destination = ZIP_DIR / f"{job_id}.zip"

    shutil.copy2(source, destination)

    if expected_size and destination.stat().st_size != expected_size:
        raise RuntimeError("ZIP download size verification failed")

    return destination


# ---------------- SAFE EXTRACTION ----------------

def extract_zip(zip_path, destination):
    destination.mkdir(parents=True, exist_ok=True)

    total_size = 0
    file_count = 0

    with zipfile.ZipFile(zip_path) as archive:

        for item in archive.infolist():

            if item.is_dir():
                continue

            name = item.filename.replace("\\", "/")
            parts = Path(name).parts

            if (
                name.startswith("/")
                or ".." in parts
                or (parts and ":" in parts[0])
            ):
                raise ValueError("Unsafe ZIP path")

            # Ignore symbolic links.
            mode = (item.external_attr >> 16) & 0o170000

            if mode == 0o120000:
                continue

            total_size += item.file_size
            file_count += 1

            if total_size > MAX_EXTRACTED:
                raise ValueError("Extraction size limit exceeded")

            if file_count > 100000:
                raise ValueError("Too many files in ZIP")

            target = (destination / name).resolve()

            if not target.is_relative_to(destination.resolve()):
                raise ValueError("Invalid extraction path")

            target.parent.mkdir(parents=True, exist_ok=True)

            with archive.open(item) as src:
                with open(target, "wb") as dst:
                    shutil.copyfileobj(
                        src, dst, length=1024 * 1024
                    )


# ---------------- VIDEO SCAN ----------------

def scan_videos(job_id, folder):

    videos = sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXT
    )

    with connect() as con:
        for video in videos:
            relative = str(video.relative_to(folder))

            con.execute("""
                INSERT OR IGNORE INTO videos(job_id,path,status)
                VALUES(?,?,'pending')
            """, (job_id, relative))

    return len(videos)


# ---------------- VIDEO UPLOAD ----------------

def upload_video(video):

    if video.stat().st_size >= MAX_VIDEO:
        raise ValueError("Video exceeds upload limit")

    with open(video, "rb") as stream:

        encoder = MultipartEncoder(fields={
            "chat_id": CHANNEL,
            "supports_streaming": "true",
            "caption": video.name[:900],
            "video": (
                video.name,
                stream,
                "application/octet-stream"
            )
        })

        response = session.post(
            f"{API}/bot{TOKEN}/sendVideo",
            data=encoder,
            headers={
                "Content-Type": encoder.content_type
            },
            timeout=(60, 3600)
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                result.get("description", "Upload failed")
            )

        return result["result"]


# ---------------- PROCESS JOB ----------------

def process_job(job_id, zip_path):

    folder = EXTRACT_DIR / job_id
    marker = folder / ".complete"

    if not marker.exists():
        send_message("ZIP extraction started.")

        extract_zip(zip_path, folder)
        marker.touch()

    total = scan_videos(job_id, folder)

    send_message(
        f"Video scan complete.\nTotal videos: {total}"
    )

    with connect() as con:
        rows = con.execute("""
            SELECT path FROM videos
            WHERE job_id=? AND status='pending'
            ORDER BY path
        """, (job_id,)).fetchall()

    uploaded = 0
    failed = 0

    for (relative,) in rows:

        video = folder / relative

        if not video.is_file():
            failed += 1
            continue

        success = False

        for attempt in range(3):

            try:
                upload_video(video)

                with connect() as con:
                    con.execute("""
                        UPDATE videos SET status='uploaded'
                        WHERE job_id=? AND path=?
                    """, (job_id, relative))

                uploaded += 1
                success = True

                log.info("Uploaded: %s", relative)

                send_message(
                    f"Uploaded: {uploaded}/{total}\n{video.name}"
                )

                # Avoid sending too many messages too quickly.
                time.sleep(1.1)
                break

            except Exception as error:
                log.exception("Upload attempt failed")

                if attempt < 2:
                    time.sleep(10 * (attempt + 1))

        if not success:
            failed += 1

            with connect() as con:
                con.execute("""
                    UPDATE videos SET status='failed'
                    WHERE job_id=? AND path=?
                """, (job_id, relative))

    with connect() as con:
        remaining = con.execute("""
            SELECT COUNT(*) FROM videos
            WHERE job_id=? AND status!='uploaded'
        """, (job_id,)).fetchone()[0]

        con.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            ("completed" if remaining == 0 else "incomplete", job_id)
        )

    send_message(
        "JOB REPORT\n"
        f"Total videos: {total}\n"
        f"Uploaded: {uploaded}\n"
        f"Failed/pending: {remaining}"
    )


# ---------------- RECOVERY ----------------

def recover_jobs():

    with connect() as con:
        jobs = con.execute("""
            SELECT id,zip_path FROM jobs
            WHERE status!='completed'
        """).fetchall()

    for job_id, zip_path in jobs:

        if Path(zip_path).exists():
            try:
                send_message(f"Resuming job: {job_id}")
                process_job(job_id, Path(zip_path))
            except Exception:
                log.exception("Recovery failed")


# ---------------- UPDATE HANDLER ----------------

def handle_update(update):

    message = update.get("message", {})

    user = message.get("from", {})

    if user.get("id") != OWNER_ID:
        return

    text = message.get("text", "")

    if text == "/start":
        send_message(
            "Bot is ready!\nSend me a ZIP file."
        )
        return

    if text == "/status":
        with connect() as con:
            jobs = con.execute("""
                SELECT status, COUNT(*) FROM jobs
                GROUP BY status
            """).fetchall()

            pending = con.execute("""
                SELECT COUNT(*) FROM videos
                WHERE status!='uploaded'
            """).fetchone()[0]

        send_message(
            f"Jobs: {jobs}\nPending videos: {pending}"
        )
        return

    if text == "/retry":
        recover_jobs()
        return

    document = message.get("document")

    if not document:
        return

    filename = document.get("file_name", "")

    if not filename.lower().endswith(".zip"):
        send_message("Please send a ZIP file.")
        return

    job_id = uuid.uuid4().hex

    try:
        send_message("ZIP download started.")

        zip_path = download_zip(
            document["file_id"],
            job_id,
            document.get("file_size", 0)
        )

        with connect() as con:
            con.execute("""
                INSERT INTO jobs VALUES(?,?,?)
            """, (job_id, str(zip_path), "processing"))

        process_job(job_id, zip_path)

    except Exception as error:
        log.exception("Job failed")

        send_message(
            f"Job failed: {str(error)[:700]}"
        )


# ---------------- MAIN LOOP ----------------

def main():

    init_db()

    send_message("ZIP uploader is online.")

    recover_jobs()

    offset = get_offset()

    log.info("Bot started.")

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

                try:
                    handle_update(update)

                except Exception:
                    log.exception("Update processing failed")

                offset = update["update_id"] + 1
                save_offset(offset)

        except KeyboardInterrupt:
            break

        except Exception:
            log.exception("Polling error")
            time.sleep(5)


if __name__ == "__main__":
    main()
