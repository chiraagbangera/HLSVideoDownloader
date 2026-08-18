import asyncio
import json
import ipaddress
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for
from playwright.async_api import async_playwright

app = Flask(__name__)

APP_NAME = "HLS Video Downloader"

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/mnt/Videos")).resolve()
TEMP_DOWNLOAD_DIR = Path(
    os.environ.get("TEMP_DOWNLOAD_DIR", "/var/tmp/hls-video-downloader")
).resolve()

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "/usr/bin/ffprobe")
CHROMIUM_BIN = os.environ.get("CHROMIUM_BIN", "").strip()

MAX_CONCURRENT_DOWNLOADS = max(
    1, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
)
MAX_LOG_LINES = max(50, int(os.environ.get("MAX_LOG_LINES", "500")))
COMPLETED_JOB_TTL_HOURS = max(
    1, int(os.environ.get("COMPLETED_JOB_TTL_HOURS", "24"))
)
EXTRACT_TIMEOUT_SECONDS = max(
    10, int(os.environ.get("EXTRACT_TIMEOUT_SECONDS", "45"))
)
PAGE_LOAD_TIMEOUT_SECONDS = max(
    10, int(os.environ.get("PAGE_LOAD_TIMEOUT_SECONDS", "45"))
)
ADBLOCK_ENABLED = os.environ.get("ADBLOCK_ENABLED", "1").lower() not in {
    "0",
    "false",
    "no",
}
ADBLOCK_FALLBACK = os.environ.get("ADBLOCK_FALLBACK", "1").lower() not in {
    "0",
    "false",
    "no",
}
CHROMIUM_NO_SANDBOX = os.environ.get("CHROMIUM_NO_SANDBOX", "0").lower() in {
    "1",
    "true",
    "yes",
}

DEFAULT_USER_AGENT = os.environ.get(
    "BROWSER_USER_AGENT",
    (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
)

DEFAULT_ADBLOCK_DOMAINS = [
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adservice.google.com",
    "adsystem.com",
    "adnxs.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "criteo.net",
    "pubmatic.com",
    "openx.net",
    "rubiconproject.com",
    "casalemedia.com",
    "scorecardresearch.com",
    "quantserve.com",
    "google-analytics.com",
    "googletagmanager.com",
    "inmobi.com",
    "getpublica.com",
    "vntsm.com",
]

EXTRA_ADBLOCK_DOMAINS = [
    item.strip().lower()
    for item in os.environ.get("ADBLOCK_DOMAINS", "").split(",")
    if item.strip()
]
ADBLOCK_DOMAINS = tuple(dict.fromkeys(DEFAULT_ADBLOCK_DOMAINS + EXTRA_ADBLOCK_DOMAINS))

AD_URL_HINTS = (
    "/ads/",
    "/ad/",
    "advert",
    "doubleclick",
    "googlesyndication",
    "vast",
    "vpaid",
    "ima3",
    "preroll",
    "pre-roll",
    "midroll",
    "mid-roll",
)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
job_queue: queue.Queue[str] = queue.Queue()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def is_direct_hls_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return bool(re.search(r"\.m3u8(?:$|[/?])", parsed.path, re.IGNORECASE)) or bool(
            re.search(r"\.m3u8(?:\?|$)", value, re.IGNORECASE)
        )
    except ValueError:
        return False


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value or "")
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    return value[:180] or "video"


def ensure_mp4_filename(value: str) -> str:
    value = sanitize_filename(value)
    if not value.lower().endswith(".mp4"):
        value += ".mp4"
    return value


def unique_destination(filename: str) -> Path:
    candidate = DOWNLOAD_DIR / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = DOWNLOAD_DIR / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def append_log(job_id: str, line: str) -> None:
    line = line.rstrip()
    if not line:
        return

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        job["log"].append(line)
        if len(job["log"]) > MAX_LOG_LINES:
            job["log"] = job["log"][-MAX_LOG_LINES:]


def get_queue_position(job_id: str) -> int | None:
    with job_queue.mutex:
        pending_ids = list(job_queue.queue)
    try:
        return pending_ids.index(job_id) + 1
    except ValueError:
        return None


def set_job(job_id: str, **updates) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(updates)


