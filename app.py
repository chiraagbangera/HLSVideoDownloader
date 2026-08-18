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

from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for

app = Flask(__name__)

APP_NAME = "HLS Video Downloader"
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/mnt/Videos")).resolve()
TEMP_DOWNLOAD_DIR = Path(
    os.environ.get("TEMP_DOWNLOAD_DIR", "/var/tmp/hls-video-downloader/jobs")
).resolve()
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "/usr/bin/ffprobe")
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")))
MAX_LOG_LINES = max(50, int(os.environ.get("MAX_LOG_LINES", "500")))
COMPLETED_JOB_TTL_HOURS = max(
    1, int(os.environ.get("COMPLETED_JOB_TTL_HOURS", "24"))
)
DEFAULT_USER_AGENT = os.environ.get("BROWSER_USER_AGENT", "Mozilla/5.0")

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
    if not is_valid_url(value):
        return False
    try:
        return bool(re.search(r"\.m3u8(?:$|[/?])", urlparse(value).path, re.IGNORECASE)) or bool(
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


def safe_origin(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        pass
    return ""


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


def set_job(job_id: str, **updates) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(updates)


def get_queue_position(job_id: str) -> int | None:
    with job_queue.mutex:
        pending_ids = list(job_queue.queue)
    try:
        return pending_ids.index(job_id) + 1
    except ValueError:
        return None


def stream_request_headers(job: dict) -> str:
    headers = []
    referer = job.get("referer", "")
    if referer:
        headers.append(f"Referer: {referer}")
        origin = safe_origin(referer)
        if origin:
            headers.append(f"Origin: {origin}")
    return "\r\n".join(headers) + ("\r\n" if headers else "")


def parse_ffmpeg_time(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


def probe_duration(job: dict) -> float | None:
    command = [
        FFPROBE_BIN,
        "-v",
        "error",
        "-user_agent",
        job.get("user_agent") or DEFAULT_USER_AGENT,
    ]
    headers = stream_request_headers(job)
    if headers:
        command += ["-headers", headers]
    command += [
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        job["source_url"],
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
        job.get("user_agent") or DEFAULT_USER_AGENT,
    ]
    headers = stream_request_headers(job)
    if headers:
        command += ["-headers", headers]
    command += [
        "-i",
        job["source_url"],
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


def create_job(source_url: str, title: str = "", referer: str = "", user_agent: str = "") -> dict:
    title = title.strip() or "video"
    referer = referer.strip()
    if "\r" in referer or "\n" in referer or (referer and not is_valid_url(referer)):
        referer = ""
    user_agent = re.sub(r"[\r\n]", "", user_agent).strip()[:500]
    job = {
        "id": uuid.uuid4().hex[:12],
        "source_url": source_url,
        "title": title,
        "filename": ensure_mp4_filename(title),
        "referer": referer,
        "user_agent": user_agent or DEFAULT_USER_AGENT,
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
        "direct_hls": True,
        "log": [],
    }
    with jobs_lock:
        jobs[job["id"]] = job
    job_queue.put(job["id"])
    return job


def public_job(job: dict) -> dict:
    result = {
        key: value
        for key, value in job.items()
        if key not in {"referer", "user_agent"}
    }
    result["log"] = list(job["log"])
    result["queue_position"] = (
        get_queue_position(job["id"]) if job["status"] == "queued" else None
    )
    return result


def download_worker(worker_number: int) -> None:
    while True:
        job_id = job_queue.get()
        temp_job_dir: Path | None = None
        try:
            with jobs_lock:
                job = jobs.get(job_id)
                if job is None:
                    continue
                job["status"] = "downloading"
                job["worker_number"] = worker_number
                job["started_at"] = utc_now()
                job["progress"] = 0.0
                job["message"] = "Probing stream"
                job_snapshot = dict(job)

            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            temp_job_dir = TEMP_DOWNLOAD_DIR / job_id
            temp_job_dir.mkdir(parents=True, exist_ok=True)
            temp_file = temp_job_dir / "download.mp4"

            duration = probe_duration(job_snapshot)
            set_job(job_id, duration_seconds=duration, message="Downloading")
            command = build_ffmpeg_command(job_snapshot, temp_file)
            append_log(job_id, "$ ffmpeg <direct HLS URL> -c copy ...")
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
                if key != "progress":
                    continue
                out_time = parse_ffmpeg_time(progress_state.get("out_time", ""))
                total_size = progress_state.get("total_size")
                speed = progress_state.get("speed")
                updates = {"downloaded_seconds": out_time, "speed": speed}
                if total_size and total_size.isdigit():
                    updates["downloaded_bytes"] = int(total_size)
                if out_time is not None and duration:
                    updates["progress"] = round(
                        min(100.0, max(0.0, out_time / duration * 100.0)), 2
                    )
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
            output_bytes = destination.stat().st_size if destination.exists() else None
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
                shutil.rmtree(temp_job_dir, ignore_errors=True)
            job_queue.task_done()


def cleanup_worker() -> None:
    while True:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=COMPLETED_JOB_TTL_HOURS)
        with jobs_lock:
            expired = [
                job_id
                for job_id, job in jobs.items()
                if job.get("status") in {"completed", "failed"}
                and (finished_at := parse_iso(job.get("finished_at")))
                and finished_at < cutoff
            ]
            for job_id in expired:
                jobs.pop(job_id, None)
        time.sleep(600)


for worker_number in range(1, MAX_CONCURRENT_DOWNLOADS + 1):
    threading.Thread(
        target=download_worker,
        args=(worker_number,),
        daemon=True,
        name=f"hls-worker-{worker_number}",
    ).start()
threading.Thread(target=cleanup_worker, daemon=True, name="job-cleanup").start()


def request_payload() -> tuple[str, str, str, str]:
    if request.is_json:
        data = request.get_json(silent=True) or {}
        return (
            str(data.get("url", "")).strip(),
            str(data.get("title", "")).strip(),
            str(data.get("referer", "")).strip(),
            str(data.get("userAgent", data.get("user_agent", ""))).strip(),
        )
    return (
        request.form.get("url", "").strip(),
        request.form.get("title", "").strip(),
        request.form.get("referer", "").strip(),
        request.form.get("user_agent", "").strip(),
    )


def queue_download_response():
    source_url, title, referer, user_agent = request_payload()
    if not is_direct_hls_url(source_url):
        message = "Only direct HTTP/HTTPS .m3u8 URLs are accepted"
        if request.is_json:
            return jsonify({"error": message}), 400
        return redirect(url_for("index", error=message))
    job = create_job(source_url, title, referer, user_agent)
    if request.is_json:
        return jsonify({"ok": True, "job": public_job(job)}), 202
    return redirect(url_for("index", queued=job["id"]))


@app.after_request
def allow_browser_capture(response):
    if request.path in {"/download", "/api/download"}:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


@app.get("/")
def index():
    return render_template(
        "index.html",
        app_name=APP_NAME,
        download_dir=str(DOWNLOAD_DIR),
        temp_download_dir=str(TEMP_DOWNLOAD_DIR),
        max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
        ttl_hours=COMPLETED_JOB_TTL_HOURS,
    )


@app.get("/capture")
def capture():
    return render_template("capture.html", app_name=APP_NAME)


@app.route("/download", methods=["POST", "OPTIONS"])
@app.route("/api/download", methods=["POST", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return make_response("", 204)
    return queue_download_response()


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
    ffmpeg_exists = Path(FFMPEG_BIN).exists()
    ffprobe_exists = Path(FFPROBE_BIN).exists()
    return jsonify(
        {
            "ok": ffmpeg_exists and ffprobe_exists,
            "ffmpeg": FFMPEG_BIN,
            "ffmpeg_exists": ffmpeg_exists,
            "ffprobe": FFPROBE_BIN,
            "ffprobe_exists": ffprobe_exists,
            "download_dir": str(DOWNLOAD_DIR),
            "temp_download_dir": str(TEMP_DOWNLOAD_DIR),
            "input": "direct m3u8 only",
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=99, threaded=True)
