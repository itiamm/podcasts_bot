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
import unicodedata
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
CHANNEL_CONFIG_PATH = ROOT / "config" / "channels.json"
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
    language: str = "zh"
    generate_chinese_audio: bool = False
    enabled: bool = True

    @property
    def long_form_feed_url(self) -> str:
        if not self.channel_id.startswith("UC"):
            raise RuntimeError(f"Missing YouTube channel_id for {self.name}")
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


@dataclass
class ChineseAudio:
    title: str
    summary: list[str]
    script: str
    audio_path: Path


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


def display_audio_filename(video: Video, title: str | None = None) -> str:
    value = title or video.title
    date_part = video.published_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y.%m.%d") if video.published_at else utc_now().strftime("%Y.%m.%d")
    # Keep Telegram's visible attachment title readable: no video id, no ASCII slug prefix.
    cleaned = re.sub(r"[A-Za-z0-9_]{6,}", "", value)
    cleaned = re.sub(r"\(?20\d{2}[./-]\d{1,2}[./-]\d{1,2}\)?", "", cleaned)
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    if not re.search(r"[\u4e00-\u9fff]", cleaned):
        cleaned = safe_filename(video.channel.name, max_len=24)

    max_units = int(env("AUDIO_FILENAME_DISPLAY_WIDTH", "34"))
    result = ""
    width = 0
    for char in cleaned:
        char_width = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if width + char_width > max_units:
            break
        result += char
        width += char_width
    result = result.strip("-._") or safe_filename(video.channel.name, max_len=24)
    return f"{result}-{date_part}"


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
    args: list[str] = [
        "--socket-timeout",
        env("YTDLP_SOCKET_TIMEOUT", "20"),
        "--retries",
        env("YTDLP_RETRIES", "3"),
    ]
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


