
import os
import sqlite3
import time
import zipfile
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import requests

DATA = Path(os.getenv("DATA_DIR", "/app/data"))
DB = DATA / "bot.sqlite3"
TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL = os.environ["CHANNEL_USERNAME"]
LOCAL_API = os.getenv("LOCAL_API", "http://127.0.0.1:8081").rstrip("/")

MAX_ZIP = 4 * 1024**3
MAX_VIDEO = 2_000_000_000
MAX_UNPACKED = int(os.getenv("MAX_EXTRACTED_BYTES", str(50 * 1024**3)))

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm",
    ".mpg", ".mpeg", ".3gp", ".ts", ".flv", ".wmv",
    ".mts", ".m2ts", ".vob", ".ogv", ".divx", ".mxf"
}

S = requests.Session()


def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "jobs").mkdir(exist_ok=True)

    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER UNIQUE NOT NULL,
                file_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS videos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                UNIQUE(job_id, rel_path)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def api(method, payload=None, timeout=90):
    url = f"{LOCAL_API}/bot{TOKEN}/{method}"

    response = S.post(
        url,
        json=payload or {},
        timeout=timeout
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            data.get("description", f"Telegram API error: {method}")
        )

    return data["result"]


def say(chat, message):
    for i in range(0, len(message), 3900):
        api("sendMessage", {
            "chat_id": chat,
            "text": message[i:i + 3900]
        })


def owner_chat(update):
    message = update.get("message", {})
    sender = message.get("from", {})
    chat = message.get("chat", {})

    if (
        sender.get("id") == OWNER_ID
        and chat.get("type") == "private"
    ):
        return chat.get("id")

    return None


def extract(job):
    folder = DATA / "jobs" / str(job["id"])
    output = folder / "files"
    marker = folder / "EXTRACTED.ok"

    if marker.exists() and output.exists():
        return output

    if output.exists():
        shutil.rmtree(output)

    output.mkdir(parents=True, exist_ok=True)

    result = api("getFile", {
        "file_id": job["file_id"]
    })

    archive = Path(result["file_path"])

    if not archive.is_file():
        raise RuntimeError(
            "Local Bot API ZIP ka local file path nahi de paya."
        )

    if archive.stat().st_size > MAX_ZIP:
        raise ValueError("ZIP 4 GB limit se badi hai.")

    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()

        if len(infos) > 20000:
            raise ValueError(
                "ZIP mein 20,000 se zyada files hain."
            )

        total = 0
        safe_files = []

        for info in infos:
            if info.is_dir():
                continue

            name = info.filename.replace("\\", "/")
            p = PurePosixPath(name)

            if p.is_absolute() or ".." in p.parts:
                raise ValueError(
                    "ZIP mein unsafe file path mila."
                )

            mode = (info.external_attr >> 16) & 0o170000

            if mode == 0o120000:
                continue

            total += info.file_size

            if total > MAX_UNPACKED:
                raise ValueError(
                    "ZIP ka extracted size configured limit se zyada hai."
                )

            safe_files.append(info)

        for info in safe_files:
            target = (
                output / PurePosixPath(info.filename)
            ).resolve()

            if not target.is_relative_to(output.resolve()):
                raise ValueError("ZIP mein unsafe path mila.")

            target.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            with z.open(info) as src:
                with target.open("wb") as dst:
                    shutil.copyfileobj(
                        src,
                        dst,
                        length=1024 * 1024
                    )

    videos = [
        p for p in output.rglob("*")
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTS
    ]

    with conn() as c:
        for video in videos:
            relative = video.relative_to(output).as_posix()

            c.execute("""
                INSERT OR IGNORE INTO videos(job_id, rel_path)
                VALUES(?, ?)
            """, (job["id"], relative))

    marker.write_text("ok", encoding="utf-8")

    return output


