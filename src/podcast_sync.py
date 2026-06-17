from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from xml.etree import ElementTree

import boto3
import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"


@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    channel_id: str
    handle_url: str
    bootstrap_days: int

    @property
    def long_form_feed_url(self) -> str:
        playlist_id = "UULF" + self.channel_id[2:]
        return f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"

    @property
    def channel_feed_url(self) -> str:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={self.channel_id}"

    @property
    def videos_tab_url(self) -> str:
        return self.handle_url.rstrip("/") + "/videos"


@dataclass
class Video:
    channel: Channel
    video_id: str
    title: str
    youtube_url: str
    published_at: datetime | None = None
    duration: int | None = None


CHANNELS = [
    Channel(
        key="bafenban",
        name="八分半",
        channel_id="UCxVq1aJ2LgAZocJm-WeXY9Q",
        handle_url="https://www.youtube.com/@8fenban",
        bootstrap_days=30,
    ),
    Channel(
        key="nana",
        name="NaNa说美股",
        channel_id="UCFhJ8ZFg9W4kLwFTBBNIjOw",
        handle_url="https://www.youtube.com/@NaNaShuoMeiGu",
        bootstrap_days=3,
    ),
]


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_filename(value: str, max_len: int = 90) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value, flags=re.UNICODE)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    return cleaned[:max_len] or "audio"


def run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def yt_dlp_args() -> list[str]:
    override = os.getenv("YT_DLP_BIN")
    if override:
        return [override]
    return [sys.executable, "-m", "yt_dlp"]


def yt_dlp_common_args() -> list[str]:
    args: list[str] = []
    js_runtimes = env("YTDLP_JS_RUNTIMES", "node" if shutil.which("node") else "")
    if js_runtimes:
        args.extend(["--js-runtimes", js_runtimes])

    remote_components = env("YTDLP_REMOTE_COMPONENTS", "ejs:github")
    if remote_components:
        args.extend(["--remote-components", remote_components])

    cookies = env("YTDLP_COOKIES")
    if cookies:
        cookie_path = Path(cookies)
        args.extend(["--cookies", str(cookie_path if cookie_path.is_absolute() else ROOT / cookie_path)])

    cookies_from_browser = env("YTDLP_COOKIES_FROM_BROWSER")
    if cookies_from_browser:
        args.extend(["--cookies-from-browser", cookies_from_browser])
    return args


def find_channel(key: str) -> Channel:
    for channel in CHANNELS:
        if channel.key == key:
            return channel
    valid = ", ".join(channel.key for channel in CHANNELS)
    raise RuntimeError(f"Unknown channel: {key}. Valid channels: {valid}")