def load_channels() -> list[Channel]:
    if not CHANNEL_CONFIG_PATH.exists():
        raise RuntimeError(f"Missing channel config: {CHANNEL_CONFIG_PATH}")
    with CHANNEL_CONFIG_PATH.open("r", encoding="utf-8") as file:
        raw_channels = json.load(file)
    if not isinstance(raw_channels, list):
        raise RuntimeError("Channel config must be a JSON list")

    channels: list[Channel] = []
    for item in raw_channels:
        if not item.get("enabled", True):
            continue
        channels.append(
            Channel(
                key=item["key"],
                name=item["name"],
                channel_id=item.get("channel_id", ""),
                handle_url=item.get("homepage_url") or item.get("handle_url") or "",
                bootstrap_days=int(item.get("bootstrap_days", 7)),
                language=item.get("language", "zh"),
                generate_chinese_audio=bool(item.get("generate_chinese_audio", False)),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return channels


def resolve_channel_id(channel: Channel) -> Channel:
    if channel.channel_id.startswith("UC"):
        return channel

    args = [
        *yt_dlp_args(),
        *yt_dlp_common_args(),
        "--dump-single-json",
        "--flat-playlist",
        "--playlist-end",
        "1",
        channel.videos_tab_url,
    ]
    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(f"Could not resolve channel_id for {channel.name}: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    channel_id = data.get("channel_id") or data.get("uploader_id") or data.get("id")
    if not isinstance(channel_id, str) or not channel_id.startswith("UC"):
        entries = data.get("entries") or []
        for entry in entries:
            channel_id = entry.get("channel_id") or entry.get("uploader_id")
            if isinstance(channel_id, str) and channel_id.startswith("UC"):
                break
    if not isinstance(channel_id, str) or not channel_id.startswith("UC"):
        raise RuntimeError(f"Could not resolve channel_id for {channel.name}")

    return Channel(
        key=channel.key,
        name=channel.name,
        channel_id=channel_id,
        handle_url=channel.handle_url,
        bootstrap_days=channel.bootstrap_days,
        language=channel.language,
        generate_chinese_audio=channel.generate_chinese_audio,
        enabled=channel.enabled,
    )


def resolve_channel_ids(channels: list[Channel]) -> list[Channel]:
    return [resolve_channel_id(channel) for channel in channels]


def find_channel(channels: list[Channel], key: str) -> Channel:
    for channel in channels:
        if channel.key == key:
            return channel
    valid = ", ".join(channel.key for channel in channels)
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
            translated_title TEXT,
            translated_summary TEXT,
            transcript_text TEXT,
            audio_language TEXT,
            processed_at TEXT,
            notified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    ensure_video_columns(conn)
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


def ensure_video_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(videos)").fetchall()
    }
    columns = {
        "translated_title": "TEXT",
        "translated_summary": "TEXT",
        "transcript_text": "TEXT",
        "audio_language": "TEXT",
    }
    for column, column_type in columns.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {column} {column_type}")


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


def latest_seen_published_at(conn: sqlite3.Connection, channel: Channel) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(published_at) AS latest FROM videos WHERE channel_key = ?",
        (channel.key,),
    ).fetchone()
    return parse_dt(row["latest"]) if row and row["latest"] else None


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


def mark_uploaded(
    conn: sqlite3.Connection,
    video: Video,
    audio_key: str,
    public_url: str,
    chinese_audio: ChineseAudio | None = None,
    transcript_text: str | None = None,
    audio_language: str | None = None,
) -> None:
    now = utc_now().isoformat()
    conn.execute(
        """
        UPDATE videos
        SET status = 'uploaded',
            audio_key = ?,
            public_url = ?,
            translated_title = ?,
            translated_summary = ?,
            transcript_text = ?,
            audio_language = ?,
            processed_at = ?,
            updated_at = ?
        WHERE video_id = ?
        """,
        (
            audio_key,
            public_url,
            chinese_audio.title if chinese_audio else None,
            "\n".join(chinese_audio.summary) if chinese_audio else None,
            transcript_text,
            audio_language,
            now,
            now,
            video.video_id,
        ),
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
        try:
            return [
                video
                for video in fetch_feed_videos(channel)
                if not video.published_at or video.published_at >= cutoff
            ]
        except Exception as exc:
            print(f"[WARN] RSS bootstrap failed for {channel.name}, falling back to yt-dlp: {exc}")

    return fetch_yt_dlp_videos(channel, cutoff=cutoff)


def fetch_yt_dlp_videos(channel: Channel, cutoff: datetime | None = None) -> list[Video]:
    limit = int(env("BOOTSTRAP_PLAYLIST_LIMIT", "120"))
    args = [
        *yt_dlp_args(),
        *yt_dlp_common_args(),
        "--ignore-errors",
        "--dump-json",
        "--playlist-end",
        str(limit),
    ]
    if cutoff:
        args.extend(["--dateafter", cutoff.strftime("%Y%m%d")])
    args.append(channel.videos_tab_url)
    result = run_command(args)
    videos = parse_yt_dlp_json_lines(result.stdout, channel)
    if result.returncode != 0:
        print(f"[WARN] yt-dlp listing returned errors for {channel.name}: {result.stderr.strip()}")
    if cutoff:
        videos = [video for video in videos if not video.published_at or video.published_at >= cutoff]
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


def public_r2_url(key: str) -> str:
    base_url = env("PUBLIC_AUDIO_BASE_URL", required=True).rstrip("/")
    return f"{base_url}/{quote(key)}"


def upload_path_to_r2(path: Path, key: str, content_type: str) -> tuple[str, str]:
    bucket = env("R2_BUCKET_NAME", required=True)
    r2_client().upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return key, public_r2_url(key)


def delete_r2_object(key: str) -> None:
    r2_client().delete_object(Bucket=env("R2_BUCKET_NAME", required=True), Key=key)


def upload_to_r2(mp3_path: Path, video: Video, title: str | None = None) -> tuple[str, str]:
    title_part = display_audio_filename(video, title=title)
    key = f"{video.channel.key}/{title_part}.mp3"
    return upload_path_to_r2(mp3_path, key, "audio/mpeg")


def truncate_text(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def build_telegram_caption(
    video: Video,
    published: str,
    translated_title: str | None = None,
    translated_summary: str | None = None,
) -> str:
    if translated_title:
        summary = translated_summary or "暂无"
        caption = (
            f"频道：{video.channel.name}\n"
            f"原标题：{video.title}\n"
            f"中文标题：{translated_title}\n"
            f"发布时间：{published}\n"
            f"中文摘要：\n{summary}"
        )
    else:
        caption = (
            f"频道：{video.channel.name}\n"
            f"标题：{video.title}\n"
            f"发布时间：{published}"
        )
    return truncate_text(caption, 1024)


def build_telegram_link_text(
    video: Video,
    public_url: str,
    published: str,
    translated_title: str | None = None,
    translated_summary: str | None = None,
    audio_language: str | None = None,
) -> str:
    caption = build_telegram_caption(video, published, translated_title, translated_summary)
    label = "中文音频" if translated_title or audio_language == "zh" else "MP3"
    return f"{caption}\n{label}：<a href=\"{html.escape(public_url, quote=True)}\">点击收听</a>"


def notify_telegram(
    video: Video,
    public_url: str,
    translated_title: str | None = None,
    translated_summary: str | None = None,
    audio_language: str | None = None,
) -> None:
    token = env("TELEGRAM_BOT_TOKEN", required=True)
    chat_id = env("TELEGRAM_CHAT_ID", required=True)
    published = video.published_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if video.published_at else "未知"
    audio_title = translated_title or video.title
    caption = build_telegram_caption(video, published, translated_title, translated_summary)
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendAudio",
        json={
            "chat_id": chat_id,
            "audio": public_url,
            "title": truncate_text(audio_title, 64),
            "performer": truncate_text(video.channel.name, 64),
            "caption": caption,
        },
        timeout=30,
    )
    if response.ok:
        return

    fallback_text = build_telegram_link_text(
        video,
        public_url,
        published,
        translated_title=translated_title,
        translated_summary=translated_summary,
        audio_language=audio_language,
    )
    fallback_response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": fallback_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    fallback_response.raise_for_status()


def dashscope_headers(async_enabled: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {env('DASHSCOPE_API_KEY', required=True)}",
        "Content-Type": "application/json",
    }
    if async_enabled:
        headers["X-DashScope-Async"] = "enable"
    return headers


def dashscope_api_base() -> str:
    return env("DASHSCOPE_API_BASE", "https://dashscope.aliyuncs.com/api/v1").rstrip("/")


def dashscope_compatible_base() -> str:
    return env("DASHSCOPE_COMPATIBLE_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")


def zhipu_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env('ZHIPU_API_KEY', required=True)}",
        "Content-Type": "application/json",
    }


