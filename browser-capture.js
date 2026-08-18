(async () => {
    // 127.0.0.1 would point to this desktop, not the Raspberry Pi.
    const capturePage = "http://192.168.1.5:99/capture";

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

    // A secure webpage cannot reliably fetch an HTTP service on the LAN, even
    // when local-network permission is granted. Open the Pi as a top-level page
    // instead. The fragment remains in the browser and is not sent to the Pi;
    // /capture reads it and performs a same-origin POST to /download.
    const target = new URL(capturePage);
    target.hash = new URLSearchParams({
        url: hlsUrl,
        title,
        referer: location.origin + "/",
        userAgent: navigator.userAgent
    }).toString();

    const captureWindow = window.open(target.toString(), "_blank");
    if (!captureWindow) {
        console.error("The capture page was blocked. Allow pop-ups for this page and run the script again.");
        return;
    }
    captureWindow.opener = null;
    console.log("Opened the downloader capture page.");
})();