def make_telegram_mp4(source):
    probe = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source)
        ],
        capture_output=True,
        text=True,
        timeout=60
    )

    codec = (
        probe.stdout.strip().lower()
        if probe.returncode == 0
        else ""
    )

    if (
        source.suffix.lower() in (".mp4", ".m4v")
        and codec == "h264"
        and source.stat().st_size <= MAX_VIDEO
    ):
        return source, None

    destination = source.with_name(
        source.name + ".telegram-ready.mp4"
    )

    destination.unlink(missing_ok=True)

    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(destination)
        ],
        capture_output=True,
        text=True
    )

    if result.returncode != 0 or not destination.exists():
        raise RuntimeError(
            "FFmpeg conversion failed: "
            + (result.stderr or "")[-900:]
        )

    if destination.stat().st_size > MAX_VIDEO:
        destination.unlink(missing_ok=True)

        raise ValueError(
            "Converted video 2000 MB limit se badi ho gayi."
        )

    return destination, destination


def send_video(path, caption):
    if path.stat().st_size > MAX_VIDEO:
        raise ValueError(
            "Video 2000 MB upload limit se badi hai."
        )

    return api(
        "sendVideo",
        {
            "chat_id": CHANNEL,
            "video": path.resolve().as_uri(),
            "caption": caption[:900],
            "supports_streaming": True
        },
        timeout=3600
    )


def process_job(job_id, notify=None):
    with conn() as c:
        job = c.execute(
            "SELECT * FROM jobs WHERE id=?",
            (job_id,)
        ).fetchone()

        if not job:
            return

        c.execute(
            "UPDATE jobs SET status='processing' WHERE id=?",
            (job_id,)
        )

    try:
        folder = extract(job)

    except Exception as e:
        with conn() as c:
            c.execute(
                "UPDATE jobs SET status='failed' WHERE id=?",
                (job_id,)
            )

        if notify:
            say(
                notify,
                f"ZIP #{job_id} extract nahi hui: {str(e)[:1000]}"
            )

        return

    with conn() as c:
        rows = c.execute("""
            SELECT * FROM videos
            WHERE job_id=?
            AND status IN ('pending', 'sending')
            ORDER BY id
        """, (job_id,)).fetchall()

        total = c.execute(
            "SELECT COUNT(*) n FROM videos WHERE job_id=?",
            (job_id,)
        ).fetchone()["n"]

    if total == 0:
        with conn() as c:
            c.execute(
                "UPDATE jobs SET status='done' WHERE id=?",
                (job_id,)
            )

        if notify:
            say(
                notify,
                f"ZIP #{job_id} mein supported video nahi mili."
            )

        return

    for row in rows:
        source = folder / row["rel_path"]

        if not source.is_file():
            with conn() as c:
                c.execute("""
                    UPDATE videos
                    SET status='failed', error=?
                    WHERE id=?
                """, ("Extracted video missing", row["id"]))

            continue

        with conn() as c:
            c.execute("""
                UPDATE videos
                SET status='sending',
                    attempts=attempts+1,
                    error=NULL
                WHERE id=?
            """, (row["id"],))

        temporary = None

        try:
            ready, temporary = make_telegram_mp4(source)

            send_video(ready, source.name)

            with conn() as c:
                c.execute("""
                    UPDATE videos
                    SET status='sent', error=NULL
                    WHERE id=?
                """, (row["id"],))

        except Exception as e:
            with conn() as c:
                c.execute("""
                    UPDATE videos
                    SET status='failed', error=?
                    WHERE id=?
                """, (str(e)[:1400], row["id"]))

        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    with conn() as c:
        counts = {
            row["status"]: row["n"]
            for row in c.execute("""
                SELECT status, COUNT(*) n
                FROM videos
                WHERE job_id=?
                GROUP BY status
            """, (job_id,)).fetchall()
        }

        sent = counts.get("sent", 0)
        failed = counts.get("failed", 0)

        status = (
            "done"
            if sent == total and failed == 0
            else "done_with_errors"
        )

        c.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            (status, job_id)
        )

    if notify:
        say(
            notify,
            f"ZIP #{job_id} report\n"
            f"Total: {total}\n"
            f"Uploaded: {sent}\n"
            f"Failed: {failed}\n"
            + (
                "Failed videos ke liye /retry bhejo."
                if failed
                else "Saari videos upload ho gayi."
            )
        )


