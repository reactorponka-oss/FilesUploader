
import os
import re
import time
import json
import shutil
import sqlite3
import subprocess
import requests

from pathlib import Path, PurePosixPath
from contextlib import contextmanager


# ================= CONFIG =================

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_USERNAME = os.environ["CHANNEL_USERNAME"]

LOCAL_API = os.getenv("LOCAL_API", "http://127.0.0.1:8081").rstrip("/")
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))

WORK_DIR = DATA_DIR / "work"
EXTRACT_DIR = WORK_DIR / "extracted"
DB_PATH = DATA_DIR / "bot.db"

MAX_ZIP_SIZE = int(os.getenv("MAX_ZIP_SIZE", str(4 * 1024**3)))
MAX_EXTRACT_SIZE = int(os.getenv("MAX_EXTRACT_SIZE", str(50 * 1024**3)))
MAX_FILES = int(os.getenv("MAX_FILES", "20000"))
MAX_VIDEO_SIZE = 2_000_000_000

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".m4v", ".ts", ".mpg", ".mpeg", ".3gp",
    ".wmv", ".flv", ".vob"
}

ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar",
    ".gz", ".bz2", ".xz",
    ".tgz", ".tbz", ".tbz2", ".txz"
}

for folder in (DATA_DIR, WORK_DIR, EXTRACT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

session = requests.Session()


# ================= DATABASE =================

@contextmanager
def conn():
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
                error TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        db.execute("""
            INSERT OR IGNORE INTO settings(key,value)
            VALUES('offset','0')
        """)


# ================= TELEGRAM API =================

def api(method, payload=None, timeout=300):
    url = f"{LOCAL_API}/bot{BOT_TOKEN}/{method}"

    try:
        response = session.post(
            url,
            json=payload or {},
            timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{method} connection error: {exc}")

    if not data.get("ok"):
        raise RuntimeError(
            f"{method}: {data.get('description', 'Unknown Telegram error')}"
        )

    return data.get("result")


def say(chat_id, message):
    try:
        api("sendMessage", {
            "chat_id": chat_id,
            "text": str(message)[:4000]
        })
    except Exception as exc:
        print("Message error:", exc)


def is_owner(message):
    chat = message.get("chat", {})
    return (
        chat.get("type") == "private"
        and chat.get("id") == OWNER_ID
    )


# ================= 7-ZIP ENGINE =================

def run_7z(args, timeout=3600):
    command = ["7z"] + args

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout
    )

    return result


def archive_test(path):
    """
    Detect archive by content, not by extension.
    7-Zip supports ZIP, RAR, 7Z, TAR and many compressed formats.
    """

    result = run_7z(["t", "-y", str(path)], timeout=3600)

    if result.returncode != 0:
        detail = (result.stdout + "\n" + result.stderr)[-1800:]

        raise RuntimeError(
            "7-Zip archive test failed. File corrupt, incomplete, "
            "encrypted, unsupported, or not an archive.\n"
            + detail
        )

    return True


def archive_listing(path):
    """
    Read archive entries before extraction.
    Reject unsafe paths and excessive uncompressed sizes.
    """

    result = run_7z(["l", "-slt", str(path)], timeout=1800)

    if result.returncode != 0:
        raise RuntimeError(
            "Archive listing failed: " + result.stderr[-1000:]
        )

    text = result.stdout
    if "----------" not in text:
        raise RuntimeError("Archive listing format unexpected.")

    body = text.split("----------", 1)[1]

    total_size = 0
    file_count = 0

    for block in body.split("\n\n"):
        values = {}

        for line in block.splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
                values[key.strip()] = value.strip()

        member = values.get("Path")
        if not member:
            continue

        # Ignore the archive's own metadata entry if present.
        pure = PurePosixPath(member.replace("\\", "/"))

        if pure.is_absolute() or ".." in pure.parts:
            raise RuntimeError(
                f"Unsafe archive path detected: {member}"
            )

        if values.get("Folder") == "+":
            continue

        file_count += 1

        try:
            total_size += int(values.get("Size", "0"))
        except ValueError:
            pass

        if file_count > MAX_FILES:
            raise RuntimeError(
                f"Archive has more than {MAX_FILES} files."
            )

        if total_size > MAX_EXTRACT_SIZE:
            raise RuntimeError(
                "Archive extracted size limit exceeded: "
                f"{total_size:,} bytes."
            )

    return file_count, total_size


def extract_with_7z(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)

    result = run_7z(
        [
            "x",
            "-y",
            "-aoa",
            f"-o{destination}",
            str(archive)
        ],
        timeout=24 * 60 * 60
    )

    if result.returncode != 0:
        detail = (result.stdout + "\n" + result.stderr)[-1800:]

        raise RuntimeError(
            "7-Zip extraction failed:\n" + detail
        )


def expand_nested_archives(root, depth=0):
    """
    Extract nested archives such as TAR inside GZ.
    Limit recursion to avoid endless nesting.
    """

    if depth >= 5:
        return

    candidates = []

    for item in root.rglob("*"):
        if not item.is_file():
            continue

        if item.suffix.lower() in ARCHIVE_EXTENSIONS:
            candidates.append(item)

    for archive in candidates:
        if not archive.exists():
            continue

        try:
            archive_test(archive)

            subdir = archive.parent / (
                archive.name + "_contents"
            )

            if subdir.exists():
                shutil.rmtree(subdir)

            archive_listing(archive)
            extract_with_7z(archive, subdir)

            # Remove nested archive only after successful extraction.
            archive.unlink(missing_ok=True)

            expand_nested_archives(subdir, depth + 1)

        except Exception as exc:
            print(
                "Nested archive skipped:",
                archive,
                str(exc)[:500]
            )


# ================= DOWNLOAD / VERIFY =================

def get_archive(job):
    file_id = job["file_id"]
    expected_size = int(job["file_size"] or 0)

    if expected_size > MAX_ZIP_SIZE:
        raise RuntimeError(
            f"Archive too large: {expected_size:,} bytes."
        )

    last_error = None

    for attempt in range(1, 4):
        try:
            result = api(
                "getFile",
                {"file_id": file_id},
                timeout=1800
            )

            file_path = result.get("file_path")

            if not file_path:
                raise RuntimeError("Telegram file_path missing.")

            archive = Path(file_path)

            if not archive.is_absolute():
                possibilities = [
                    Path.cwd() / archive,
                    DATA_DIR / archive,
                    DATA_DIR / "botapi" / archive
                ]

                archive = next(
                    (p for p in possibilities if p.is_file()),
                    archive
                )

            if not archive.is_file():
                raise RuntimeError(
                    f"Local file not found: {archive}"
                )

            actual_size = archive.stat().st_size

            print(
                f"ARCHIVE DEBUG attempt={attempt} "
                f"path={archive} "
                f"actual={actual_size} expected={expected_size}"
            )

            if actual_size <= 0:
                raise RuntimeError("Downloaded archive is empty.")

            if actual_size > MAX_ZIP_SIZE:
                raise RuntimeError("Downloaded archive exceeds size limit.")

            if expected_size and actual_size != expected_size:
                raise RuntimeError(
                    f"File size mismatch: expected {expected_size}, "
                    f"received {actual_size}."
                )

            # Do not force ZIP signature validation.
            # 7-Zip detects the real format itself.
            archive_test(archive)
            archive_listing(archive)

            return archive

        except Exception as exc:
            last_error = exc
            print(f"Archive attempt {attempt} failed:", exc)

            if attempt < 3:
                time.sleep(attempt * 3)

    raise RuntimeError(
        f"Archive download/validation failed after 3 attempts: {last_error}"
    )


# ================= EXTRACTION =================

def extract_job(job_id, archive):
    target = EXTRACT_DIR / str(job_id)

    if target.exists():
        shutil.rmtree(target)

    target.mkdir(parents=True, exist_ok=True)

    try:
        count, size = archive_listing(archive)

        print(
            f"Archive entries={count}, "
            f"uncompressed_size={size}"
        )

        extract_with_7z(archive, target)

        # Handle nested TAR.GZ, TAR.BZ2 and similar archives.
        expand_nested_archives(target)

        files = [
            p for p in target.rglob("*")
            if p.is_file()
        ]

        total_size = sum(p.stat().st_size for p in files)

        if len(files) > MAX_FILES:
            raise RuntimeError("Extracted file count limit exceeded.")

        if total_size > MAX_EXTRACT_SIZE:
            raise RuntimeError("Extracted size limit exceeded.")

        videos = [
            p for p in files
            if p.suffix.lower() in VIDEO_EXTENSIONS
        ]

        if not videos:
            raise RuntimeError(
                "Archive extracted, but no supported video files found."
            )

        return videos

    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


# ================= VIDEO CONVERSION =================

def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "json",
            str(path)
        ],
        capture_output=True,
        text=True,
        timeout=180
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FFprobe failed: " + result.stderr[-1000:]
        )

    streams = json.loads(result.stdout or "{}").get("streams", [])

    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None
    )

    if not video:
        raise RuntimeError("No video stream found.")

    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"),
        None
    )

    return video.get("codec_name"), (
        audio.get("codec_name") if audio else None
    )


