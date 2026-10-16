import os
import re
import uuid
import time
import asyncio
import logging
import shutil
import subprocess
from typing import List, Optional, Dict, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

import yt_dlp

from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("media_downloader")

# Base paths
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ClassPlus Access Token (Can be overridden via env CP_TOKEN or token_cp)
CP_TOKEN = os.environ.get(
    "CP_TOKEN", 
    os.environ.get("token_cp", "eyJjb3Vyc2VJZCI6IjQ1NjY4NyIsInR1dG9ySWQiOm51bGwsIm9yZ0lkIjo0ODA2MTksImNhdGVnb3J5SWQiOm51bGx9r")
)

# Optional YouTube cookies path
COOKIES_FILE = os.environ.get(
    "COOKIES_FILE", 
    str(BASE_DIR.parent / "Banana_bot--main" / "youtube_cookies.txt")
)

# In-memory Task Store and Queue
tasks_lock = asyncio.Lock()
tasks_db: Dict[str, Dict[str, Any]] = {}
download_queue: asyncio.Queue = asyncio.Queue()
MAX_CONCURRENT_WORKERS = 2
thread_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(queue_worker_loop())
    logger.info("Universal Web Video Downloader background worker loop initialized.")
    yield
    worker_task.cancel()

app = FastAPI(
    title="Universal Web Video Downloader",
    description="Clean, queue-based video & media downloader API with ClassPlus and Akamai DRM support",
    version="1.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



def sanitize_filename(name: str) -> str:
    """Sanitize string to be safe for filenames across Windows and Linux."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip()
    return name if name else "video"


def format_bytes(bytes_count: Optional[float]) -> str:
    """Format bytes into readable string."""
    if not bytes_count or bytes_count <= 0:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.1f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.1f} PB"


def format_seconds(seconds: Optional[float]) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if seconds is None or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# YouTube Uploader integration
try:
    from youtube_uploader import upload_video_to_youtube, get_youtube_service
    YOUTUBE_UPLOADER_AVAILABLE = True
except Exception as e:
    logger.warning(f"YouTube uploader module not available: {e}")
    YOUTUBE_UPLOADER_AVAILABLE = False


# --- Models ---
class SingleDownloadRequest(BaseModel):
    url: str
    custom_name: Optional[str] = None
    resolution: str = "720"  # 'best', '1080', '720', '480', '360', 'audio'
    upload_to_youtube: bool = False
    privacy_status: str = "unlisted"  # 'unlisted', 'private', 'public'
    user_id: Optional[str] = None


class BatchItemRequest(BaseModel):
    url: str
    custom_name: Optional[str] = None
    resolution: Optional[str] = "720"
    upload_to_youtube: bool = False
    privacy_status: Optional[str] = "unlisted"
    user_id: Optional[str] = None


class BatchDownloadRequest(BaseModel):
    items: Optional[List[BatchItemRequest]] = None
    raw_text: Optional[str] = None
    default_resolution: str = "720"
    upload_to_youtube: bool = False
    default_privacy: str = "unlisted"
    user_id: Optional[str] = None



# --- DRM & ClassPlus URL Resolver Helpers ---
def extract_keys_from_mpd(mpd_content: str) -> List[str]:
    """Extract decryption keys from MPD content."""
    keys = []
    try:
        kid_pattern = r'default_KID="([^"]+)"'
        kid_matches = re.findall(kid_pattern, mpd_content)
        for kid in kid_matches:
            clean_kid = kid.replace('-', '').lower()
            keys.append(f"{clean_kid}:00000000000000000000000000000000")
        return keys
    except Exception as e:
        logger.warning(f"Error extracting keys from MPD: {e}")
        return []


def get_mps_and_keys(api_url: str) -> tuple:
    """
    Fetch MPD and keys from ClassPlus API or direct manifest.
    Supports both L1 (key + userIds) and L2 (hdnts only) links.
    """
    try:
        if api_url.endswith('.mpd') or '/manifest' in api_url:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/dash+xml,application/xml,text/xml,*/*'
            }
            response = requests.get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            mpd_content = response.text
            keys = extract_keys_from_mpd(mpd_content)
            return api_url, keys

        response = requests.get(api_url, timeout=30)
        response_json = response.json()
        mpd = response_json.get('MPD') or api_url
        keys = response_json.get('KEYS') or []

        if not keys and isinstance(mpd, str) and ('<MPD' in mpd or mpd.endswith('.mpd')):
            if '<MPD' in mpd:
                keys = extract_keys_from_mpd(mpd)
            else:
                _, keys = get_mps_and_keys(mpd)

        return mpd, keys
    except Exception as e:
        logger.warning(f"Error in get_mps_and_keys: {e}")
        return api_url, []


def resolve_media_url(url: str) -> Dict[str, Any]:
    """
    Resolves ClassPlus, Akamai (L1/L2), Testbook, and DRM URLs before downloading.
    """
    base_url = url.strip()
    resolved_url = base_url
    keys_string = ""
    extra_headers: Dict[str, str] = {}
    media_type = "Standard"

    # 1. VisionIAS support
    if "visionias" in resolved_url:
        try:
            headers = {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Referer': 'http://www.visionias.in/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = requests.get(resolved_url, headers=headers, timeout=15)
            m = re.search(r'(https://.*?playlist\.m3u8.*?)["\']', resp.text)
            if m:
                resolved_url = m.group(1)
                media_type = "VisionIAS Stream"
        except Exception as e:
            logger.warning(f"VisionIAS resolve error: {e}")

    # 2. PW Live / Multiverse support
    elif "d1d34p8vz63oiq" in resolved_url or "sec1.pw.live" in resolved_url:
        resolved_url = f"https://download.asmultiverse.com?Vurl={resolved_url}"
        media_type = "PW Stream"

    # 3. Encrypted AppX support
    elif 'encrypted.m' in resolved_url:
        if '*' in resolved_url:
            resolved_url = resolved_url.split('*')[0]
        media_type = "AppX Encrypted"

    # ============================================================
    #  🔥 FIXED: ClassPlus / Akamai Logic (L1 + L2)
    # ============================================================
    elif ('classplusapp' in resolved_url or "testbook.com" in resolved_url or 
          "classplusapp.com/drm" in resolved_url or "media-cdn.classplusapp.com/drm" in resolved_url or 
          "akamai-cdn.classplusapp.com" in resolved_url):
        
        media_type = "ClassPlus / Akamai"

        # ---------- EXTRACT contentHashId / contentHashIdl ----------
        content_id = None
        hash_patterns = [
            r'contentHashId=([^&]+)',
            r'contentHashIdl=([^&]+)',
            r'[?&]contentHashId=([^&]+)',
            r'[?&]contentHashIdl=([^&]+)',
        ]
        for pattern in hash_patterns:
            match = re.search(pattern, resolved_url)
            if match:
                content_id = match.group(1).split('&')[0]
                break

        # Also try to get vidKey from URL path
        vidkey = None
        vid_patterns = [
            r'/lc/([^/]+)/',
            r'vidKey=([^&]+)',
        ]
        for pattern in vid_patterns:
            match = re.search(pattern, resolved_url)
            if match:
                vidkey = match.group(1)
                break

        logger.info(f"🔑 ClassPlus content_id: {content_id}")
        logger.info(f"🔑 ClassPlus vidkey: {vidkey}")

        # ---------- CHECK IF IT'S L1 (has key + userIds) ----------
        is_l1 = 'key=' in resolved_url and 'userIds=' in resolved_url
        is_l2 = 'hdnts=' in resolved_url and not is_l1

        if is_l1:
            logger.info("✅ L1 link detected (key + userIds) — preserving all parameters")
            media_type = "ClassPlus Akamai (L1)"
        elif is_l2:
            logger.info("✅ L2 link detected (hdnts only)")
            media_type = "ClassPlus Akamai (L2)"
        else:
            logger.info("ℹ️ Standard / unknown ClassPlus link format")

        # ---------- BUILD HEADERS ----------
        headers = {
            'host': 'api.classplusapp.com',
            'x-access-token': f'{CP_TOKEN}',    
            'accept-language': 'EN',
            'api-version': '18',
            'app-version': '1.4.73.2',
            'build-number': '35',
            'connection': 'Keep-Alive',
            'content-type': 'application/json',
            'device-details': 'Xiaomi_Redmi 7_SDK-32',
            'device-id': 'c28d3cb16bbdac01',
            'region': 'IN',
            'user-agent': 'Mobile-Android',
            'webengage-luid': '00000187-6fe4-5d41-a530-26186858be4c',
            'accept-encoding': 'gzip'
        }

        # ---------- USE content_id OR vidkey ----------
        api_content_id = content_id or vidkey

        if not api_content_id:
            # If no content_id found, try to extract from URL
            match = re.search(r'/([a-zA-Z0-9]+-[0-9]+[a-z]?)/', resolved_url)
            if match:
                api_content_id = match.group(1)

        if api_content_id:
            params = {
                'contentId': api_content_id,
                'offlineDownload': "false"
            }

            try:
                res = requests.get(
                    "https://api.classplusapp.com/cams/uploader/video/jw-signed-url",
                    params=params,
                    headers=headers,
                    timeout=15
                ).json()

                logger.info(f"📦 ClassPlus API Response: {res}")

                # Check if it's a DRM URL
                if ("testbook.com" in base_url or "classplusapp.com/drm" in base_url or 
                    "media-cdn.classplusapp.com/drm" in base_url or '/drm/' in base_url or
                    "akamai-cdn.classplusapp.com" in base_url):
                    if 'drmUrls' in res and 'manifestUrl' in res['drmUrls']:
                        mpd_url = res['drmUrls']['manifestUrl']
                        mpd, keys = get_mps_and_keys(mpd_url)
                        resolved_url = mpd if (isinstance(mpd, str) and not mpd.startswith('<?xml') and '<MPD' not in mpd) else mpd_url
                        keys_string = " ".join([f"--key {key}" for key in keys])
                        media_type = "ClassPlus DRM (MPD)"
                    else:
                        resolved_url = res.get("url", base_url)
                        keys_string = ""
                else:
                    resolved_url = res.get("url", base_url)
                    keys_string = ""

                logger.info(f"✅ ClassPlus API Success - contentId: {api_content_id[:30]}...")

            except Exception as e:
                logger.warning(f"⚠️ ClassPlus API Error: {e}")
                # Keep original URL if API fails — L1 and L2 both preserved
                resolved_url = base_url
                keys_string = ""
        else:
            logger.info("⚠️ No contentId or vidkey found, using original URL")
            resolved_url = base_url
            keys_string = ""

    # Extra headers for specific CDNs
    if "webvideos.classplusapp." in resolved_url:
        extra_headers['Referer'] = 'https://web.classplusapp.com/'
        extra_headers['x-cdn-tag'] = 'empty'

    return {
        "url": resolved_url,
        "keys_string": keys_string,
        "extra_headers": extra_headers,
        "media_type": media_type
    }


# --- Worker Loop ---
def sync_download_worker(task_id: str):
    """Synchronous worker executed in ThreadPool for yt-dlp with ClassPlus/DRM support."""
    task = tasks_db.get(task_id)
    if not task:
        return

    original_url = task["url"]
    custom_name = task.get("custom_name") or ""
    resolution = task.get("resolution", "720")

    task["status"] = "downloading"
    task["started_at"] = time.time()
    
    # Resolve ClassPlus / Akamai / DRM URL
    resolved_info = resolve_media_url(original_url)
    target_url = resolved_info["url"]
    keys_string = resolved_info.get("keys_string", "")
    extra_headers = resolved_info.get("extra_headers", {})
    task["media_type"] = resolved_info.get("media_type", "Standard")
    task["resolved_url"] = target_url
    logger.info(f"Task {task_id}: Resolved link [{task['media_type']}] -> {target_url[:75]}...")

    # yt-dlp format selection
    if resolution == "audio":
        format_spec = "bestaudio/best"
        outtmpl = str(DOWNLOADS_DIR / f"{task_id}_%(title)s.%(ext)s")
        if custom_name.strip():
            safe_name = sanitize_filename(custom_name)
            outtmpl = str(DOWNLOADS_DIR / f"{task_id}_{safe_name}.%(ext)s")
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        if resolution == "best":
            format_spec = "bestvideo+bestaudio/best"
        else:
            try:
                res_num = int(resolution)
                format_spec = f"bestvideo[height<={res_num}]+bestaudio/best[height<={res_num}]/best"
            except ValueError:
                format_spec = "bestvideo+bestaudio/best"

        outtmpl = str(DOWNLOADS_DIR / f"{task_id}_%(title)s.%(ext)s")
        if custom_name.strip():
            safe_name = sanitize_filename(custom_name)
            outtmpl = str(DOWNLOADS_DIR / f"{task_id}_{safe_name}.%(ext)s")
        postprocessors = []

    def progress_hook(d):
        if task.get("cancelled", False):
            raise Exception("Download cancelled by user.")

        status = d.get("status")
        if status == "downloading":
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total_bytes > 0:
                task["progress"] = round((downloaded / total_bytes) * 100, 1)
            else:
                task["progress"] = 0.0

            speed = d.get("speed")
            task["speed_str"] = f"{format_bytes(speed)}/s" if speed else "Calculating..."
            task["eta_str"] = format_seconds(d.get("eta"))
            task["filesize_str"] = format_bytes(total_bytes) if total_bytes > 0 else "Unknown"

            filename = d.get("filename")
            if filename and not task.get("resolved_filename"):
                task["resolved_filename"] = Path(filename).name
        elif status == "finished":
            task["progress"] = 99.0
            task["status"] = "processing"
            task["speed_str"] = "Finalizing..."
            task["eta_str"] = "00:00"

    ydl_opts = {
        'format': format_spec,
        'outtmpl': outtmpl,
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'retries': 20,
        'fragment_retries': 20,
        'noplaylist': True,
        'nocheckcertificate': True,
        'allow_unplayable_formats': True,
    }

    if extra_headers:
        ydl_opts['http_headers'] = extra_headers

    if postprocessors:
        ydl_opts['postprocessors'] = postprocessors

    if Path(COOKIES_FILE).exists() and ("youtube.com" in target_url or "youtu.be" in target_url):
        ydl_opts['cookiefile'] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info first
            try:
                info = ydl.extract_info(target_url, download=False)
                extracted_title = info.get('title', 'Video')
            except Exception as e:
                logger.warning(f"Metadata extraction fallback for {task_id}: {e}")
                extracted_title = custom_name.strip() or f"Video_{task_id}"
            task["display_title"] = custom_name.strip() if custom_name.strip() else extracted_title

            # Perform download
            ydl.download([target_url])

        # Decrypt DRM if keys_string exists and mp4decrypt is available
        if keys_string and '--key' in keys_string:
            mp4decrypt_bin = shutil.which("mp4decrypt")
            if mp4decrypt_bin:
                task["status"] = "processing"
                task["speed_str"] = "Decrypting DRM..."
                task_files = list(DOWNLOADS_DIR.glob(f"{task_id}_*"))
                for tf in task_files:
                    if tf.suffix in [".mp4", ".m4a"]:
                        dec_file = tf.with_name(f"dec_{tf.name}")
                        decrypt_cmd = f'"{mp4decrypt_bin}" {keys_string} "{tf}" "{dec_file}"'
                        logger.info(f"Decrypting with mp4decrypt: {decrypt_cmd}")
                        subprocess.run(decrypt_cmd, shell=True)
                        if dec_file.exists() and dec_file.stat().st_size > 0:
                            tf.unlink(missing_ok=True)
                            dec_file.rename(tf)
            else:
                logger.info(f"DRM keys found ({keys_string}) but mp4decrypt not installed in PATH.")

        # Find the final file on disk
        candidates = list(DOWNLOADS_DIR.glob(f"{task_id}_*"))
        if candidates:
            final_file = candidates[0]
            task["file_path"] = str(final_file)
            task["resolved_filename"] = final_file.name
            friendly_name = final_file.name.replace(f"{task_id}_", "")
            task["friendly_filename"] = friendly_name
            task["filesize_str"] = format_bytes(final_file.stat().st_size)
            task["download_url"] = f"/api/files/{task_id}"

            # Auto-upload to YouTube if enabled
            if task.get("upload_to_youtube"):
                task["status"] = "uploading"
                task["speed_str"] = "Uploading to YouTube..."
                task["progress"] = 0.0
                logger.info(f"Task {task_id}: Starting auto-upload to YouTube...")

                def yt_progress(pct):
                    task["progress"] = pct
                    task["speed_str"] = f"YouTube: {pct}%"

                yt_title = task.get("display_title") or friendly_name
                if YOUTUBE_UPLOADER_AVAILABLE:
                    yt_res = upload_video_to_youtube(
                        file_path=str(final_file),
                        title=yt_title,
                        description=f"Auto-downloaded & uploaded via OmniStream\nOriginal Source: {original_url}",
                        privacy_status=task.get("privacy_status", "unlisted"),
                        progress_callback=yt_progress,
                        user_id=task.get("user_id")
                    )
                    if yt_res.get("success"):
                        task["youtube_url"] = yt_res.get("url")
                        task["youtube_status"] = "uploaded"
                        logger.info(f"Task {task_id}: YouTube upload succeeded -> {yt_res.get('url')}")
                    else:
                        task["youtube_status"] = f"Upload failed: {yt_res.get('error')}"
                        logger.warning(f"Task {task_id}: YouTube upload failed -> {yt_res.get('error')}")
                else:
                    task["youtube_status"] = "YouTube uploader module not configured."

            task["status"] = "completed"
            task["progress"] = 100.0
            task["completed_at"] = time.time()
            logger.info(f"Task {task_id} completed successfully: {final_file.name}")
        else:
            task["status"] = "failed"
            task["error_message"] = "Output file not found after download completed."
    except Exception as e:
        logger.error(f"Error downloading task {task_id}: {str(e)}")
        task["status"] = "failed"
        task["error_message"] = str(e)
    finally:
        task["finished_at"] = time.time()


async def queue_worker_loop():
    """Background task to pull items from the download queue."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            task_id = await download_queue.get()
            task = tasks_db.get(task_id)
            if task and not task.get("cancelled", False):
                await loop.run_in_executor(thread_pool, sync_download_worker, task_id)
            download_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Queue worker exception: {e}")
            await asyncio.sleep(1)


# Cloudflare D1 Auth API URL (configurable via env or cloudflare_api.txt)
CLOUDFLARE_AUTH_API = os.environ.get("CLOUDFLARE_AUTH_API", "").rstrip("/")
if not CLOUDFLARE_AUTH_API and (BASE_DIR / "cloudflare_api.txt").exists():
    try:
        CLOUDFLARE_AUTH_API = (BASE_DIR / "cloudflare_api.txt").read_text().strip().rstrip("/")
    except Exception:
        pass


# --- Helper Functions ---
def create_task_entry(
    url: str, 
    custom_name: Optional[str], 
    resolution: str,
    upload_to_youtube: bool = False,
    privacy_status: str = "unlisted",
    user_id: Optional[str] = None
) -> str:
    task_id = str(uuid.uuid4())[:8]
    is_cp = any(k in url.lower() for k in ["classplusapp", "testbook.com", "akamai", "media-cdn"])
    tasks_db[task_id] = {
        "id": task_id,
        "url": url.strip(),
        "custom_name": custom_name.strip() if custom_name else "",
        "display_title": custom_name.strip() if custom_name else url.strip(),
        "resolution": resolution,
        "media_type": "ClassPlus / Akamai" if is_cp else "Standard",
        "resolved_url": None,
        "upload_to_youtube": upload_to_youtube,
        "privacy_status": privacy_status,
        "user_id": user_id,
        "youtube_url": None,
        "youtube_status": None,
        "status": "queued",
        "progress": 0.0,
        "speed_str": "--",
        "eta_str": "--",
        "filesize_str": "--",
        "file_path": None,
        "resolved_filename": None,
        "friendly_filename": None,
        "download_url": None,
        "error_message": None,
        "cancelled": False,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
    }
    return task_id



# --- API Routes ---
def background_yt_upload(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        return
    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        task["youtube_status"] = "Error: File missing from disk"
        return
    task["status"] = "uploading"
    task["speed_str"] = "Uploading to YouTube..."
    task["progress"] = 0.0

    def yt_progress(pct):
        task["progress"] = pct
        task["speed_str"] = f"YouTube: {pct}%"

    yt_title = task.get("display_title") or task.get("friendly_filename") or "Uploaded Video"
    if YOUTUBE_UPLOADER_AVAILABLE:
        yt_res = upload_video_to_youtube(
            file_path=file_path,
            title=yt_title,
            description=f"Auto-downloaded & uploaded via OmniStream\nOriginal Source: {task.get('url', '')}",
            privacy_status=task.get("privacy_status", "unlisted"),
            progress_callback=yt_progress,
            user_id=task.get("user_id")
        )
        if yt_res.get("success"):
            task["youtube_url"] = yt_res.get("url")
            task["youtube_status"] = "uploaded"
            task["status"] = "completed"
            task["progress"] = 100.0
            logger.info(f"Task {task_id}: YouTube upload succeeded -> {yt_res.get('url')}")
        else:
            task["youtube_status"] = f"Upload failed: {yt_res.get('error')}"
            task["status"] = "completed"
            task["progress"] = 100.0
            logger.warning(f"Task {task_id}: YouTube upload failed -> {yt_res.get('error')}")


@app.get("/api/youtube/status")
async def get_youtube_status(user_id: Optional[str] = Query(default=None)):
    # 1. Check Cloudflare if configured and user_id passed
    if user_id and CLOUDFLARE_AUTH_API:
        try:
            cf_res = requests.get(f"{CLOUDFLARE_AUTH_API}/api/user/token?user_id={user_id}", timeout=5)
            cdata = cf_res.json() if "application/json" in cf_res.headers.get("content-type", "") else {}
            if cf_res.status_code == 200:
                return {
                    "available": True,
                    "authenticated": cdata.get("authenticated", False),
                    "channel_name": cdata.get("channel_name", "Connected"),
                    "mode": "cloudflare"
                }
            elif cf_res.status_code == 404:
                return {
                    "available": True,
                    "authenticated": False,
                    "mode": "cloudflare"
                }
        except Exception as e:
            logger.warning(f"Error checking user status with Cloudflare API: {e}")

    # 2. Local fallback
    srv = get_youtube_service(user_id=user_id) if YOUTUBE_UPLOADER_AVAILABLE else None
    return {
        "available": YOUTUBE_UPLOADER_AVAILABLE,
        "authenticated": srv is not None,
        "mode": "local"
    }


@app.post("/api/user/save-secrets")
async def save_user_secrets(payload: dict):
    user_id = payload.get("user_id")
    client_secrets = payload.get("client_secrets")
    if not user_id or not client_secrets:
        raise HTTPException(status_code=400, detail="user_id and client_secrets are required")

    if not CLOUDFLARE_AUTH_API:
        # Save locally if no Cloudflare configured
        try:
            with open(BASE_DIR / "client_secrets.json", "w") as f:
                json.dump(client_secrets, f, indent=2)
            return {"success": True, "message": "Saved client_secrets.json locally on server."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    try:
        cf_res = requests.post(
            f"{CLOUDFLARE_AUTH_API}/api/user/save-secrets",
            json={"user_id": user_id, "client_secrets": client_secrets},
            timeout=10
        )
        return cf_res.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to Cloudflare API: {str(e)}")


@app.get("/api/user/login-url")
async def get_user_login_url(user_id: str):
    if not CLOUDFLARE_AUTH_API:
        return {"url": None, "error": "Cloudflare Auth API URL is not set"}
    return {"url": f"{CLOUDFLARE_AUTH_API}/api/auth/google/login?user_id={user_id}"}


@app.post("/api/youtube/upload/{task_id}")
async def manual_youtube_upload(task_id: str, background_tasks: BackgroundTasks):
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    srv = get_youtube_service(user_id=task.get("user_id")) if YOUTUBE_UPLOADER_AVAILABLE else None
    if not srv:
        raise HTTPException(status_code=400, detail="YouTube API not authenticated. Please connect your YouTube channel first.")
    
    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Downloaded video file not found on disk")

    background_tasks.add_task(background_yt_upload, task_id)
    return {"success": True, "message": "YouTube upload started"}


@app.post("/api/download/single")
async def download_single(req: SingleDownloadRequest):
    url = req.url.strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Invalid URL provided. Please provide a full HTTP/HTTPS URL.")

    task_id = create_task_entry(
        url=url, 
        custom_name=req.custom_name, 
        resolution=req.resolution,
        upload_to_youtube=req.upload_to_youtube,
        privacy_status=req.privacy_status,
        user_id=req.user_id
    )
    await download_queue.put(task_id)
    return {"success": True, "task": tasks_db[task_id]}


@app.post("/api/download/batch")
async def download_batch(req: BatchDownloadRequest):
    added_tasks = []

    # Case 1: Structured items list
    if req.items:
        for item in req.items:
            u = item.url.strip()
            if u.startswith("http://") or u.startswith("https://"):
                res = item.resolution or req.default_resolution
                yt_up = item.upload_to_youtube or req.upload_to_youtube
                priv = item.privacy_status or req.default_privacy
                uid = item.user_id or req.user_id
                tid = create_task_entry(u, item.custom_name, res, yt_up, priv, user_id=uid)
                await download_queue.put(tid)
                added_tasks.append(tasks_db[tid])

    # Case 2: Raw multiline text
    if req.raw_text:
        lines = [line.strip() for line in req.raw_text.splitlines() if line.strip()]
        for line in lines:
            extracted_url = None
            extracted_name = None

            if " | " in line:
                parts = line.split(" | ", 1)
                if parts[0].startswith("http"):
                    extracted_url, extracted_name = parts[0], parts[1]
                else:
                    extracted_name, extracted_url = parts[0], parts[1]
            elif " : " in line:
                parts = line.split(" : ", 1)
                if parts[0].startswith("http"):
                    extracted_url, extracted_name = parts[0], parts[1]
                else:
                    extracted_name, extracted_url = parts[0], parts[1]
            elif "://" in line:
                match = re.search(r'(https?://[^\s]+)', line)
                if match:
                    extracted_url = match.group(1)
                    name_candidate = line.replace(extracted_url, "").strip(" -:|")
                    if name_candidate:
                        extracted_name = name_candidate

            if extracted_url and (extracted_url.startswith("http://") or extracted_url.startswith("https://")):
                tid = create_task_entry(
                    url=extracted_url, 
                    custom_name=extracted_name, 
                    resolution=req.default_resolution,
                    upload_to_youtube=req.upload_to_youtube,
                    privacy_status=req.default_privacy,
                    user_id=req.user_id
                )
                await download_queue.put(tid)
                added_tasks.append(tasks_db[tid])

    if not added_tasks:
        raise HTTPException(status_code=400, detail="No valid URLs found in request.")

    return {"success": True, "count": len(added_tasks), "tasks": added_tasks}



@app.get("/api/queue")
async def get_queue():
    # Return tasks sorted by creation time (most recent first)
    sorted_tasks = sorted(tasks_db.values(), key=lambda t: t.get("created_at", 0), reverse=True)
    return {"tasks": sorted_tasks}


@app.get("/api/queue/{task_id}")
async def get_task_status(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/api/queue/{task_id}")
async def cancel_or_delete_task(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task["cancelled"] = True
    # If file exists, clean up
    file_path = task.get("file_path")
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            logger.warning(f"Failed to delete file {file_path}: {e}")

    tasks_db.pop(task_id, None)
    return {"success": True, "message": f"Task {task_id} deleted."}


@app.post("/api/queue/clear")
async def clear_finished_tasks():
    to_delete = [
        tid for tid, t in tasks_db.items()
        if t.get("status") in ("completed", "failed")
    ]
    for tid in to_delete:
        tasks_db.pop(tid, None)
    return {"success": True, "cleared_count": len(to_delete)}


@app.get("/api/files/{task_id}")
async def download_completed_file(task_id: str):
    task = tasks_db.get(task_id)
    if not task or task.get("status") != "completed":
        raise HTTPException(status_code=404, detail="File is not available or download not completed.")

    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Downloaded file not found on disk.")

    friendly_name = task.get("friendly_filename") or Path(file_path).name
    return FileResponse(
        path=file_path,
        filename=friendly_name,
        media_type="application/octet-stream"
    )


@app.get("/api/stats")
async def get_system_stats():
    total = len(tasks_db)
    active = sum(1 for t in tasks_db.values() if t.get("status") in ("queued", "downloading", "processing"))
    completed = sum(1 for t in tasks_db.values() if t.get("status") == "completed")
    failed = sum(1 for t in tasks_db.values() if t.get("status") == "failed")

    # Compute downloads folder size
    total_size = sum(f.stat().st_size for f in DOWNLOADS_DIR.glob("*") if f.is_file())

    return {
        "total_tasks": total,
        "active_tasks": active,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "disk_usage": format_bytes(total_size),
        "queue_length": download_queue.qsize()
    }


# Mount static assets
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