def handle(update):
    chat = owner_chat(update)

    if not chat:
        return

    message = update.get("message", {})
    text_value = (
        message.get("text") or ""
    ).strip()

    if text_value.startswith(("/start", "/help")):
        say(
            chat,
            "ZIP Video Bot ready.\n"
            "4 GB tak ki ZIP ko Document/File ke roop mein bhejo.\n"
            "Videos channel par Telegram video player format mein jayengi.\n"
            "/status - status\n"
            "/retry - failed videos dobara bhejo"
        )
        return

    if text_value.startswith("/status"):
        with conn() as c:
            jobs = c.execute("""
                SELECT id, file_name, status
                FROM jobs
                ORDER BY id DESC
                LIMIT 10
            """).fetchall()

            counts = c.execute("""
                SELECT status, COUNT(*) n
                FROM videos
                GROUP BY status
            """).fetchall()

        lines = ["Recent jobs:"]

        lines += [
            f"#{r['id']} | {r['status']} | {r['file_name']}"
            for r in jobs
        ]

        lines += ["", "Video status:"]

        lines += [
            f"{r['status']}: {r['n']}"
            for r in counts
        ]

        say(chat, "\n".join(lines))
        return

    if text_value.startswith("/retry"):
        with conn() as c:
            c.execute("""
                UPDATE videos
                SET status='pending'
                WHERE status='failed'
            """)

            ids = [
                r["job_id"]
                for r in c.execute("""
                    SELECT DISTINCT job_id
                    FROM videos
                    WHERE status='pending'
                    ORDER BY job_id
                """).fetchall()
            ]

        if not ids:
            say(chat, "Retry ke liye koi failed video nahi hai.")
            return

        say(chat, f"{len(ids)} job(s) retry ho rahi hain.")

        for job_id in ids:
            process_job(job_id, chat)

        return

    document = message.get("document")

    if not document:
        say(
            chat,
            "ZIP archive ko Document/File ke roop mein bhejo."
        )
        return

    name = document.get("file_name", "archive.zip")
    size = int(document.get("file_size", 0))

    if not name.lower().endswith(".zip"):
        say(chat, "Sirf .zip archive supported hai.")
        return

    if size > MAX_ZIP:
        say(chat, "ZIP 4 GB limit se badi hai.")
        return

    update_id = int(update["update_id"])

    with conn() as c:
        old = c.execute(
            "SELECT id FROM jobs WHERE update_id=?",
            (update_id,)
        ).fetchone()

        if old:
            job_id = old["id"]

        else:
            cur = c.execute("""
                INSERT INTO jobs(
                    update_id, file_id, file_name,
                    file_size, status, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
            """, (
                update_id,
                document["file_id"],
                name,
                size,
                "queued",
                int(time.time())
            ))

            job_id = cur.lastrowid

    say(
        chat,
        f"ZIP #{job_id} mil gayi: {name}\nProcessing shuru."
    )

    process_job(job_id, chat)


def recover():
    with conn() as c:
        ids = [
            r["id"]
            for r in c.execute("""
                SELECT id FROM jobs
                WHERE status IN ('queued', 'processing')
                ORDER BY id
            """).fetchall()
        ]

        c.execute("""
            UPDATE videos
            SET status='pending'
            WHERE status='sending'
        """)

    for job_id in ids:
        process_job(job_id, OWNER_ID)


def main():
    init_db()

    print(
        "Bot starting; local Bot API:",
        LOCAL_API,
        flush=True
    )

    recover()

    # Correct settings-table lookup; do not query sqlite_master for "value".
    with conn() as c:
        row = c.execute(
            "SELECT value FROM settings WHERE key='offset'"
        ).fetchone()

        offset = int(row["value"]) if row else 0

    while True:
        try:
            updates = api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": ["message"]
                },
                timeout=70
            )

            for update in updates:
                try:
                    handle(update)

                except Exception as e:
                    print(
                        "Update error:",
                        repr(e),
                        flush=True
                    )

                    chat = owner_chat(update)

                    if chat:
                        try:
                            say(
                                chat,
                                f"Error: {str(e)[:900]}"
                            )
                        except Exception:
                            pass

                offset = int(update["update_id"]) + 1

                with conn() as c:
                    c.execute("""
                        INSERT INTO settings(key, value)
                        VALUES('offset', ?)
                        ON CONFLICT(key)
                        DO UPDATE SET value=excluded.value
                    """, (str(offset),))

        except Exception as e:
            print(
                "Polling error:",
                repr(e),
                flush=True
            )

            time.sleep(5)


if __name__ == "__main__":
    main()
 
