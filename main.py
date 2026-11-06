import json
import os
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath

import requests

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "bot.sqlite3"
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = os.environ["TELEGRAM_API_ID"]
API_HASH = os.environ["TELEGRAM_API_HASH"]
CHANNEL = os.environ["CHANNEL_USERNAME"]
OWNER_ID = int(os.environ["OWNER_ID"])

LOCAL_API = os.getenv("LOCAL_API", "http://127.0.0.1:8081").rstrip("/")
MAX_ZIP_BYTES = 4 * 1024**3
MAX_VIDEO_BYTES = 2_000_000_000  # Telegram Local Bot API upload limit: 2000 MB
MAX_EXTRACTED_BYTES = int(os.getenv("MAX_EXTRACTED_BYTES", str(50 * 1024**3)))
MAX_ARCHIVE_FILES = int(os.getenv("MAX_ARCHIVE_FILES", "20000"))
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".mpg", ".mpeg",
    ".3gp", ".ts", ".flv", ".wmv", ".mts", ".m2ts", ".vob", ".ogv",
    ".divx", ".mxf"
}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ZipVideoChannelBot/1.0"})


def db():
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "jobs").mkdir(exist_ok=True)
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            update_id INTEGER UNIQUE NOT NULL,
            file_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at INTEGER NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            UNIQUE(job_id, rel_path)
        )""")


def api(method, payload=None, timeout=90):
    url = f"{LOCAL_API}/bot{BOT_TOKEN}/{method}"
    response = SESSION.post(url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"Telegram API error in {method}"))
    return data["result"]


def send_text(chat_id, text):
    # Telegram text messages are limited; split long reports safely.
    for start in range(0, len(text), 3900):
        api("sendMessage", {"chat_id": chat_id, "text": text[start:start + 3900]})


def safe_zip_members(zf):
    total = 0
    members = []
    infos = zf.infolist()
    if len(infos) > MAX_ARCHIVE_FILES:
        raise ValueError(f"Archive mein {len(infos)} entries hain; limit {MAX_ARCHIVE_FILES} hai.")
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        p = PurePosixPath(name)
        if p.is_absolute() or any(part in ("..", "") for part in p.parts):
            raise ValueError(f"Unsafe ZIP path mila: {name[:160]}")
        # ZIP symlinks are not extracted.
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            continue
        total += info.file_size
        if total > MAX_EXTRACTED_BYTES:
            raise ValueError(
                f"Uncompressed data {MAX_EXTRACTED_BYTES // (1024**3)} GB limit se zyada hai."
            )
        members.append(info)
    return members


def extract_job(job):
    job_dir = DATA_DIR / "jobs" / str(job["id"])
    extract_dir = job_dir / "files"
    marker = job_dir / "EXTRACTED.ok"
    if marker.exists() and extract_dir.exists():
        return extract_dir

    if extract_dir.exists():
        import shutil
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    # Local Bot API getFile returns an absolute path when --local is enabled.
    remote = api("getFile", {"file_id": job["file_id"]})
    zip_path = Path(remote["file_path"])
    if not zip_path.is_file():
        raise RuntimeError("Local Bot API ne ZIP ka local file path nahi diya.")
    if zip_path.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("ZIP 4 GB limit se bada hai.")

    with zipfile.ZipFile(zip_path) as zf:
        members = safe_zip_members(zf)
        for info in members:
            target = (extract_dir / PurePosixPath(info.filename)).resolve()
            if not target.is_relative_to(extract_dir.resolve()):
                raise ValueError("ZIP mein unsafe file path mila.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)

    found = []
    for path in extract_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            found.append(path)

    with db() as con:
        for path in found:
            rel = path.relative_to(extract_dir).as_posix()
            con.execute(
                "INSERT OR IGNORE INTO videos(job_id, rel_path) VALUES (?, ?)",
                (job["id"], rel)
            )
    marker.write_text("ok", encoding="utf-8")
    return extract_dir


def probe_video_codec(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of",
         "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip().lower() if result.returncode == 0 else ""


def prepare_video(source):
    """
    Return (upload_path, temporary_output_path_or_None).
    Telegram's video player reliably supports MP4/H.264; other containers/codecs
    are converted to MP4/H.264 so they are sent as playable videos, not documents.
    """
    codec = probe_video_codec(source)
    if source.suffix.lower() in {".mp4", ".m4v"} and codec == "h264":
        return source, None

    output = source.with_name(source.name + ".telegram-ready.mp4")
    if output.exists():
        output.unlink()

    cmd = [
        "ffmpeg", "-nostdin", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output.exists():
        detail = (result.stderr or "ffmpeg conversion failed")[-1200:]
        raise RuntimeError(f"Video MP4 conversion failed: {detail}")
    if output.stat().st_size > MAX_VIDEO_BYTES:
        output.unlink(missing_ok=True)
        raise ValueError("Converted MP4 2000 MB limit se bada ho gaya.")
    return output, output


def upload_video(path, caption):
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError("Video 2000 MB upload limit se bada hai.")
    # Local Bot API accepts file:// paths, avoiding a second huge multipart copy.
    file_uri = path.resolve().as_uri()
    return api(
        "sendVideo",
        {
            "chat_id": CHANNEL,
            "video": file_uri,
            "caption": caption[:900],
            "supports_streaming": True
        },
        timeout=3600
    )


def process_job(job_id, notify_chat=None):
    with db() as con:
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return
        con.execute("UPDATE jobs SET status='processing' WHERE id=?", (job_id,))

    try:
        extract_dir = extract_job(job)
    except Exception as exc:
        with db() as con:
            con.execute("UPDATE jobs SET status='failed' WHERE id=?", (job_id,))
        if notify_chat:
            send_text(notify_chat, f"ZIP #{job_id} extract nahi hui: {str(exc)[:1200]}")
        return

    with db() as con:
        rows = con.execute(
            "SELECT * FROM videos WHERE job_id=? AND status IN ('pending','sending') ORDER BY id",
            (job_id,)
        ).fetchall()
        total = con.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE job_id=?", (job_id,)
        ).fetchone()["n"]

    if total == 0:
        with db() as con:
            con.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
        if notify_chat:
            send_text(notify_chat, f"ZIP #{job_id} mein supported video files nahi mili.")
        return

    for row in rows:
        source = extract_dir / row["rel_path"]
        if not source.is_file():
            with db() as con:
                con.execute(
                    "UPDATE videos SET status='failed', error=? WHERE id=?",
                    ("Extracted video file missing", row["id"])
                )
            continue

        with db() as con:
            con.execute(
                "UPDATE videos SET status='sending', attempts=attempts+1, error=NULL WHERE id=?",
                (row["id"],)
            )

        temp_path = None
        try:
            upload_path, temp_path = prepare_video(source)
            upload_video(upload_path, source.name)
            with db() as con:
                con.execute("UPDATE videos SET status='sent', error=NULL WHERE id=?", (row["id"],))
        except Exception as exc:
            with db() as con:
                con.execute(
                    "UPDATE videos SET status='failed', error=? WHERE id=?",
                    (str(exc)[:1500], row["id"])
                )
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

    with db() as con:
        counts = {
            r["status"]: r["n"]
            for r in con.execute(
                "SELECT status, COUNT(*) AS n FROM videos WHERE job_id=? GROUP BY status",
                (job_id,)
            ).fetchall()
        }
        failed = counts.get("failed", 0)
        sent = counts.get("sent", 0)
        con.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            ("done" if failed == 0 and sent == total else "done_with_errors", job_id)
        )

    if notify_chat:
        send_text(
            notify_chat,
            f"ZIP #{job_id} processing report\n"
            f"Total videos: {total}\n"
            f"Uploaded: {sent}\n"
            f"Failed: {failed}\n"
            + ("Failed videos ke liye /retry bhejo." if failed else "Saari videos upload ho gayi.")
        )


def get_owner_chat(update):
    message = update.get("message") or update.get("channel_post") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if sender.get("id") != OWNER_ID or chat.get("type") != "private":
        return None
    return chat.get("id")


def handle_update(update):
    chat_id = get_owner_chat(update)
    if not chat_id:
        return

    message = update.get("message", {})
    text_value = (message.get("text") or "").strip()

    if text_value.startswith("/start") or text_value.startswith("/help"):
        send_text(
            chat_id,
            "ZIP Video Uploader ready.\n\n"
            "4 GB tak ki ZIP file yahan Document ke roop mein bhejo.\n"
            "Bot ZIP ke andar ki videos nikaal kar channel par Telegram video player format mein upload karega.\n\n"
            "/status - current jobs ka status\n"
            "/retry - failed videos dobara bhejo"
        )
        return

    if text_value.startswith("/status"):
        with db() as con:
            jobs = con.execute(
                "SELECT id, file_name, status FROM jobs ORDER BY id DESC LIMIT 10"
            ).fetchall()
            counts = con.execute(
                "SELECT status, COUNT(*) AS n FROM videos GROUP BY status"
            ).fetchall()
        lines = ["Recent jobs:"]
        lines += [f"#{r['id']} | {r['status']} | {r['file_name']}" for r in jobs]
        lines += ["", "Video totals:"] + [f"{r['status']}: {r['n']}" for r in counts]
        send_text(chat_id, "\n".join(lines))
        return

    if text_value.startswith("/retry"):
        with db() as con:
            con.execute("UPDATE videos SET status='pending' WHERE status='failed'")
            con.execute(
                "UPDATE jobs SET status='processing' WHERE id IN "
                "(SELECT DISTINCT job_id FROM videos WHERE status='pending')"
            )
            job_ids = [
                r["id"] for r in con.execute(
                    "SELECT DISTINCT job_id AS id FROM videos WHERE status='pending' ORDER BY id"
                ).fetchall()
            ]
        if not job_ids:
            send_text(chat_id, "Retry ke liye koi failed video nahi hai.")
            return
        send_text(chat_id, f"{len(job_ids)} job(s) retry ho rahi hain.")
        for jid in job_ids:
            process_job(jid, chat_id)
        return

    document = message.get("document")
    if not document:
        send_text(chat_id, "ZIP archive ko Telegram mein Document/File ke roop mein bhejo.")
        return

    file_name = document.get("file_name", "archive.zip")
    if not file_name.lower().endswith(".zip"):
        send_text(chat_id, "Abhi sirf .zip archive supported hai.")
        return
    file_size = int(document.get("file_size", 0))
    if file_size > MAX_ZIP_BYTES:
        send_text(chat_id, "ZIP 4 GB limit se badi hai.")
        return

    update_id = int(update["update_id"])
    with db() as con:
        existing = con.execute("SELECT id FROM jobs WHERE update_id=?", (update_id,)).fetchone()
        if existing:
            job_id = existing["id"]
        else:
            cur = con.execute(
                "INSERT INTO jobs(update_id,file_id,file_name,file_size,status,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (update_id, document["file_id"], file_name, file_size, "queued", int(time.time()))
            )
            job_id = cur.lastrowid

    send_text(chat_id, f"ZIP #{job_id} mil gayi: {file_name}\nProcessing shuru.")
    process_job(job_id, chat_id)


def recover_jobs():
    with db() as con:
        ids = [
            r["id"] for r in con.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','processing') ORDER BY id"
            ).fetchall()
        ]
        # A crash after Telegram accepted a video but before SQLite recorded success
        # can cause that one video to be sent again. This cannot be made exactly-once
        # with the Bot API because sendVideo has no idempotency key.
        con.execute("UPDATE videos SET status='pending' WHERE status='sending'")
    for jid in ids:
        process_job(jid, OWNER_ID)


def main():
    init_db()
    print("Bot starting; local Bot API:", LOCAL_API, flush=True)
    recover_jobs()

    with db() as con:
        row = con.execute("SELECT value FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        offset_row = con.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        offset = int(offset_row["value"]) if offset_row else 0

    while True:
        try:
            updates = api("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message"]}, timeout=70)
            for update in updates:
                try:
                    handle_update(update)
                except Exception as exc:
                    print("Update handling error:", repr(exc), flush=True)
                    chat_id = get_owner_chat(update)
                    if chat_id:
                        try:
                            send_text(chat_id, f"Error: {str(exc)[:1000]}")
                        except Exception:
                            pass
                offset = int(update["update_id"]) + 1
                with db() as con:
                    con.execute(
                        "INSERT INTO settings(key,value) VALUES('offset',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(offset),)
                    )
        except Exception as exc:
            print("Polling error:", repr(exc), flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