def extract_video_id(url: str) -> str:
    patterns = [
        r"[?&]v=([0-9A-Za-z_-]{6,})",
        r"youtu\.be/([0-9A-Za-z_-]{6,})",
        r"/shorts/([0-9A-Za-z_-]{6,})",
        r"/live/([0-9A-Za-z_-]{6,})",
        r"/embed/([0-9A-Za-z_-]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return safe_filename(url, max_len=120)


def connect_db() -> sqlite3.Connection:
    db_path = ROOT / env("PODCAST_DB_PATH", "data/podcasts.sqlite3")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            channel_key TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            title TEXT NOT NULL,
            youtube_url TEXT NOT NULL,
            published_at TEXT,
            duration INTEGER,
            status TEXT NOT NULL,
            audio_key TEXT,
            public_url TEXT,
            processed_at TEXT,
            notified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_state (
            channel_key TEXT PRIMARY KEY,
            bootstrapped_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def is_bootstrapped(conn: sqlite3.Connection, channel: Channel) -> bool:
    row = conn.execute(
        "SELECT bootstrapped_at FROM channel_state WHERE channel_key = ?",
        (channel.key,),
    ).fetchone()
    return bool(row and row["bootstrapped_at"])


def mark_bootstrapped(conn: sqlite3.Connection, channel: Channel) -> None:
    conn.execute(
        """
        INSERT INTO channel_state(channel_key, bootstrapped_at)
        VALUES(?, ?)
        ON CONFLICT(channel_key) DO UPDATE SET bootstrapped_at = excluded.bootstrapped_at
        """,
        (channel.key, utc_now().isoformat()),
    )
    conn.commit()


def already_seen(conn: sqlite3.Connection, video_id: str) -> bool:
    row = conn.execute("SELECT status FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if row is None:
        return False
    return row["status"] not in {"failed", "failed_metadata", "notification_failed", "processing"}


def existing_video(conn: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()


def upsert_video(conn: sqlite3.Connection, video: Video, status: str) -> None:
    now = utc_now().isoformat()
    conn.execute(
        """
        INSERT INTO videos(
            video_id, channel_key, channel_name, title, youtube_url, published_at,
            duration, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            title = excluded.title,
            published_at = COALESCE(excluded.published_at, videos.published_at),
            duration = COALESCE(excluded.duration, videos.duration),
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            video.video_id,
            video.channel.key,
            video.channel.name,
            video.title,
            video.youtube_url,
            video.published_at.isoformat() if video.published_at else None,
            video.duration,
            status,
            now,
            now,
        ),
    )
    conn.commit()


def mark_uploaded(conn: sqlite3.Connection, video: Video, audio_key: str, public_url: str) -> None:
    now = utc_now().isoformat()
    conn.execute(
        """
        UPDATE videos
        SET status = 'uploaded',
            audio_key = ?,
            public_url = ?,
            processed_at = ?,
            updated_at = ?
        WHERE video_id = ?
        """,
        (audio_key, public_url, now, now, video.video_id),
    )
    conn.commit()


def mark_notified(conn: sqlite3.Connection, video_id: str) -> None:
    now = utc_now().isoformat()
    conn.execute(
        "UPDATE videos SET notified_at = ?, updated_at = ? WHERE video_id = ?",
        (now, now, video_id),
    )
    conn.commit()


def mark_status(conn: sqlite3.Connection, video: Video, status: str) -> None:
    conn.execute(
        "UPDATE videos SET status = ?, updated_at = ? WHERE video_id = ?",
        (status, utc_now().isoformat(), video.video_id),
    )
    conn.commit()


def mark_status_by_id(conn: sqlite3.Connection, video_id: str, status: str) -> None:
    conn.execute(
        "UPDATE videos SET status = ?, updated_at = ? WHERE video_id = ?",
        (status, utc_now().isoformat(), video_id),
    )
    conn.commit()


def fetch_feed_videos(channel: Channel) -> list[Video]:
    last_error: Exception | None = None
    response_text = ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        )
    }
    for feed_url in (channel.long_form_feed_url, channel.channel_feed_url):
        for attempt in range(3):
            try:
                response = requests.get(feed_url, timeout=30, headers=headers)
                response.raise_for_status()
                response_text = response.text
                break
            except requests.HTTPError as exc:
                last_error = exc
                if response.status_code == 404 and "playlist_id=" in feed_url:
                    break
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                print(f"[WARN] feed unavailable for {channel.name}: {feed_url} ({exc})")
        if response_text:
            break
    if not response_text:
        raise last_error or RuntimeError(f"No feed available for {channel.name}")

    root = ElementTree.fromstring(response_text)
    videos: list[Video] = []
    for entry in root.findall(f"{ATOM}entry"):
        video_id = entry.findtext(f"{YT}videoId")
        title = entry.findtext(f"{ATOM}title") or entry.findtext(f".//{MEDIA}title") or ""
        link = entry.find(f"{ATOM}link")
        href = link.attrib.get("href") if link is not None else None
        published = parse_dt(entry.findtext(f"{ATOM}published"))
        if video_id and href:
            videos.append(
                Video(
                    channel=channel,
                    video_id=video_id,
                    title=html.unescape(title),
                    youtube_url=href,
                    published_at=published,
                )
            )
    return videos


def fetch_bootstrap_videos(channel: Channel) -> list[Video]:
    cutoff = utc_now() - timedelta(days=channel.bootstrap_days)
    if env("BOOTSTRAP_SOURCE", "rss").lower() == "rss":
        return [
            video
            for video in fetch_feed_videos(channel)
            if not video.published_at or video.published_at >= cutoff
        ]

    limit = int(env("BOOTSTRAP_PLAYLIST_LIMIT", "120"))
    args = [
        *yt_dlp_args(),
        *yt_dlp_common_args(),
        "--ignore-errors",
        "--dump-json",
        "--playlist-end",
        str(limit),
        "--dateafter",
        cutoff.strftime("%Y%m%d"),
        channel.videos_tab_url,
    ]
    result = run_command(args)
    videos = parse_yt_dlp_json_lines(result.stdout, channel)
    if result.returncode != 0 and not videos:
        print(f"[WARN] bootstrap listing failed for {channel.name}: {result.stderr.strip()}")
        return [v for v in fetch_feed_videos(channel) if not v.published_at or v.published_at >= cutoff]
    return videos


def parse_yt_dlp_json_lines(output: str, channel: Channel) -> list[Video]:
    videos: list[Video] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = data.get("id")
        title = data.get("title") or video_id or ""
        url = data.get("url") or data.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
        if video_id:
            videos.append(
                Video(
                    channel=channel,
                    video_id=video_id,
                    title=html.unescape(title),
                    youtube_url=url if url.startswith("http") else f"https://www.youtube.com/watch?v={video_id}",
                    published_at=parse_yt_upload_date(data.get("upload_date")),
                    duration=data.get("duration"),
                )
            )
    return videos


def parse_yt_upload_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def enrich_video(video: Video) -> Video:
    args = [*yt_dlp_args(), *yt_dlp_common_args(), "--dump-json", "--skip-download", video.youtube_url]
    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed for {video.youtube_url}: {result.stderr.strip()}")
    data = json.loads(result.stdout.splitlines()[-1])
    return Video(
        channel=video.channel,
        video_id=data.get("id") or video.video_id,
        title=html.unescape(data.get("title") or video.title),
        youtube_url=data.get("webpage_url") or video.youtube_url,
        published_at=parse_yt_upload_date(data.get("upload_date")) or video.published_at,
        duration=data.get("duration") or video.duration,
    )


def download_mp3(video: Video) -> Path:
    download_dir = ROOT / env("PODCAST_DOWNLOAD_DIR", "downloads")
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(download_dir / f"{video.video_id}.%(ext)s")
    quality = env("MP3_AUDIO_QUALITY", "128K").lower().rstrip("k")
    args = [
        *yt_dlp_args(),
        *yt_dlp_common_args(),
        "--no-playlist",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        quality + "K",
        "-o",
        output_template,
        video.youtube_url,
    ]
    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed for {video.youtube_url}: {result.stderr.strip()}")
    mp3_path = download_dir / f"{video.video_id}.mp3"
    if not mp3_path.exists():
        matches = list(download_dir.glob(f"{video.video_id}*.mp3"))
        if not matches:
            raise RuntimeError(f"MP3 file not found after download: {video.video_id}")
        mp3_path = matches[0]
    return mp3_path


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=env("R2_ENDPOINT", required=True),
        aws_access_key_id=env("R2_ACCESS_KEY_ID", required=True),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY", required=True),
        region_name="auto",
    )


def upload_to_r2(mp3_path: Path, video: Video) -> tuple[str, str]:
    bucket = env("R2_BUCKET_NAME", required=True)
    base_url = env("PUBLIC_AUDIO_BASE_URL", required=True).rstrip("/")
    title_part = safe_filename(video.title, max_len=70)
    key = f"{video.channel.key}/{video.video_id}-{title_part}.mp3"
    r2_client().upload_file(
        str(mp3_path),
        bucket,
        key,
        ExtraArgs={"ContentType": "audio/mpeg"},
    )
    return key, f"{base_url}/{quote(key)}"


def notify_telegram(video: Video, public_url: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN", required=True)
    chat_id = env("TELEGRAM_CHAT_ID", required=True)
    published = video.published_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if video.published_at else "未知"
    text = (
        f"频道：{video.channel.name}\n"
        f"标题：{video.title}\n"
        f"发布时间：{published}\n"
        f"MP3：{public_url}"
    )
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    response.raise_for_status()


def collect_candidates(conn: sqlite3.Connection, channel: Channel) -> Iterable[Video]:
    if is_bootstrapped(conn, channel):
        candidates = fetch_feed_videos(channel)
    else:
        candidates = fetch_bootstrap_videos(channel)
    return sorted(
        candidates,
        key=lambda item: item.published_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
    )


def process_video(conn: sqlite3.Connection, video: Video, dry_run: bool = False) -> bool:
    existing = existing_video(conn, video.video_id)
    if existing and existing["public_url"] and not existing["notified_at"]:
        pending_video = Video(
            channel=video.channel,
            video_id=existing["video_id"],
            title=existing["title"],
            youtube_url=existing["youtube_url"],
            published_at=parse_dt(existing["published_at"]),
            duration=existing["duration"],
        )
        if dry_run:
            print(f"[DRY-RUN] would notify pending upload {pending_video.channel.name}: {pending_video.title}")
            return True
        try:
            notify_telegram(pending_video, existing["public_url"])
            mark_notified(conn, pending_video.video_id)
            mark_status_by_id(conn, pending_video.video_id, "uploaded")
            print(f"[OK] notified pending upload {pending_video.channel.name}: {pending_video.title}")
            return True
        except Exception as exc:
            mark_status_by_id(conn, pending_video.video_id, "notification_failed")
            print(f"[WARN] telegram notification failed for {pending_video.video_id}: {exc}")
            return False

    if already_seen(conn, video.video_id):
        return True

    try:
        enriched = enrich_video(video)
    except RuntimeError as exc:
        message = str(exc)
        if "Sign in to confirm" in message or "not a bot" in message:
            raise
        if not dry_run:
            upsert_video(conn, video, "failed_metadata")
        print(f"[SKIP] metadata unavailable for {video.channel.name} {video.title}: {message}")
        return False

    max_seconds = int(env("MAX_VIDEO_SECONDS", "10800"))
    if enriched.duration and enriched.duration > max_seconds:
        if not dry_run:
            upsert_video(conn, enriched, "skipped_too_long")
        print(f"[SKIP] {enriched.channel.name} {enriched.title} duration={enriched.duration}s")
        return True
    min_long_seconds = int(env("MIN_LONG_VIDEO_SECONDS", "181"))
    if enriched.duration and enriched.duration < min_long_seconds:
        if not dry_run:
            upsert_video(conn, enriched, "skipped_short")
        print(f"[SKIP] {enriched.channel.name} {enriched.title} duration={enriched.duration}s")
        return True

    if dry_run:
        print(f"[DRY-RUN] would process {enriched.channel.name}: {enriched.title}")
        return True

    upsert_video(conn, enriched, "processing")
    mp3_path: Path | None = None
    try:
        mp3_path = download_mp3(enriched)
        audio_key, public_url = upload_to_r2(mp3_path, enriched)
        mark_uploaded(conn, enriched, audio_key, public_url)
        try:
            notify_telegram(enriched, public_url)
            mark_notified(conn, enriched.video_id)
            print(f"[OK] {enriched.channel.name}: {enriched.title}")
        except Exception as exc:
            mark_status_by_id(conn, enriched.video_id, "notification_failed")
            print(f"[WARN] uploaded but telegram notification failed for {enriched.video_id}: {exc}")
            return False
        return True
    except Exception:
        mark_status(conn, enriched, "failed")
        raise
    finally:
        if mp3_path and mp3_path.exists():
            mp3_path.unlink()


def notify_pending_uploads(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    rows = conn.execute(
        """
        SELECT *
        FROM videos
        WHERE public_url IS NOT NULL
          AND notified_at IS NULL
          AND status != 'deleted'
        ORDER BY processed_at, created_at
        """
    ).fetchall()
    for row in rows:
        channel = next((item for item in CHANNELS if item.key == row["channel_key"]), None)
        if channel is None:
            channel = Channel(
                key=row["channel_key"],
                name=row["channel_name"],
                channel_id="",
                handle_url="",
                bootstrap_days=0,
            )
        video = Video(
            channel=channel,
            video_id=row["video_id"],
            title=row["title"],
            youtube_url=row["youtube_url"],
            published_at=parse_dt(row["published_at"]),
            duration=row["duration"],
        )
        if dry_run:
            print(f"[DRY-RUN] would notify pending upload {video.channel.name}: {video.title}")
            continue
        try:
            notify_telegram(video, row["public_url"])
            mark_notified(conn, video.video_id)
            mark_status_by_id(conn, video.video_id, "uploaded")
            print(f"[OK] notified pending upload {video.channel.name}: {video.title}")
        except Exception as exc:
            mark_status_by_id(conn, video.video_id, "notification_failed")
            print(f"[WARN] telegram notification failed for {video.video_id}: {exc}")


def cleanup_retention(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    retention_days = int(env("RETENTION_DAYS", "30"))
    cutoff = utc_now() - timedelta(days=retention_days)
    rows = conn.execute(
        """
        SELECT video_id, audio_key, public_url
        FROM videos
        WHERE audio_key IS NOT NULL
          AND processed_at IS NOT NULL
          AND status != 'deleted'
          AND processed_at < ?
        """,
        (cutoff.isoformat(),),
    ).fetchall()
    if not rows:
        return

    bucket = env("R2_BUCKET_NAME", required=True)
    client = r2_client()
    for row in rows:
        print(f"[CLEANUP] deleting {row['audio_key']}")
        if not dry_run:
            client.delete_object(Bucket=bucket, Key=row["audio_key"])
            conn.execute(
                "UPDATE videos SET status = 'deleted', updated_at = ? WHERE video_id = ?",
                (utc_now().isoformat(), row["video_id"]),
            )
            conn.commit()


def check_dependencies() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found. Install it with: brew install ffmpeg")
    result = run_command([*yt_dlp_args(), "--version"])
    if result.returncode != 0:
        raise RuntimeError("yt-dlp is not available. Run: python3 -m pip install -r requirements.txt")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync YouTube long videos to MP3 on Cloudflare R2.")
    parser.add_argument("--dry-run", action="store_true", help="List work without downloading, uploading, or notifying.")
    parser.add_argument("--cleanup-only", action="store_true", help="Only delete expired R2 audio objects.")
    parser.add_argument("--url", action="append", help="Manually process a YouTube video URL, including member-only videos you can access.")
    parser.add_argument("--channel", default="bafenban", help="Channel key for --url uploads. Defaults to bafenban.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    check_dependencies()
    conn = connect_db()

    if args.cleanup_only:
        cleanup_retention(conn, dry_run=args.dry_run)
        return

    notify_pending_uploads(conn, dry_run=args.dry_run)

    if args.url:
        channel = find_channel(args.channel)
        for url in args.url:
            manual_video = Video(
                channel=channel,
                video_id=extract_video_id(url),
                title=url,
                youtube_url=url,
            )
            process_video(conn, manual_video, dry_run=args.dry_run)
        cleanup_retention(conn, dry_run=args.dry_run)
        return

    for channel in CHANNELS:
        print(f"[CHANNEL] {channel.name}")
        completed_without_auth_error = True
        for video in collect_candidates(conn, channel):
            process_video(conn, video, dry_run=args.dry_run)
        if not args.dry_run and completed_without_auth_error and not is_bootstrapped(conn, channel):
            mark_bootstrapped(conn, channel)

    cleanup_retention(conn, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