def make_mp4(path):
    video_codec, audio_codec = probe_video(path)

    if (
        path.suffix.lower() == ".mp4"
        and video_codec == "h264"
        and audio_codec in (None, "aac")
    ):
        if path.stat().st_size > MAX_VIDEO_SIZE:
            raise RuntimeError("Video exceeds Telegram 2000MB limit.")

        return path, None

    output = path.with_name(path.stem + "_telegram.mp4")

    result = subprocess.run(
        [
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
        ],
        capture_output=True,
        text=True,
        timeout=24 * 60 * 60
    )

    if result.returncode != 0 or not output.is_file():
        raise RuntimeError(
            "FFmpeg conversion failed: " + result.stderr[-1500:]
        )

    if output.stat().st_size > MAX_VIDEO_SIZE:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "Converted video exceeds Telegram 2000MB limit."
        )

    return output, output


# ================= CHANNEL UPLOAD =================

def send_video(path):
    if path.stat().st_size > MAX_VIDEO_SIZE:
        raise RuntimeError("Video exceeds 2000MB upload limit.")

    return api(
        "sendVideo",
        {
            "chat_id": CHANNEL_USERNAME,
            "video": path.resolve().as_uri(),
            "supports_streaming": True,
            "caption": path.name[:900]
        },
        timeout=24 * 60 * 60
    )


