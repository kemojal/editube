"""FFmpeg-based proxy generation background job.

Downloads the original video from its URL, transcodes to the requested
proxy profile, uploads the result, and updates the VideoProxy row.

Requires ``ffmpeg`` and ``ffprobe`` on $PATH (or configure via env vars).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.request

logger = logging.getLogger(__name__)

_FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg").strip()
_FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe").strip()

PROXY_PROFILES = {
    "540p_h264": {
        "width": 960,
        "height": 540,
        "bitrate": "2M",
        "audio_bitrate": "128k",
        "label": "540p Review Proxy",
    },
    "720p_h264": {
        "width": 1280,
        "height": 720,
        "bitrate": "4M",
        "audio_bitrate": "192k",
        "label": "720p Proxy",
    },
    "1080p_h264": {
        "width": 1920,
        "height": 1080,
        "bitrate": "8M",
        "audio_bitrate": "256k",
        "label": "1080p Proxy",
    },
}


def _download_to_temp(url: str, suffix: str = ".mp4") -> str:
    """Download a URL to a named temp file, return the path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        urllib.request.urlretrieve(url, path)
    except Exception:
        os.close(fd)
        os.unlink(path)
        raise
    else:
        os.close(fd)
    return path


def _probe_duration(path: str) -> int:
    """Return video duration in seconds via ffprobe, or 0."""
    try:
        cmd = [
            _FFPROBE, "-v", "quiet", "-print_format", "json",
            "-show_format", path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        return int(float(data.get("format", {}).get("duration", 0)))
    except Exception:
        return 0


def _transcode(input_path: str, output_path: str, profile: dict) -> None:
    """Run ffmpeg to produce the proxy rendition."""
    w = profile["width"]
    h = profile["height"]
    # Scale to fit within profile dimensions while keeping aspect ratio
    # -2 ensures even dimensions (required by H.264)
    scale_filter = f"scale='if(gt(iw/ih,{w}/{h}),{w},-2)':'if(gt(iw/ih,{w}/{h}),-2,{h})'"

    cmd = [
        _FFMPEG,
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "fast",
        "-b:v", profile["bitrate"],
        "-maxrate", profile["bitrate"],
        "-bufsize", str(int(profile["bitrate"].replace("M", "")) * 2) + "M",
        "-vf", scale_filter,
        "-c:a", "aac",
        "-b:a", profile["audio_bitrate"],
        "-movflags", "+faststart",
        "-threads", "0",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed (exit {result.returncode}): {result.stderr[:500]}"
        )


def _upload_proxy(proxy_path: str) -> dict:
    """Upload proxy file to Cloudinary.

    Returns {"url": str, "bytes": int, "public_id": str}.
    Falls back to local storage if Cloudinary is not configured.
    """
    storage = os.getenv("PROXY_STORAGE", "cloudinary").strip()

    if storage == "local":
        local_dir = os.getenv("PROXY_LOCAL_DIR", "./proxies").strip()
        os.makedirs(local_dir, exist_ok=True)
        filename = os.path.basename(proxy_path)
        dest = os.path.join(local_dir, filename)

        import shutil
        shutil.copy2(proxy_path, dest)
        size = os.path.getsize(dest)
        return {"url": dest, "bytes": size, "public_id": filename}

    # Default: Cloudinary
    try:
        import cloudinary
        import cloudinary.uploader

        result = cloudinary.uploader.upload_large(
            proxy_path,
            resource_type="video",
            folder="editube-proxies",
            chunk_size=6 * 1024 * 1024,
            timeout=900,
        )
        return {
            "url": result.get("secure_url", ""),
            "bytes": int(result.get("bytes", 0)),
            "public_id": result.get("public_id", ""),
        }
    except Exception as e:
        raise RuntimeError(f"Proxy upload failed: {e}") from e


def proxy_generation_job(proxy_id: int) -> None:
    """RQ job: generate proxy for a VideoProxy row.

    This is the entry point called by the worker.
    """
    # Import inside job to avoid circular imports at module level
    from app.db.database import SessionLocal
    from app.db.models import VideoProxy

    db = SessionLocal()
    try:
        proxy = db.query(VideoProxy).filter(VideoProxy.id == proxy_id).first()
        if not proxy:
            logger.error("VideoProxy %s not found", proxy_id)
            return

        profile_config = PROXY_PROFILES.get(proxy.profile)
        if not profile_config:
            proxy.status = "failed"
            proxy.error_message = f"Unknown profile: {proxy.profile}"
            db.commit()
            return

        video = proxy.video
        if not video or not video.file_path:
            proxy.status = "failed"
            proxy.error_message = "Video has no file_path"
            db.commit()
            return

        proxy.status = "processing"
        db.commit()

        input_path = None
        output_path = None
        try:
            # Download original
            logger.info("Downloading original for proxy %s: %s", proxy_id, video.file_path[:80])
            input_path = _download_to_temp(video.file_path)

            # Transcode
            output_path = tempfile.mktemp(suffix=f"_proxy_{proxy.profile}.mp4")
            logger.info("Transcoding proxy %s (profile=%s)", proxy_id, proxy.profile)
            _transcode(input_path, output_path, profile_config)

            # Upload
            upload_result = _upload_proxy(output_path)

            # Probe output
            duration = _probe_duration(output_path)
            size = upload_result.get("bytes", 0) or os.path.getsize(output_path)

            # Update row
            proxy.status = "completed"
            proxy.file_url = upload_result["url"]
            proxy.width = profile_config["width"]
            proxy.height = profile_config["height"]
            proxy.bitrate_kbps = int(profile_config["bitrate"].replace("M", "")) * 1000
            proxy.codec = "h264"
            proxy.size_bytes = size
            proxy.duration = duration or video.duration
            proxy.error_message = None
            db.commit()

            logger.info("Proxy %s completed: %s", proxy_id, proxy.file_url[:80] if proxy.file_url else "")

        except Exception as e:
            proxy.status = "failed"
            proxy.error_message = str(e)[:1000]
            db.commit()
            logger.exception("Proxy generation failed for %s: %s", proxy_id, e)

        finally:
            for p in (input_path, output_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    finally:
        db.close()
