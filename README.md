# LAN HLS Video Downloader

A generic Raspberry Pi web service for downloading HLS video streams you are authorized to save.

Paste either:

- a normal HTTP/HTTPS video webpage URL, or
- a direct `.m3u8` HLS URL.

For webpage URLs, the Pi opens the page in one shared headless Chromium process, creates an isolated browser context for the job, blocks common ad/tracking domains, attempts to start playback, observes the actual network traffic, and captures the active non-ad HLS manifest. If ad blocking prevents the player from initializing, it can automatically retry extraction once without blocking ads.

The resolved stream is then downloaded with FFmpeg using stream copy (`-c copy`) to local temporary storage. Only after FFmpeg completes successfully is the MP4 moved to the NAS destination.

## Default layout

- Web UI: `http://192.168.1.5:101`
- NAS destination: `/mnt/Videos`
- Temporary job storage: `/var/tmp/lan-hls-video-downloader/jobs`
- Concurrent jobs: `2`
- Completed/failed job retention in UI: `24 hours`
- Browser ad blocking: enabled
- Ad-block extraction fallback: enabled

## Architecture

```text
Browser / phone / PC
        |
        | paste normal page URL
        v
Raspberry Pi :101
        |
        v
      Queue
        |
        +--> Worker 1 --+
        |               |
        +--> Worker 2 --+--> shared headless Chromium
        |                    (separate context/page per extraction)
        |                          |
        |                          +--> detect active .m3u8
        |                                  |
        +----------------------------------+
                                           v
                                      FFmpeg -c copy
                                           |
                              local temporary MP4
                                           |
                                     successful only
                                           v
                                      /mnt/Videos
```

The page URL is queued, not the temporary signed HLS URL. This means a job can wait in the queue without its stream token expiring before the worker starts.

## Install on Raspberry Pi

Copy/extract this folder to the Pi, then:

```bash
cd lan-hls-video-downloader
sudo ./install.sh
```

The installer installs:

- Python venv support
- FFmpeg / FFprobe
- Python dependencies
- Playwright Chromium plus its Linux dependencies
- systemd service

It installs the application to:

```text
/opt/lan-hls-video-downloader
```

and creates temporary directories under:

```text
/var/tmp/lan-hls-video-downloader
```

### NAS permission check

The service runs as user `pi` by default. Verify that user can write to the NAS:

```bash
sudo -u pi touch /mnt/Videos/.lan-hls-test
sudo rm /mnt/Videos/.lan-hls-test
```

If your Pi account has a different username, install with:

```bash
sudo SERVICE_USER=myusername ./install.sh
```

## Service commands

```bash
sudo systemctl status lan-hls-video-downloader
sudo systemctl restart lan-hls-video-downloader
sudo systemctl stop lan-hls-video-downloader
journalctl -u lan-hls-video-downloader -f
```

Health endpoint:

```text
http://192.168.1.5:101/api/health
```

## Web interface

Open:

```text
http://192.168.1.5:101
```

Paste a page such as:

```text
https://example.com/watch/123
```

or a direct HLS manifest:

```text
https://cdn.example.com/path/master.m3u8?token=...
```

The optional title field overrides the filename. If left empty, the downloader uses the page's Open Graph title when available, otherwise the document title.

Job states are:

```text
queued
  -> extracting
  -> downloading
  -> moving
  -> completed

or failed
```

The UI refreshes automatically and shows queue position, worker, FFmpeg progress, media time, output size, speed, messages, and logs.

## Concurrency

Default:

```ini
Environment=MAX_CONCURRENT_DOWNLOADS=2
```

Additional jobs remain queued. To change it:

```bash
sudo nano /etc/systemd/system/lan-hls-video-downloader.service
sudo systemctl daemon-reload
sudo systemctl restart lan-hls-video-downloader
```

Two workers may extract concurrently, but they share a single Chromium browser process. Each extraction uses its own isolated browser context/page. Once a worker has resolved its HLS manifest, that browser context is closed and FFmpeg handles the actual download.

## Ad blocking

The downloader uses Chromium request interception rather than a browser extension. This avoids maintaining an extension and works in headless mode.

Default:

```ini
Environment=ADBLOCK_ENABLED=1
Environment=ADBLOCK_FALLBACK=1
```

Common advertising and tracking domains are blocked during extraction. Images and fonts are also skipped because they are not needed to discover the HLS stream.

If no HLS stream is found with ad blocking, `ADBLOCK_FALLBACK=1` causes one clean retry without the block list. Set it to `0` if you never want that fallback.

Add custom blocked domains with a comma-separated environment variable:

```ini
Environment=ADBLOCK_DOMAINS=ads.example.com,tracker.example.net
```

## Chromium

The installer downloads Playwright's Chromium build into:

```text
/var/tmp/lan-hls-video-downloader/browsers
```

and the systemd unit sets `PLAYWRIGHT_BROWSERS_PATH` to that directory. This avoids depending on whatever Chromium version happens to be installed by the Raspberry Pi OS package manager.

A custom browser can still be forced if needed:

```ini
Environment=CHROMIUM_BIN=/usr/bin/chromium
```

The application explicitly requests Chromium sandboxing. If your Raspberry Pi image prevents Chromium from launching under systemd because of sandbox configuration, first check the logs. As a last resort you can set:

```ini
Environment=CHROMIUM_NO_SANDBOX=1
```

then reload/restart the service. Disabling the browser sandbox reduces isolation and is not the preferred configuration.

## Highest quality / no re-encoding

FFmpeg is invoked with:

```text
-c copy
```

so the video/audio are remuxed rather than transcoded. There is no generation loss. When a master HLS manifest exposes multiple video/audio streams, FFmpeg's normal automatic stream selection is left enabled, which favors the highest-resolution video and the audio stream with the most channels.

The completed file is written first to local Pi storage and then moved to `/mnt/Videos`. If the NAS is a different filesystem, the final `move` is implemented as a copy-to-NAS followed by removal of the local temporary file.

## API

Queue a job:

```http
POST /api/download
Content-Type: application/json

{
  "url": "https://example.com/watch/123",
  "title": "Optional title"
}
```

List jobs:

```text
GET /api/jobs
```

Single job:

```text
GET /api/jobs/<job-id>
```

Health:

```text
GET /api/health
```

Short-lived stream URLs, cookies, and authorization headers captured by Chromium are intentionally not exposed by the public API/UI.

## Notes

- Job history is in memory and is cleared by a service restart.
- Completed/failed jobs are automatically removed from memory after 24 hours by default.
- This service intentionally accepts arbitrary HTTP/HTTPS URLs, so expose it only to a trusted LAN.
- Not every website uses HLS. DASH-only (`.mpd`) or DRM-protected playback is outside this downloader's scope.
- Use only with media you are authorized to download.