# ================= JOB PROCESSING =================

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
        say(chat_id, f"Job #{job_id} not found.")
        return

    update_job(job_id, "processing")

    say(
        chat_id,
        f"Job #{job_id} started.\n"
        f"File: {job['file_name']}"
    )

    try:
        with conn() as db:
            existing = db.execute(
                "SELECT COUNT(*) AS n FROM videos WHERE job_id=?",
                (job_id,)
            ).fetchone()["n"]

        # Keep existing video records on retry.
        # This prevents already-uploaded videos from being sent again.
        if not existing:
            archive = get_archive(job)

            say(
                chat_id,
                "Archive detected and verified. Extraction starting."
            )

            video_paths = extract_job(job_id, archive)

            with conn() as db:
                for path in video_paths:
                    db.execute(
                        """
                        INSERT INTO videos(job_id,path,status)
                        VALUES(?,?,'pending')
                        """,
                        (job_id, str(path))
                    )

        with conn() as db:
            videos = db.execute(
                """
                SELECT * FROM videos
                WHERE job_id=? AND status!='sent'
                ORDER BY id
                """,
                (job_id,)
            ).fetchall()

        total = len(videos)
        sent = 0
        failed = 0

        say(chat_id, f"Uploading {total} remaining videos to channel.")

        for index, video in enumerate(videos, start=1):
            video_id = video["id"]
            source = Path(video["path"])
            converted = None

            try:
                if not source.is_file():
                    raise RuntimeError("Extracted video file is missing.")

                with conn() as db:
                    db.execute(
                        "UPDATE videos SET status='sending' WHERE id=?",
                        (video_id,)
                    )

                upload_path, converted = make_mp4(source)

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
                print(f"Uploaded {index}/{total}: {source.name}")

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

                print("Video failed:", source.name, exc)

            finally:
                if converted and converted.exists():
                    converted.unlink(missing_ok=True)

        with conn() as db:
            remaining = db.execute(
                """
                SELECT COUNT(*) AS n FROM videos
                WHERE job_id=? AND status!='sent'
                """,
                (job_id,)
            ).fetchone()["n"]

        final_status = "completed" if remaining == 0 else "completed_with_errors"
        update_job(job_id, final_status)

        say(
            chat_id,
            f"Job #{job_id} finished.\n\n"
            f"Attempted: {total}\n"
            f"Uploaded now: {sent}\n"
            f"Failed now: {failed}\n"
            f"Still remaining: {remaining}"
        )

    except Exception as exc:
        update_job(job_id, "failed", str(exc))

        print(f"JOB #{job_id} ERROR:", exc)

        say(
            chat_id,
            f"Job #{job_id} process nahi hui.\n\n"
            f"Reason:\n{str(exc)[:3000]}"
        )


