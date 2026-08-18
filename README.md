# HLS Video Downloader

A Raspberry Pi web service for downloading direct HLS (`.m3u8`) streams you
are authorized to save. It does not open webpages, parse HTML, run Chromium, or
attempt to discover media URLs.

Submit a direct HLS URL through the web interface or send it from
[`browser-capture.js`](browser-capture.js). The supplied title becomes the
default MP4 filename. FFmpeg downloads the stream with stream copy (`-c copy`)
to local temporary storage, then moves the completed MP4 to the NAS.

## Educational use and legal disclaimer

This project is provided strictly for educational, research, and personal
experimentation purposes. It is not intended for piracy, copyright
infringement, DRM circumvention, or downloading any media without the rights
holder's permission. Only use it with content you own or are explicitly
authorized to download.

You are solely responsible for how you use this software and for complying with
all applicable laws, licenses, website terms, and intellectual-property rights.
The author does not endorse unauthorized copying and accepts no responsibility
or liability for misuse of this project or any resulting claims, damages,
penalties, or other consequences.

## Default layout

- Web UI and API: `http://192.168.1.5:99`
- NAS destination: `/mnt/Videos`
- Temporary job storage: `/var/tmp/hls-video-downloader/jobs`
- Concurrent downloads: `2`
- Completed/failed job retention: `24 hours`
- Accepted input: direct HTTP/HTTPS `.m3u8` URLs only

## Install on Raspberry Pi

```bash
cd hls-video-downloader
sudo ./install.sh
```

The installer installs Python venv support, FFmpeg/FFprobe, the Python
dependencies, and the systemd service. Chromium and Playwright are not used.

The service runs as user `pi` by default. Verify NAS access with:

```bash
sudo -u pi touch /mnt/Videos/.hls-video-test
sudo rm /mnt/Videos/.hls-video-test
```

For another service account:

```bash
sudo SERVICE_USER=myusername ./install.sh
```

## Deploy updates from VS Code

Run:

```text
Terminal -> Run Build Task -> Deploy to Raspberry Pi
```

The task prompts for the SSH destination, copies updated files, stops the
existing service, applies dependency and systemd changes, restarts the service,
and performs a health check. If the service is not installed, the same task
runs the initial installer.

From a terminal:

```bash
PI_HOST=pi@192.168.1.5 ./deploy.sh
```

## Submit from the browser

Open or refresh the video page and wait for it to finish selecting its media
server. Paste the contents of [`browser-capture.js`](browser-capture.js) into
the browser developer console. The script:

1. Reads completed resource requests from the browser performance timeline.
2. Selects the latest matching `/etv/content/` `.m3u8` URL.
3. Reads `data-content-title` from `#UIVideoPlayer` for the filename.
4. Opens `http://192.168.1.5:99/capture` in a new tab with the URL, title,
   referer, and current browser user-agent stored in the URL fragment.
5. The Pi capture page removes that fragment, submits the payload to
   `/download` on the same origin, and opens the queued job list.

This top-level navigation bridge avoids Chrome blocking a direct `fetch()` from
an HTTPS video page to the Pi's HTTP service. URL fragments are not transmitted
in HTTP requests. If Chrome blocks the new tab, allow pop-ups for the video
page and run the script again. Change `capturePage` at the top of the script if
the Pi address changes. Do not use `127.0.0.1` unless the downloader is running
on the same computer as the browser.

The capture tab redirects to `http://192.168.1.5:99` after queueing so you can
monitor progress.

## Web interface

The form accepts a direct URL such as:

```text
https://cdn.example.com/path/master.m3u8?token=...
```

Normal webpage URLs are rejected. The optional title becomes the filename;
when omitted, the filename is `video.mp4`. Duplicate filenames receive a
numeric suffix rather than overwriting an existing video.

Job states are:

```text
queued -> downloading -> moving -> completed
                                \-> failed
```

## API

Both `/download` and `/api/download` accept the browser script's JSON payload:

```http
POST /download
Content-Type: application/json

{
  "url": "https://cdn.example.com/path/master.m3u8?token=...",
  "title": "Video title",
  "referer": "https://example.com/",
  "userAgent": "Mozilla/5.0 ..."
}
```

A successful request returns HTTP `202` with the queued job. A URL that is not
a direct `.m3u8` returns HTTP `400`.

Other endpoints:

```text
GET /api/jobs
GET /api/jobs/<job-id>
GET /api/health
```

## Service commands

```bash
sudo systemctl status hls-video-downloader
sudo systemctl restart hls-video-downloader
sudo systemctl stop hls-video-downloader
journalctl -u hls-video-downloader -f
```

## Download behavior

FFmpeg receives the captured browser user-agent, `Referer`, and `Origin` headers
because signed HLS links may require the same request context as the browser.
It is invoked with `-c copy`, so audio and video are remuxed rather than
transcoded. The completed file is moved to `/mnt/Videos` only after FFmpeg exits
successfully.

Job history is held in memory and is cleared by a service restart. Finished and
failed records are removed after 24 hours by default. Signed URLs may expire,
so submit them promptly after capture.

## License

This project is available under the [MIT License](LICENSE). You may use, copy,
modify, and distribute it, provided that the copyright and license notice are
retained in copies or substantial portions of the software. The software is
supplied **as is**, without warranty; see the license for the complete terms.
