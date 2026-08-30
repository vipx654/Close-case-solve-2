"""Fast media processing for Telegram inline playback.

Goal (per product decision): FAST stream mode — never do a slow full H.264
transcode. We:
  1. probe with ffprobe (duration, width, height, video/audio codecs);
  2. generate a thumbnail (1 frame, JPEG);
  3. if the container is not a streamable MP4 (e.g. MKV / AVI), REMUX to MP4
     with stream-copy (`-c copy`) — this is seconds, not minutes — and add
     faststart so the video starts playing before download finishes.

ffmpeg / ffprobe are optional: if they are missing every helper degrades
gracefully (returns None / the original path) so the rest of the bot still
works and simply uploads the file as a document.
"""
import os
import json
import asyncio
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(os.getcwd(), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Codecs Telegram can stream inside MP4. Anything else would need a real
# transcode, which we intentionally avoid (fast mode) — we still remux the
# container but Telegram may fall back to "download".
_STREAMABLE_VIDEO = {"h264", "avc1", "hevc", "h265", "av01"}  # h265/av1 stream on newer clients
_STREAMABLE_AUDIO = {"aac", "mp3", "mp4a", "opus", "vorbis", "ac3"}
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".flv", ".ts", ".wmv"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus"}


def tools_available() -> bool:
    return bool(FFMPEG and FFPROBE)


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _VIDEO_EXTS


def is_audio(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _AUDIO_EXTS


async def _run(cmd: list, timeout: int = 900):
    """Run a command, capturing output. Returns (rc, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, (stderr.decode("utf-8", "ignore") if stderr else "")
    except FileNotFoundError:
        return 127, "ffmpeg/ffprobe not installed"
    except asyncio.TimeoutError:
        return 124, "timeout"
    except Exception as e:  # pragma: no cover
        return 1, str(e)


async def probe(path: str) -> dict:
    """Return media info dict (duration, width, height, vcodec, acodec) or {}."""
    if not FFPROBE or not os.path.exists(path):
        return {}
    cmd = [
        FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    data = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return {}
        data = json.loads(out.decode("utf-8", "ignore"))
    except Exception as e:
        logger.warning(f"ffprobe failed for {path}: {e}")
        return {}

    info = {"duration": 0, "width": 0, "height": 0, "vcodec": "", "acodec": ""}
    try:
        info["duration"] = int(float(data.get("format", {}).get("duration", 0) or 0))
    except (TypeError, ValueError):
        info["duration"] = 0
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not info["vcodec"]:
            info["vcodec"] = st.get("codec_name", "")
            info["width"] = st.get("width", 0) or 0
            info["height"] = st.get("height", 0) or 0
        elif st.get("codec_type") == "audio" and not info["acodec"]:
            info["acodec"] = st.get("codec_name", "")
    return info


async def make_thumb(path: str, seek_seconds: int = 5) -> str | None:
    """Extract a single frame as a JPEG thumbnail. Returns path or None."""
    if not FFMPEG or not os.path.exists(path):
        return None
    thumb_path = os.path.splitext(path)[0] + ".jpg"
    cmd = [
        FFMPEG, "-y", "-ss", str(seek_seconds), "-i", path,
        "-vframes", "1", "-vf", "thumbnail", "-q:v", "3", thumb_path,
    ]
    rc, err = await _run(cmd, timeout=120)
    if rc == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        return thumb_path
    # Retry from the very start (short clips / seek past end).
    cmd[2] = "0"
    rc, _ = await _run(cmd, timeout=120)
    if rc == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        return thumb_path
    return None


async def remux_to_mp4(path: str, timeout: int = 1800) -> tuple[str | None, bool]:
    """Remux a video to a streamable MP4 with stream copy (no re-encode).

    Returns (output_path, converted). If the file is already an MP4 or ffmpeg
    is unavailable, returns (path, False) so the caller uploads the original.
    """
    ext = os.path.splitext(path)[1].lower()
    if not FFMPEG or ext == ".mp4":
        return path, False
    out_path = os.path.splitext(path)[0] + ".mp4"
    # -c copy = no re-encoding (fast); -movflags +faststart = play while downloading
    cmd = [
        FFMPEG, "-y", "-i", path,
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c", "copy", "-movflags", "+faststart",
        out_path,
    ]
    rc, err = await _run(cmd, timeout=timeout)
    if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path, True
    logger.warning(f"remux failed for {path}: {err[-300:]}")
    # Clean a partial output and fall back to the original file.
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass
    return path, False


async def prepare_video(path: str, make_thumbnail: bool = True) -> dict:
    """One-call helper: probe + remux + thumbnail.

    Returns a dict:
      {
        "path": final upload path (mp4 if remuxed),
        "converted": bool,
        "thumb": thumb path or None,
        "duration": int seconds,
        "width": int, "height": int,
      }
    """
    result = {"path": path, "converted": False, "thumb": None,
              "duration": 0, "width": 0, "height": 0}
    if not tools_available() or not is_video(path):
        if is_video(path):
            result.update(await probe(path))
        return result

    info = await probe(path)
    result.update(info)

    out, converted = await remux_to_mp4(path)
    result["path"] = out
    result["converted"] = converted

    if make_thumbnail:
        thumb_source = out if converted else path
        result["thumb"] = await make_thumb(thumb_source)

    return result


def cleanup_files(*paths: str) -> None:
    """Best-effort removal of temp files (video, thumbs, downloads)."""
    for p in paths:
        if not p:
            continue
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