# ================= MESSAGE HANDLER =================

def handle(message):
    if not is_owner(message):
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/start"):
        say(
            chat_id,
            "Bot ready!\n\n"
            "ZIP, RAR, 7Z, TAR, GZ, BZ2, XZ archives bhejo.\n"
            "Main videos extract karke channel par playable videos upload karunga.\n\n"
            "/status - Recent jobs\n"
            "/retry JOB_ID - Failed job retry"
        )
        return

    if text.startswith("/status"):
        with conn() as db:
            jobs = db.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 10"
            ).fetchall()

        if not jobs:
            say(chat_id, "No jobs yet.")
            return

        lines = ["Recent jobs:"]

        for job in jobs:
            lines.append(
                f"#{job['id']} | {job['file_name']} | {job['status']}"
            )

            if job["error"]:
                lines.append(f"Error: {job['error'][:250]}")

        say(chat_id, "\n".join(lines))
        return

    if text.startswith("/retry"):
        parts = text.split()

        if len(parts) != 2 or not parts[1].isdigit():
            say(chat_id, "Usage: /retry JOB_ID")
            return

        job_id = int(parts[1])

        with conn() as db:
            job = db.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,)
            ).fetchone()

            if not job:
                say(chat_id, "Job not found.")
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
        file_id = document.get("file_id")
        file_name = document.get("file_name", "archive")
        file_size = int(document.get("file_size", 0))

        if file_size > MAX_ZIP_SIZE:
            say(chat_id, "Archive size exceeds configured limit.")
            return

        if not file_id:
            say(chat_id, "Document file_id missing.")
            return

        with conn() as db:
            cursor = db.execute(
                """
                INSERT INTO jobs(
                    file_id,file_name,file_size,status,created_at
                )
                VALUES(?,?,?,'pending',?)
                """,
                (file_id, file_name, file_size, int(time.time()))
            )

            job_id = cursor.lastrowid

        process_job(job_id, chat_id)
        return

    if text.startswith("/"):
        say(chat_id, "Unknown command. Send /start.")
        return

    say(chat_id, "Archive ko Telegram document ke roop mein bhejo.")


# ================= RECOVERY =================

def recover():
    with conn() as db:
        # A restart during sendVideo can leave uncertainty about whether
        # Telegram accepted the upload. Do not automatically resend it.
        db.execute(
            """
            UPDATE videos
            SET status='failed',
                error='Bot restarted during upload; check channel before retry.'
            WHERE status='sending'
            """
        )

        jobs = db.execute(
            """
            SELECT id FROM jobs
            WHERE status IN ('pending','processing')
            ORDER BY id
            """
        ).fetchall()

    for row in jobs:
        try:
            process_job(row["id"], OWNER_ID)
        except Exception as exc:
            print("Recovery error:", exc)


# ================= MAIN =================

def main():
    init_db()

    print("Local Bot API:", LOCAL_API)
    print("Starting bot...")

    me = api("getMe")
    print("Connected as:", me.get("username"))

    try:
        api("getChat", {"chat_id": CHANNEL_USERNAME})
        print("Channel access OK:", CHANNEL_USERNAME)
    except Exception as exc:
        print("Channel check warning:", exc)

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
                    print("Update error:", exc)

                offset = update_id + 1

                with conn() as db:
                    db.execute(
                        """
                        INSERT INTO settings(key,value)
                        VALUES('offset',?)
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