def safe_origin(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        pass
    return ""


def stream_request_headers(job: dict) -> str:
    headers = []
    internal = job.get("stream_headers") or {}

    referer = internal.get("referer") or job.get("source_url")
    origin = internal.get("origin") or safe_origin(job.get("source_url", ""))
    cookie = internal.get("cookie")
    authorization = internal.get("authorization")

    if referer:
        headers.append(f"Referer: {referer}")
    if origin:
        headers.append(f"Origin: {origin}")
    if cookie:
        headers.append(f"Cookie: {cookie}")
    if authorization:
        headers.append(f"Authorization: {authorization}")

    return "\r\n".join(headers) + ("\r\n" if headers else "")


def parse_ffmpeg_time(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


def probe_duration(job: dict) -> float | None:
    stream_url = job.get("stream_url")
    if not stream_url:
        return None

    command = [
        FFPROBE_BIN,
        "-v",
        "error",
        "-user_agent",
        job.get("stream_user_agent") or DEFAULT_USER_AGENT,
    ]

    headers = stream_request_headers(job)
    if headers:
        command += ["-headers", headers]

    command += [
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        stream_url,
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = float(result.stdout.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def build_ffmpeg_command(job: dict, temp_file: Path) -> list[str]:
    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-user_agent",
        job.get("stream_user_agent") or DEFAULT_USER_AGENT,
    ]

    headers = stream_request_headers(job)
    if headers:
        command += ["-headers", headers]

    # No explicit -map is used. FFmpeg's normal automatic stream selection
    # prefers the highest-resolution video and the audio stream with the most
    # channels when the HLS manifest exposes multiple choices.
    command += [
        "-i",
        job["stream_url"],
        "-sn",
        "-dn",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(temp_file),
    ]
    return command


def custom_chromium_binary() -> str | None:
    if not CHROMIUM_BIN:
        return None
    if Path(CHROMIUM_BIN).exists():
        return CHROMIUM_BIN
    raise RuntimeError(f"CHROMIUM_BIN does not exist: {CHROMIUM_BIN}")


def host_matches_blocklist(hostname: str) -> bool:
    hostname = (hostname or "").lower().strip(".")
    if not hostname:
        return False
    return any(hostname == domain or hostname.endswith("." + domain) for domain in ADBLOCK_DOMAINS)


def looks_like_ad_url(url: str) -> bool:
    lower = url.lower()
    try:
        hostname = urlparse(url).hostname or ""
    except ValueError:
        hostname = ""
    return host_matches_blocklist(hostname) or any(hint in lower for hint in AD_URL_HINTS)


def hostname_is_ip(url: str) -> bool:
    try:
        hostname = urlparse(url).hostname or ""
        if not hostname:
            return False
        ipaddress.ip_address(hostname)
        return True
    except (ValueError, TypeError):
        return False


def is_hls_candidate(url: str, content_type: str = "") -> bool:
    lower_url = url.lower()
    lower_type = (content_type or "").lower()
    return (
        ".m3u8" in lower_url
        or "application/vnd.apple.mpegurl" in lower_type
        or "application/x-mpegurl" in lower_type
        or "audio/mpegurl" in lower_type
    )


class BrowserManager:
    """Own one Chromium process on one asyncio loop and create a context/page per extraction."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="chromium-loop",
        )
        self.thread.start()
        self._playwright = None
        self._browser = None
        self._browser_lock = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _ensure_browser(self):
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()

        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser

            self._playwright = await async_playwright().start()

            args = [
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                "--mute-audio",
            ]

            launch_options = {
                "headless": True,
                "args": args,
                "chromium_sandbox": not CHROMIUM_NO_SANDBOX,
            }
            chromium_path = custom_chromium_binary()
            if chromium_path:
                launch_options["executable_path"] = chromium_path

            self._browser = await self._playwright.chromium.launch(**launch_options)
            return self._browser

    def health(self) -> dict:
        future = asyncio.run_coroutine_threadsafe(self._health(), self.loop)
        return future.result(timeout=20)

    async def _health(self) -> dict:
        browser = await self._ensure_browser()
        executable = None
        if CHROMIUM_BIN:
            executable = CHROMIUM_BIN
        elif self._playwright is not None:
            executable = self._playwright.chromium.executable_path
        return {
            "connected": browser.is_connected(),
            "executable": executable,
            "sandbox_enabled": not CHROMIUM_NO_SANDBOX,
        }

    def extract(self, page_url: str, job_id: str) -> dict:
        future = asyncio.run_coroutine_threadsafe(
            self._extract_with_fallback(page_url, job_id),
            self.loop,
        )
        timeout = (EXTRACT_TIMEOUT_SECONDS + PAGE_LOAD_TIMEOUT_SECONDS + 15) * (
            2 if ADBLOCK_ENABLED and ADBLOCK_FALLBACK else 1
        )
        return future.result(timeout=timeout)

    async def _extract_with_fallback(self, page_url: str, job_id: str) -> dict:
        if is_direct_hls_url(page_url):
            return {
                "stream_url": page_url,
                "page_title": "video",
                "user_agent": DEFAULT_USER_AGENT,
                "headers": {
                    "referer": page_url,
                    "origin": safe_origin(page_url),
                },
                "adblock_used": False,
                "direct_hls": True,
            }

        attempts = [ADBLOCK_ENABLED]
        if ADBLOCK_ENABLED and ADBLOCK_FALLBACK:
            attempts.append(False)

        last_error = None
        for block_ads in attempts:
            try:
                if block_ads:
                    append_log(job_id, "Opening page in headless Chromium with network ad blocking")
                else:
                    append_log(job_id, "Retrying extraction without ad blocking")
                return await self._extract_once(page_url, job_id, block_ads)
            except Exception as exc:
                last_error = exc
                append_log(job_id, f"Extraction attempt failed: {exc}")

        raise RuntimeError(f"Could not find an HLS stream: {last_error}")

    async def _extract_once(self, page_url: str, job_id: str, block_ads: bool) -> dict:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            java_script_enabled=True,
            ignore_https_errors=False,
            viewport={"width": 1280, "height": 720},
        )

        candidate_future = self.loop.create_future()
        candidates: list[dict] = []

        async def route_handler(route):
            req = route.request
            url = req.url
            try:
                hostname = urlparse(url).hostname or ""
            except ValueError:
                hostname = ""

            if block_ads and host_matches_blocklist(hostname):
                await route.abort()
                return

            # Images/fonts do not help HLS extraction and consume memory/bandwidth.
            if req.resource_type in {"image", "font"}:
                await route.abort()
                return

            await route.continue_()

        await context.route("**/*", route_handler)
        page = await context.new_page()
        page.set_default_timeout(1200)

        def remember_candidate(
            url: str,
            content_type: str,
            request_headers: dict,
            manifest_text: str = "",
            settle: bool = False,
            source: str = "unknown",
            status: int | None = None,
        ) -> None:
            if not is_hls_candidate(url, content_type):
                return
            if looks_like_ad_url(url):
                return

            # DOM/performance entries can contain placeholder/signed IP URLs that
            # the player later replaces with a real CDN hostname. Do not let an
            # unverified raw-IP fallback outrank an observed playable response.
            raw_ip = hostname_is_ip(url)
            valid_manifest = manifest_text.lstrip().startswith("#EXTM3U")

            # If we actually received a response body and it is not an HLS
            # manifest, it is not a usable candidate.
            if manifest_text and not valid_manifest:
                return

            score = 0
            lower_type = (content_type or "").lower()

            if ".m3u8" in url.lower():
                score += 100
            if "mpegurl" in lower_type:
                score += 75
            if url.startswith("https://"):
                score += 5

            # Strongly prefer streams that Chromium actually requested and
            # successfully received over URLs merely embedded in page markup.
            if source == "network":
                score += 250
            elif source == "performance":
                score += 25

            if valid_manifest:
                score += 250

            if "#EXT-X-STREAM-INF" in manifest_text:
                score += 50
            elif "#EXTINF" in manifest_text:
                score += 25

            # A hostname/CDN URL is usually the final browser-selected stream.
            # Raw IP URLs are often placeholders. They remain usable as a last
            # resort only when they were verified by a real successful response.
            if raw_ip:
                score -= 250
            else:
                score += 125

            candidate = {
                "url": url,
                "content_type": content_type,
                "headers": request_headers,
                "score": score,
                "seen": time.monotonic(),
                "source": source,
                "raw_ip": raw_ip,
                "valid_manifest": valid_manifest,
                "status": status,
            }
            candidates.append(candidate)

            host = urlparse(url).hostname or "unknown"
            append_log(
                job_id,
                f"HLS candidate: source={source} host={host} "
                f"score={score} verified={'yes' if valid_manifest else 'no'}",
            )

            # Only settle early on a verified network response using a normal
            # hostname. Raw IPs and DOM fallbacks must wait so the real CDN
            # request has time to appear.
            if (
                settle
                and source == "network"
                and valid_manifest
                and not raw_ip
                and not candidate_future.done()
            ):
                async def settle_candidate():
                    await asyncio.sleep(1.5)
                    if candidate_future.done():
                        return

                    verified_domain = [
                        item
                        for item in candidates
                        if item.get("source") == "network"
                        and item.get("valid_manifest")
                        and not item.get("raw_ip")
                    ]

                    if verified_domain:
                        best = max(
                            verified_domain,
                            key=lambda item: (item["score"], item["seen"]),
                        )
                        candidate_future.set_result(best)

                asyncio.create_task(settle_candidate())

        async def on_response(response):
            try:
                if response.status < 200 or response.status >= 300:
                    return

                content_type = response.headers.get("content-type", "")
                if not is_hls_candidate(response.url, content_type):
                    return
                if looks_like_ad_url(response.url):
                    return

                headers = await response.request.all_headers()
                manifest_text = ""

                try:
                    body = await response.body()
                    manifest_text = body[:262144].decode("utf-8", errors="ignore")
                except Exception:
                    pass

                remember_candidate(
                    response.url,
                    content_type,
                    headers,
                    manifest_text=manifest_text,
                    settle=True,
                    source="network",
                    status=response.status,
                )
            except Exception:
                pass

        page.on("response", on_response)

        try:
            try:
                await page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT_SECONDS * 1000,
                )
            except Exception as exc:
                append_log(job_id, f"Page load warning: {exc}")

            # Get a useful title before playback changes the document.
            page_title = ""
            try:
                page_title = await page.locator('meta[property="og:title"]').get_attribute("content") or ""
            except Exception:
                pass
            if not page_title:
                try:
                    page_title = await page.title()
                except Exception:
                    page_title = ""

            # Some sites place an HLS URL directly in the DOM. Keep it only as a
            # fallback; an observed network response is preferred because sites
            # may rewrite/select a different CDN URL at runtime.
            dom_candidates = []
            try:
                dom_candidates = await page.evaluate(
                    """
                    () => {
                        const urls = new Set();
                        const re = /https?:\\/\\/[^\\s\"'<>]+?\\.m3u8(?:\\?[^\\s\"'<>]*)?/gi;
                        const html = document.documentElement?.innerHTML || "";
                        for (const m of html.matchAll(re)) urls.add(m[0].replaceAll('&amp;', '&'));
                        for (const el of document.querySelectorAll('[src],[href],[data-src],[data-hls],[data-hls-link]')) {
                            for (const name of ['src','href','data-src','data-hls','data-hls-link']) {
                                const v = el.getAttribute(name);
                                if (v && v.includes('.m3u8')) {
                                    try { urls.add(new URL(v, location.href).href); } catch {}
                                }
                            }
                        }
                        return [...urls];
                    }
                    """
                )
            except Exception:
                dom_candidates = []

            for url in dom_candidates:
                if not looks_like_ad_url(url):
                    remember_candidate(url, "", {}, settle=False, source="dom")

            async def try_start_playback() -> None:
                try:
                    await page.evaluate(
                        """
                        () => {
                            for (const v of document.querySelectorAll('video')) {
                                try { v.muted = true; v.volume = 0; v.play().catch(() => {}); } catch {}
                            }
                        }
                        """
                    )
                except Exception:
                    pass

                selectors = [
                    "button[aria-label*='play' i]",
                    "[role='button'][aria-label*='play' i]",
                    "button[title*='play' i]",
                    "[class*='play-button' i]",
                    "[class~='play']",
                    "#play",
                    ".play-pause",
                    "video",
                ]
                for selector in selectors:
                    try:
                        locator = page.locator(selector).first
                        if await locator.is_visible(timeout=250):
                            await locator.click(timeout=700, force=True)
                            break
                    except Exception:
                        continue

            deadline = time.monotonic() + EXTRACT_TIMEOUT_SECONDS
            await try_start_playback()

            while time.monotonic() < deadline and not candidate_future.done():
                # If the DOM candidate was all we found, give network playback a
                # few seconds to produce the actual CDN request before using it.
                await asyncio.sleep(2)
                await try_start_playback()

                # Check performance resources too, as an additional generic path.
                try:
                    resources = await page.evaluate(
                        "performance.getEntriesByType('resource').map(e => e.name)"
                    )
                    for url in resources:
                        if is_hls_candidate(url) and not looks_like_ad_url(url):
                            remember_candidate(url, "", {}, settle=False, source="performance")
                except Exception:
                    pass

                if candidates and time.monotonic() > deadline - max(5, EXTRACT_TIMEOUT_SECONDS - 6):
                    # Prefer a verified browser response on a real hostname.
                    preferred = [
                        item
                        for item in candidates
                        if item.get("source") == "network"
                        and item.get("valid_manifest")
                        and not item.get("raw_ip")
                    ]

                    if preferred and not candidate_future.done():
                        candidate_future.set_result(
                            max(
                                preferred,
                                key=lambda item: (item["score"], item["seen"]),
                            )
                        )

            if not candidate_future.done():
                # Selection priority:
                # 1. Verified network manifest on hostname.
                # 2. Any verified network manifest (raw IP allowed only here).
                # 3. Non-IP fallback discovered from DOM/performance.
                verified_domain = [
                    item
                    for item in candidates
                    if item.get("source") == "network"
                    and item.get("valid_manifest")
                    and not item.get("raw_ip")
                ]
                verified_network = [
                    item
                    for item in candidates
                    if item.get("source") == "network"
                    and item.get("valid_manifest")
                ]
                non_ip_fallback = [
                    item
                    for item in candidates
                    if not item.get("raw_ip")
                ]

                pool = verified_domain or verified_network or non_ip_fallback

                if pool:
                    candidate_future.set_result(
                        max(
                            pool,
                            key=lambda item: (item["score"], item["seen"]),
                        )
                    )
                elif candidates:
                    hosts = sorted(
                        {
                            urlparse(item["url"]).hostname or "unknown"
                            for item in candidates
                        }
                    )
                    raise RuntimeError(
                        "Only unverified raw-IP HLS placeholders were found "
                        f"({', '.join(hosts)}); no playable CDN/network manifest "
                        f"was observed within {EXTRACT_TIMEOUT_SECONDS}s"
                    )
                else:
                    raise RuntimeError(
                        f"No non-ad HLS manifest was observed within {EXTRACT_TIMEOUT_SECONDS}s"
                    )

            candidate = await candidate_future

            cookies = await context.cookies()
            cookie_header = "; ".join(
                f"{cookie['name']}={cookie['value']}" for cookie in cookies
            )

            request_headers = {
                str(k).lower(): str(v) for k, v in (candidate.get("headers") or {}).items()
            }

            referer = request_headers.get("referer") or page_url
            origin = request_headers.get("origin") or safe_origin(page_url)
            authorization = request_headers.get("authorization", "")
            user_agent = request_headers.get("user-agent") or DEFAULT_USER_AGENT

            selected_host = urlparse(candidate["url"]).hostname or "unknown"
            append_log(
                job_id,
                f"Selected HLS manifest: host={selected_host} "
                f"source={candidate.get('source', 'unknown')} "
                f"verified={'yes' if candidate.get('valid_manifest') else 'no'}",
            )

            return {
                "stream_url": candidate["url"],
                "page_title": page_title or "video",
                "user_agent": user_agent,
                "headers": {
                    "referer": referer,
                    "origin": origin,
                    "cookie": cookie_header,
                    "authorization": authorization,
                },
                "adblock_used": block_ads,
                "direct_hls": False,
            }
        finally:
            await context.close()


browser_manager = BrowserManager()


def extract_stream_for_job(job_id: str, source_url: str) -> dict:
    return browser_manager.extract(source_url, job_id)


def download_worker(worker_number: int) -> None:
    while True:
        job_id = job_queue.get()
        temp_job_dir: Path | None = None

        try:
            with jobs_lock:
                job = jobs.get(job_id)
                if job is None:
                    continue
                job["status"] = "extracting"
                job["worker_number"] = worker_number
                job["started_at"] = utc_now()
                job["progress"] = 0.0
                job["message"] = "Finding HLS stream"
                source_url = job["source_url"]
                requested_title = job.get("requested_title", "")

            result = extract_stream_for_job(job_id, source_url)

            title = requested_title or result.get("page_title") or "video"
            filename = ensure_mp4_filename(title)

            with jobs_lock:
                job = jobs.get(job_id)
                if job is None:
                    raise RuntimeError("Job disappeared during extraction")
                job["title"] = title
                job["filename"] = filename
                job["stream_url"] = result["stream_url"]
                job["stream_user_agent"] = result.get("user_agent") or DEFAULT_USER_AGENT
                job["stream_headers"] = result.get("headers") or {}
                job["adblock_used"] = result.get("adblock_used")
                job["direct_hls"] = result.get("direct_hls", False)
                job["status"] = "downloading"
                job["message"] = "Probing stream"
                job_snapshot = dict(job)

            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

            temp_job_dir = TEMP_DOWNLOAD_DIR / job_id
            temp_job_dir.mkdir(parents=True, exist_ok=True)
            temp_file = temp_job_dir / "download.mp4"

            duration = probe_duration(job_snapshot)
            set_job(job_id, duration_seconds=duration, message="Downloading")

            with jobs_lock:
                fresh = jobs.get(job_id)
                if fresh is None:
                    raise RuntimeError("Job disappeared before FFmpeg start")
                job_snapshot = dict(fresh)

            command = build_ffmpeg_command(job_snapshot, temp_file)
            append_log(job_id, "$ ffmpeg <resolved HLS URL> -c copy ...")

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            set_job(job_id, process_id=process.pid)

            def stderr_reader() -> None:
                if process.stderr is None:
                    return
                for stderr_line in process.stderr:
                    append_log(job_id, stderr_line)

            stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
            stderr_thread.start()

            progress_state: dict[str, str] = {}
            if process.stdout is None:
                raise RuntimeError("ffmpeg did not provide a progress stream")

            for line in process.stdout:
                line = line.strip()
                if not line or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                progress_state[key] = value

                if key == "progress":
                    out_time = parse_ffmpeg_time(progress_state.get("out_time", ""))
                    total_size = progress_state.get("total_size")
                    speed = progress_state.get("speed")

                    percent = None
                    if out_time is not None and duration:
                        percent = min(100.0, max(0.0, out_time / duration * 100.0))

                    updates = {
                        "downloaded_seconds": out_time,
                        "speed": speed,
                    }
                    if total_size and total_size.isdigit():
                        updates["downloaded_bytes"] = int(total_size)
                    if percent is not None:
                        updates["progress"] = round(percent, 2)

                    set_job(job_id, **updates)
                    progress_state.clear()

            return_code = process.wait()
            stderr_thread.join(timeout=2)

            if return_code != 0:
                raise RuntimeError(f"ffmpeg exited with code {return_code}")

            if not temp_file.exists() or temp_file.stat().st_size == 0:
                raise RuntimeError("ffmpeg completed but no output file was created")

            set_job(job_id, status="moving", message="Moving completed file to NAS")

            with jobs_lock:
                current_job = jobs.get(job_id)
                if current_job is None:
                    raise RuntimeError("Job disappeared before final move")
                destination = unique_destination(current_job["filename"])

            shutil.move(str(temp_file), str(destination))

            output_bytes = None
            try:
                output_bytes = destination.stat().st_size
            except OSError:
                pass

            set_job(
                job_id,
                return_code=0,
                status="completed",
                progress=100.0,
                finished_at=utc_now(),
                process_id=None,
                message="Completed",
                output_path=str(destination),
                output_bytes=output_bytes,
            )

        except Exception as exc:
            append_log(job_id, f"Server error: {exc}")
            set_job(
                job_id,
                status="failed",
                finished_at=utc_now(),
                process_id=None,
                message=str(exc),
            )

        finally:
            if temp_job_dir is not None:
                try:
                    shutil.rmtree(temp_job_dir, ignore_errors=True)
                except OSError:
                    pass
            job_queue.task_done()


def start_workers() -> None:
    for worker_number in range(1, MAX_CONCURRENT_DOWNLOADS + 1):
        thread = threading.Thread(
            target=download_worker,
            args=(worker_number,),
            daemon=True,
            name=f"hls-worker-{worker_number}",
        )
        thread.start()


def cleanup_worker() -> None:
    while True:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=COMPLETED_JOB_TTL_HOURS)
        to_delete: list[str] = []

        with jobs_lock:
            for job_id, job in jobs.items():
                if job.get("status") not in {"completed", "failed"}:
                    continue
                finished_at = parse_iso(job.get("finished_at"))
                if finished_at and finished_at < cutoff:
                    to_delete.append(job_id)

            for job_id in to_delete:
                jobs.pop(job_id, None)

        time.sleep(600)


def start_cleanup_worker() -> None:
    threading.Thread(
        target=cleanup_worker,
        daemon=True,
        name="job-cleanup",
    ).start()


def create_job(source_url: str, title: str = "") -> dict:
    job_id = uuid.uuid4().hex[:12]
    requested_title = title.strip()

    job = {
        "id": job_id,
        "source_url": source_url,
        "requested_title": requested_title,
        "title": requested_title or "Waiting for page title",
        "filename": ensure_mp4_filename(requested_title or "video"),
        "status": "queued",
        "message": "Queued",
        "progress": 0.0,
        "duration_seconds": None,
        "downloaded_seconds": None,
        "downloaded_bytes": None,
        "output_bytes": None,
        "speed": None,
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "return_code": None,
        "worker_number": None,
        "process_id": None,
        "output_path": None,
        "adblock_used": None,
        "direct_hls": is_direct_hls_url(source_url),
        "stream_url": None,
        "stream_user_agent": None,
        "stream_headers": {},
        "log": [],
    }

    with jobs_lock:
        jobs[job_id] = job

    job_queue.put(job_id)
    return job


def public_job(job: dict) -> dict:
    # Do not expose short-lived signed manifests, cookies, or auth headers in the UI/API.
    result = {
        key: value
        for key, value in job.items()
        if key not in {"stream_url", "stream_headers", "stream_user_agent"}
    }
    result["log"] = list(job["log"])
    result["queue_position"] = (
        get_queue_position(job["id"]) if job["status"] == "queued" else None
    )
    return result


start_workers()
start_cleanup_worker()


@app.get("/")
def index():
    return render_template(
        "index.html",
        app_name=APP_NAME,
        download_dir=str(DOWNLOAD_DIR),
        temp_download_dir=str(TEMP_DOWNLOAD_DIR),
        max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
        ttl_hours=COMPLETED_JOB_TTL_HOURS,
        adblock_enabled=ADBLOCK_ENABLED,
        adblock_fallback=ADBLOCK_FALLBACK,
    )


@app.post("/download")
def download_form():
    source_url = request.form.get("url", "").strip()
    title = request.form.get("title", "").strip()

    if not is_valid_url(source_url):
        return redirect(url_for("index", error="Invalid URL"))

    job = create_job(source_url, title)
    return redirect(url_for("index", queued=job["id"]))


@app.post("/api/download")
def download_api():
    data = request.get_json(silent=True) or {}
    source_url = str(data.get("url", "")).strip()
    title = str(data.get("title", "")).strip()

    if not is_valid_url(source_url):
        return jsonify({"error": "Invalid or missing http/https URL"}), 400

    job = create_job(source_url, title)
    return jsonify({"job": public_job(job)}), 202


@app.get("/api/jobs")
def all_jobs():
    with jobs_lock:
        snapshot = [public_job(job) for job in list(jobs.values())[-100:][::-1]]

    return jsonify(
        {
            "app_name": APP_NAME,
            "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
            "download_dir": str(DOWNLOAD_DIR),
            "temp_download_dir": str(TEMP_DOWNLOAD_DIR),
            "adblock_enabled": ADBLOCK_ENABLED,
            "jobs": snapshot,
        }
    )


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(public_job(job))


@app.get("/api/health")
def health():
    browser_info = None
    browser_error = None
    try:
        browser_info = browser_manager.health()
    except Exception as exc:
        browser_error = str(exc)

    ffmpeg_exists = Path(FFMPEG_BIN).exists()
    ffprobe_exists = Path(FFPROBE_BIN).exists()
    browser_ok = bool(browser_info and browser_info.get("connected"))

    return jsonify(
        {
            "ok": browser_ok and ffmpeg_exists and ffprobe_exists,
            "ffmpeg": FFMPEG_BIN,
            "ffmpeg_exists": ffmpeg_exists,
            "ffprobe": FFPROBE_BIN,
            "ffprobe_exists": ffprobe_exists,
            "chromium": browser_info,
            "chromium_error": browser_error,
            "download_dir": str(DOWNLOAD_DIR),
            "temp_download_dir": str(TEMP_DOWNLOAD_DIR),
        }
    )



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=99, threaded=True)
