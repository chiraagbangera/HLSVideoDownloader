(async () => {
    // 127.0.0.1 would point to this desktop, not the Raspberry Pi.
    const downloaderEndpoint = "http://192.168.1.5:99/download";

    const resources = performance
        .getEntriesByType("resource")
        .map((entry) => entry.name);

    const streams = resources.filter((url) =>
        /\.m3u8(?:\?|$)/i.test(url) &&
        url.includes("/etv/content/")
    );

    if (!streams.length) {
        console.error("No movie M3U8 found. Refresh the page and wait for the player to finish loading.");
        return;
    }

    // Usually the latest matching request is the active CDN URL.
    const hlsUrl = streams[streams.length - 1];
    const player = document.querySelector("#UIVideoPlayer");
    const title = player?.dataset.contentTitle || document.title || "video";

    console.log("Title:", title);
    console.log("HLS:", hlsUrl);

    const response = await fetch(downloaderEndpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            url: hlsUrl,
            title,
            referer: location.origin + "/",
            userAgent: navigator.userAgent
        })
    });

    const result = await response.json();
    if (!response.ok) {
        console.error("Downloader rejected the request:", result);
        return;
    }
    console.log("Queued download:", result);
})();