def zhipu_api_base() -> str:
    return env("ZHIPU_API_BASE", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")


def nvidia_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env('NVIDIA_API_KEY', required=True)}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def nvidia_api_base() -> str:
    return env("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")


def upload_asr_source(mp3_path: Path, video: Video) -> tuple[str, str]:
    key = f"_asr_source/{video.channel.key}/{video.video_id}.mp3"
    return upload_path_to_r2(mp3_path, key, "audio/mpeg")


def submit_asr_task(audio_url: str, language: str = "en") -> str:
    payload = {
        "model": env("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash-filetrans"),
        "input": {"file_url": audio_url},
        "parameters": {
            "channel_id": [0],
            "language": language,
            "enable_itn": True,
            "enable_words": False,
        },
    }
    response = requests.post(
        f"{dashscope_api_base()}/services/audio/asr/transcription",
        headers=dashscope_headers(async_enabled=True),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    task_id = (data.get("output") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"DashScope ASR task_id missing: {data}")
    return task_id


def extract_transcription_url(task_data: dict) -> str | None:
    output = task_data.get("output") or {}
    result = output.get("result") or {}
    if isinstance(result, dict) and result.get("transcription_url"):
        return result["transcription_url"]

    results = output.get("results") or []
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("transcription_url"):
                return item["transcription_url"]
    return None


def wait_asr_task(task_id: str) -> str:
    timeout_seconds = int(env("DASHSCOPE_ASR_TIMEOUT_SECONDS", "3600"))
    poll_seconds = int(env("DASHSCOPE_ASR_POLL_SECONDS", "5"))
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        response = requests.get(
            f"{dashscope_api_base()}/tasks/{task_id}",
            headers=dashscope_headers(async_enabled=True),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        output = data.get("output") or {}
        status = str(output.get("task_status") or "").upper()
        if status == "SUCCEEDED":
            transcription_url = extract_transcription_url(data)
            if not transcription_url:
                raise RuntimeError(f"DashScope ASR transcription_url missing: {data}")
            return transcription_url
        if status in {"FAILED", "UNKNOWN", "CANCELED"}:
            raise RuntimeError(f"DashScope ASR task failed: {data}")
        time.sleep(poll_seconds)

    raise RuntimeError(f"DashScope ASR task timed out: {task_id}")


def extract_transcript_text(data: dict) -> str:
    texts: list[str] = []
    transcripts = data.get("transcripts") or []
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        sentences = transcript.get("sentences") or []
        if sentences:
            texts.extend(sentence.get("text", "") for sentence in sentences if isinstance(sentence, dict))
        elif transcript.get("text"):
            texts.append(transcript["text"])

    if not texts and data.get("text"):
        texts.append(data["text"])
    text = "\n".join(item.strip() for item in texts if item and item.strip())
    if not text:
        raise RuntimeError("DashScope ASR returned empty transcript")
    return text


def transcribe_with_dashscope(mp3_path: Path, video: Video) -> str:
    source_key = ""
    try:
        source_key, source_url = upload_asr_source(mp3_path, video)
        task_id = submit_asr_task(source_url, language=video.channel.language or "en")
        transcription_url = wait_asr_task(task_id)
        response = requests.get(transcription_url, timeout=120)
        response.raise_for_status()
        return extract_transcript_text(response.json())
    finally:
        if source_key:
            try:
                delete_r2_object(source_key)
            except Exception as exc:
                print(f"[WARN] failed to delete temporary ASR source {source_key}: {exc}")


def whisper_bin() -> str:
    configured = env("WHISPER_BIN")
    if configured:
        return configured
    for candidate in ("whisper-cli", "whisper-cpp", "main"):
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError("whisper.cpp not found. Install it or set WHISPER_BIN in .env")


def transcribe_with_local_whisper(mp3_path: Path, video: Video) -> str:
    model_path = env("WHISPER_MODEL_PATH", required=True)
    output_prefix = mp3_path.with_suffix(".whisper")
    output_txt = Path(str(output_prefix) + ".txt")
    args = [
        whisper_bin(),
        "-m",
        model_path,
        "-f",
        str(mp3_path),
        "-l",
        env("WHISPER_LANGUAGE", video.channel.language or "en"),
        "-otxt",
        "-of",
        str(output_prefix),
    ]
    extra_args = env("WHISPER_EXTRA_ARGS")
    if extra_args:
        args.extend(extra_args.split())

    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(f"local whisper transcription failed: {result.stderr.strip()}")
    if not output_txt.exists():
        raise RuntimeError(f"local whisper output missing: {output_txt}")
    text = output_txt.read_text(encoding="utf-8").strip()
    output_txt.unlink(missing_ok=True)
    if not text:
        raise RuntimeError("local whisper returned empty transcript")
    return text


def transcribe_audio(mp3_path: Path, video: Video) -> str:
    provider = env("ASR_PROVIDER", "dashscope").lower()
    if provider == "local_whisper":
        return transcribe_with_local_whisper(mp3_path, video)
    if provider == "dashscope":
        return transcribe_with_dashscope(mp3_path, video)
    raise RuntimeError(f"Unsupported ASR_PROVIDER: {provider}")


def chunk_text(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in re.split(r"\n+", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current_len + len(paragraph) > max_chars and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if len(paragraph) > max_chars:
            for start in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[start : start + max_chars])
            continue
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        chunks.append("\n".join(current))
    return chunks


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def dashscope_chat_json(messages: list[dict], temperature: float = 0.3) -> dict:
    payload = {
        "model": env("DASHSCOPE_TRANSLATE_MODEL", "qwen-plus"),
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        f"{dashscope_compatible_base()}/chat/completions",
        headers=dashscope_headers(),
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return parse_json_object(content)


def zhipu_chat_json(messages: list[dict], temperature: float = 0.3) -> dict:
    payload = {
        "model": env("ZHIPU_MODEL", "glm-4.7-flash"),
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        f"{zhipu_api_base()}/chat/completions",
        headers=zhipu_headers(),
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return parse_json_object(content)


def nvidia_chat_json(messages: list[dict], temperature: float = 0.3) -> dict:
    payload = {
        "model": env("NVIDIA_MODEL", "minimaxai/minimax-m3"),
        "messages": messages,
        "max_tokens": int(env("NVIDIA_MAX_TOKENS", "8192")),
        "temperature": temperature,
        "top_p": float(env("NVIDIA_TOP_P", "0.95")),
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        f"{nvidia_api_base()}/chat/completions",
        headers=nvidia_headers(),
        json=payload,
        timeout=int(env("NVIDIA_TIMEOUT_SECONDS", "600")),
    )
    if not response.ok:
        raise RuntimeError(f"NVIDIA chat failed ({response.status_code}): {truncate_text(response.text, 500)}")
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    try:
        return parse_json_object(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"NVIDIA chat returned invalid JSON: {truncate_text(content, 500)}") from exc


def text_model_json(messages: list[dict], temperature: float = 0.3) -> dict:
    provider = env("TEXT_PROVIDER", "dashscope").lower()
    if provider == "zhipu":
        return zhipu_chat_json(messages, temperature=temperature)
    if provider == "dashscope":
        return dashscope_chat_json(messages, temperature=temperature)
    if provider == "nvidia":
        return nvidia_chat_json(messages, temperature=temperature)
    raise RuntimeError(f"Unsupported TEXT_PROVIDER: {provider}")


def summarize_transcript_chunk(video: Video, chunk: str, index: int, total: int) -> str:
    result = text_model_json(
        [
            {
                "role": "system",
                "content": "你是金融播客编辑，擅长把英文投资视频转成准确、自然、适合中文听众收听的中文内容。",
            },
            {
                "role": "user",
                "content": (
                    f"这是 {video.channel.name} 的视频《{video.title}》转写稿第 {index}/{total} 段。\n"
                    "请提取事实、观点、论据、数字和风险提示，输出 JSON："
                    "{\"summary\":\"中文分段摘要\"}。\n\n"
                    f"{chunk}"
                ),
            },
        ]
    )
    return str(result.get("summary") or "").strip()


def build_chinese_podcast_content(video: Video, transcript_text: str) -> tuple[str, list[str], str]:
    max_chunk_chars = int(env("TRANSLATE_CHUNK_CHARS", "20000"))
    chunks = chunk_text(transcript_text, max_chunk_chars)
    if len(chunks) > 1:
        source_text = "\n\n".join(
            summarize_transcript_chunk(video, chunk, index, len(chunks))
            for index, chunk in enumerate(chunks, start=1)
        )
    else:
        source_text = transcript_text

    target_minutes = env("CHINESE_PODCAST_TARGET_MINUTES", "8-12")
    result = text_model_json(
        [
            {
                "role": "system",
                "content": (
                    "你是中文财经播客制作人。你的任务不是逐字翻译，而是把英文视频内容改写成"
                    "中文听众可以直接收听的播客稿，保持事实准确，明确区分原作者观点和你的串联表达。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"频道：{video.channel.name}\n原标题：{video.title}\n"
                    f"目标长度：约 {target_minutes} 分钟中文音频。\n"
                    "风格：轻松播客感，口语自然，但不要夸张，不要新增原文没有的信息。\n"
                    "输出严格 JSON："
                    "{\"title\":\"中文标题\",\"summary\":[\"要点1\",\"要点2\",\"要点3\"],\"script\":\"完整中文播客稿\"}。\n\n"
                    f"材料：\n{source_text}"
                ),
            },
        ],
        temperature=0.35,
    )
    title = str(result.get("title") or video.title).strip()
    summary_raw = result.get("summary") or []
    if isinstance(summary_raw, str):
        summary = [line.strip("- 1234567890.、") for line in summary_raw.splitlines() if line.strip()]
    else:
        summary = [str(item).strip() for item in summary_raw if str(item).strip()]
    script = str(result.get("script") or "").strip()
    if not script:
        raise RuntimeError("DashScope translation returned empty podcast script")
    return title, summary[:5], script


def split_tts_segments(text: str) -> list[str]:
    max_chars = int(env("TTS_SEGMENT_CHARS", "450"))
    segments: list[str] = []
    current = ""
    for part in re.split(r"(?<=[。！？!?；;])", text):
        part = part.strip()
        if not part:
            continue
        if len(current) + len(part) > max_chars and current:
            segments.append(current)
            current = ""
        while len(part) > max_chars:
            segments.append(part[:max_chars])
            part = part[max_chars:]
        current += part
    if current:
        segments.append(current)
    return segments


def synthesize_sambert_segment(text: str, output_path: Path) -> None:
    import dashscope
    from dashscope.audio.tts import SpeechSynthesizer

    dashscope.api_key = env("DASHSCOPE_API_KEY", required=True)
    websocket_url = env("DASHSCOPE_WEBSOCKET_URL")
    if websocket_url:
        dashscope.base_websocket_api_url = websocket_url

    result = SpeechSynthesizer.call(
        model=env("DASHSCOPE_TTS_MODEL", "sambert-zhide-v1"),
        text=text,
        sample_rate=int(env("DASHSCOPE_TTS_SAMPLE_RATE", "48000")),
        format=env("DASHSCOPE_TTS_FORMAT", "wav"),
        rate=float(env("DASHSCOPE_TTS_RATE", "1.0")),
    )
    audio_data = result.get_audio_data()
    if audio_data is None:
        raise RuntimeError(f"DashScope Sambert TTS failed: {result.get_response()}")
    output_path.write_bytes(audio_data)


def synthesize_tts_segment(text: str, output_path: Path) -> None:
    provider = env("TTS_PROVIDER", "dashscope").lower()
    if provider == "macos_say":
        result = run_command(
            [
                "say",
                "-v",
                env("MACOS_TTS_VOICE", "Tingting"),
                "-r",
                env("MACOS_TTS_RATE", "185"),
                "-o",
                str(output_path),
                "--",
                text,
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(f"macOS say TTS failed: {result.stderr.strip()}")
        return
    if provider != "dashscope":
        raise RuntimeError(f"Unsupported TTS_PROVIDER: {provider}")

    model = env("DASHSCOPE_TTS_MODEL", "qwen3-tts-instruct-flash")
    if model.startswith("sambert-"):
        synthesize_sambert_segment(text, output_path)
        return

    if model.startswith("cosyvoice"):
        payload = {
            "model": model,
            "input": {
                "text": text,
                "voice": env("DASHSCOPE_TTS_VOICE", "longxiaochun"),
                "format": "wav",
                "sample_rate": 24000,
            },
        }
        tts_url = env(
            "DASHSCOPE_TTS_URL",
            f"{dashscope_api_base()}/services/audio/tts/SpeechSynthesizer",
        )
        response = requests.post(tts_url, headers=dashscope_headers(), json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        audio_url = ((data.get("output") or {}).get("audio") or {}).get("url")
        if not audio_url:
            raise RuntimeError(f"DashScope TTS audio url missing: {data}")
        audio_response = requests.get(audio_url, timeout=120)
        audio_response.raise_for_status()
        output_path.write_bytes(audio_response.content)
        return

    payload = {
        "model": model,
        "input": {
            "text": text,
            "voice": env("DASHSCOPE_TTS_VOICE", "Cherry"),
            "language_type": "Chinese",
        },
    }
    instructions = env(
        "DASHSCOPE_TTS_INSTRUCTIONS",
        "轻松播客感，语速中等，表达自然清楚，适合财经内容收听。",
    )
    if instructions and "instruct" in model:
        payload["input"]["instructions"] = instructions
        payload["input"]["optimize_instructions"] = True

    tts_url = env(
        "DASHSCOPE_TTS_URL",
        f"{dashscope_api_base()}/services/aigc/multimodal-generation/generation",
    )
    response = requests.post(tts_url, headers=dashscope_headers(), json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    audio = (data.get("output") or {}).get("audio") or {}
    audio_url = audio.get("url")
    if not audio_url:
        raise RuntimeError(f"DashScope TTS audio url missing: {data}")
    audio_response = requests.get(audio_url, timeout=120)
    audio_response.raise_for_status()
    output_path.write_bytes(audio_response.content)


def concat_audio_segments(segment_paths: list[Path], output_path: Path) -> None:
    concat_file = output_path.with_suffix(".concat.txt")
    lines = []
    for path in segment_paths:
        escaped_path = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    quality = env("MP3_AUDIO_QUALITY", "128K")
    result = run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-vn",
            "-b:a",
            quality,
            str(output_path),
        ]
    )
    concat_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr.strip()}")


def synthesize_chinese_audio(video: Video, title: str, summary: list[str], script: str) -> ChineseAudio:
    download_dir = ROOT / env("PODCAST_DOWNLOAD_DIR", "downloads")
    work_dir = download_dir / f"{video.video_id}-tts-parts"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = download_dir / f"{video.video_id}-zh.mp3"
    try:
        segment_paths: list[Path] = []
        for index, segment in enumerate(split_tts_segments(script), start=1):
            suffix = "aiff" if env("TTS_PROVIDER", "dashscope").lower() == "macos_say" else env("DASHSCOPE_TTS_FORMAT", "wav")
            segment_path = work_dir / f"{index:03d}.{suffix}"
            synthesize_tts_segment(segment, segment_path)
            segment_paths.append(segment_path)
        concat_audio_segments(segment_paths, output_path)
        return ChineseAudio(title=title, summary=summary, script=script, audio_path=output_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def generate_chinese_audio(video: Video, source_mp3: Path) -> tuple[ChineseAudio, str]:
    transcript_text = transcribe_audio(source_mp3, video)
    title, summary, script = build_chinese_podcast_content(video, transcript_text)
    return synthesize_chinese_audio(video, title, summary, script), transcript_text


def collect_candidates(conn: sqlite3.Connection, channel: Channel) -> Iterable[Video]:
    if is_bootstrapped(conn, channel):
        latest_published = latest_seen_published_at(conn, channel)
        try:
            candidates = fetch_feed_videos(channel)
        except Exception as exc:
            print(f"[WARN] RSS listing failed for {channel.name}, falling back to yt-dlp: {exc}")
            cutoff = latest_published or (utc_now() - timedelta(days=channel.bootstrap_days))
            candidates = fetch_yt_dlp_videos(channel, cutoff=cutoff)
        if latest_published:
            candidates = [
                video
                for video in candidates
                if not video.published_at or video.published_at > latest_published
            ]
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
            notify_telegram(
                pending_video,
                existing["public_url"],
                translated_title=existing["translated_title"],
                translated_summary=existing["translated_summary"],
                audio_language=existing["audio_language"],
            )
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
        if enriched.channel.generate_chinese_audio:
            print(f"[DRY-RUN] would generate Chinese audio {enriched.channel.name}: {enriched.title}")
        else:
            print(f"[DRY-RUN] would process {enriched.channel.name}: {enriched.title}")
        return True

    upsert_video(conn, enriched, "processing")
    mp3_path: Path | None = None
    chinese_audio_path: Path | None = None
    try:
        mp3_path = download_mp3(enriched)
        if enriched.channel.generate_chinese_audio:
            chinese_audio, transcript_text = generate_chinese_audio(enriched, mp3_path)
            chinese_audio_path = chinese_audio.audio_path
            audio_key, public_url = upload_to_r2(chinese_audio.audio_path, enriched, title=chinese_audio.title)
            mark_uploaded(
                conn,
                enriched,
                audio_key,
                public_url,
                chinese_audio=chinese_audio,
                transcript_text=transcript_text,
                audio_language="zh",
            )
            translated_title = chinese_audio.title
            translated_summary = "\n".join(f"{index}. {item}" for index, item in enumerate(chinese_audio.summary, start=1))
            notify_kwargs = {
                "translated_title": translated_title,
                "translated_summary": translated_summary,
                "audio_language": "zh",
            }
        else:
            audio_key, public_url = upload_to_r2(mp3_path, enriched)
            mark_uploaded(conn, enriched, audio_key, public_url, audio_language=enriched.channel.language)
            notify_kwargs = {"audio_language": enriched.channel.language}

        try:
            notify_telegram(enriched, public_url, **notify_kwargs)
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
        if chinese_audio_path and chinese_audio_path.exists():
            chinese_audio_path.unlink()


def notify_pending_uploads(conn: sqlite3.Connection, channels: list[Channel], dry_run: bool = False) -> None:
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
        channel = next((item for item in channels if item.key == row["channel_key"]), None)
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
            notify_telegram(
                video,
                row["public_url"],
                translated_title=row["translated_title"],
                translated_summary=row["translated_summary"],
                audio_language=row["audio_language"],
            )
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
    parser.add_argument("--list-channels", action="store_true", help="List enabled channels from config/channels.json.")
    parser.add_argument("--url", action="append", help="Manually process a YouTube video URL, including member-only videos you can access.")
    parser.add_argument("--channel", default="nana", help="Channel key for --url uploads. Defaults to nana.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    check_dependencies()

    if args.cleanup_only:
        conn = connect_db()
        cleanup_retention(conn, dry_run=args.dry_run)
        return

    channels = resolve_channel_ids(load_channels())

    if args.list_channels:
        for channel in channels:
            mode = "中文音频" if channel.generate_chinese_audio else "原音频"
            print(f"{channel.key}\t{channel.name}\t{channel.language}\t{mode}\t{channel.handle_url}")
        return

    conn = connect_db()

    notify_pending_uploads(conn, channels, dry_run=args.dry_run)

    if args.url:
        channel = find_channel(channels, args.channel)
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

    for channel in channels:
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
