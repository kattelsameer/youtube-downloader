from pathlib import Path

import pytest
import yt_dlp

from youtube_downloader import backend
from youtube_downloader.config import BUILTIN_DEFAULTS, Job

VIDEO = "https://www.youtube.com/watch?v=10azSRvOv_E"
PLAYLIST = "https://www.youtube.com/playlist?list=PLfP6i5T0-DkK52drjlLM2wEGXSL-rqF4T"
MIX = "https://www.youtube.com/watch?v=10azSRvOv_E&list=RD10azSRvOv_E&start_radio=1"


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL. `responses` maps URL -> info dict,
    exception, or callable(ydl, url) returning an info dict."""
    responses: dict = {}
    instances: list = []

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        self.last_call = {"url": url, "download": download, "process": process}
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response(self, url) if callable(response) else response


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.responses, FakeYDL.instances = {}, []
    monkeypatch.setattr(backend.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def make_settings(**overrides):
    return {**BUILTIN_DEFAULTS, "js_runtime": "none", **overrides}


def make_job(tmp_path, mode="playlist", url=PLAYLIST, **overrides):
    settings = make_settings(output_dir=str(tmp_path), **overrides)
    return Job(tag="t", name="t", mode=mode, url=url, enabled=True, count=None, settings=settings)


def make_task(tmp_path, video_id="10azSRvOv_E", force=False, **overrides):
    return {"video_id": video_id, "tag": "t", "title": None, "force": force,
            "target_dir": str(tmp_path / "t"), "output_dir": str(tmp_path),
            "settings": make_settings(output_dir=str(tmp_path), **overrides)}


# --- yt-dlp options --------------------------------------------------------

def test_mp3_options(tmp_path):
    opts = backend.build_download_options(make_task(tmp_path, format="mp3", audio_quality=320))
    assert opts["format"] == "bestaudio/best" and opts["final_ext"] == "mp3"
    assert opts["postprocessors"][0] == {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                         "preferredquality": "320"}
    assert opts["outtmpl"]["default"] == str(tmp_path / "t" / "%(title)s [%(id)s].%(ext)s")
    assert opts["noplaylist"] is True and opts["overwrites"] is None


@pytest.mark.parametrize("height, first_sort", [(360, "res:360"), (1080, "res:1080"), ("best", "res")])
def test_mp4_options(tmp_path, height, first_sort):
    opts = backend.build_download_options(make_task(tmp_path, format="mp4", max_resolution=height))
    assert opts["format_sort"] == [first_sort, "ext:mp4:m4a", "vcodec:h264"]
    assert opts["merge_output_format"] == "mp4" and opts["final_ext"] == "mp4"
    assert {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"} in opts["postprocessors"]


def test_overwrite_rules(tmp_path):
    assert backend.build_download_options(make_task(tmp_path, force=True))["overwrites"] is True
    assert backend.build_download_options(make_task(tmp_path, skip_existing=False))["overwrites"] is True
    assert backend.build_download_options(make_task(tmp_path, skip_existing=True))["overwrites"] is None


def test_embed_options(tmp_path):
    opts = backend.build_download_options(make_task(tmp_path, embed_metadata=True, embed_thumbnail=True))
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert keys == ["FFmpegThumbnailsConvertor", "FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"]
    assert opts["writethumbnail"] is True
    opts = backend.build_download_options(make_task(tmp_path, embed_metadata=False, embed_thumbnail=False))
    assert [pp["key"] for pp in opts["postprocessors"]] == ["FFmpegExtractAudio"]


def test_network_options(tmp_path):
    opts = backend.build_download_options(make_task(
        tmp_path, cookies_file="/c.txt", proxy="socks5://127.0.0.1:1080", rate_limit=2048,
        retries=7, socket_timeout=12, concurrent_fragment_downloads=8))
    assert opts["cookiefile"] == "/c.txt" and opts["proxy"] == "socks5://127.0.0.1:1080"
    assert opts["ratelimit"] == 2048 and opts["socket_timeout"] == 12
    assert opts["retries"] == opts["fragment_retries"] == opts["extractor_retries"] == 7
    assert opts["concurrent_fragment_downloads"] == 8
    opts = backend.build_download_options(make_task(tmp_path))
    assert "cookiefile" not in opts and "proxy" not in opts and "ratelimit" not in opts


def test_js_runtime_choice(monkeypatch):
    monkeypatch.setattr(backend.shutil, "which", lambda name: "/bin/x" if name in ("node", "bun") else None)
    assert backend.js_runtimes_option("auto") == {"node": {}}
    assert backend.js_runtimes_option("deno") == {"deno": {}}
    assert backend.js_runtimes_option("none") == {}
    monkeypatch.setattr(backend.shutil, "which", lambda name: None)
    assert backend.js_runtimes_option("auto") == {}


def test_retry_chatter_is_debug_only(caplog):
    logger = backend.YtdlLogger("[t] ")
    with caplog.at_level("DEBUG", logger="ytdl"):
        logger.warning("[youtube:tab] HTTP Error 404: Not Found. Retrying (1/3)...")
        logger.warning("[youtube:tab] PLx: Unable to download webpage. Giving up after 3 retries")
        logger.warning("\x1b[0;33mWARNING:\x1b[0m [youtube] YouTube said: INFO - 1 unavailable video is hidden")
    levels = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert [lvl for lvl, _ in levels] == ["DEBUG", "DEBUG", "WARNING"]
    assert levels[2][1] == "[t] [youtube] YouTube said: INFO - 1 unavailable video is hidden"


def test_clean_error():
    raw = "\x1b[0;31mERROR:\x1b[0m [youtube] abc: Video unavailable.\n  This video is private"
    assert backend.clean_error(raw) == "[youtube] abc: Video unavailable. This video is private"
    assert backend.clean_error(ValueError("")) == "ValueError"


# --- download worker -------------------------------------------------------

def fake_download(ydl, url):
    target = Path(ydl.opts["outtmpl"]["default"].replace("%(title)s", "Chaahat")
                  .replace("%(id)s", "10azSRvOv_E").replace("%(ext)s", ydl.opts["final_ext"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\0" * 1234)
    for hook in ydl.opts["progress_hooks"]:
        hook({"status": "downloading", "info_dict": {"title": "Chaahat"}})
    for hook in ydl.opts["post_hooks"]:
        hook(str(target))
    return {"id": "10azSRvOv_E", "title": "Chaahat", "uploader": "Artist", "duration": 300,
            "upload_date": "20240501"}


def test_worker_success(tmp_path, fake_ydl):
    fake_ydl.responses[VIDEO] = fake_download
    result = backend.download_worker(make_task(tmp_path))
    assert result["ok"] is True and result["error"] is None
    assert result["file_path"] == "t/Chaahat [10azSRvOv_E].mp3" and result["file_size"] == 1234
    assert (result["title"], result["uploader"], result["duration"], result["upload_date"]) == \
        ("Chaahat", "Artist", 300, "20240501")
    assert fake_ydl.instances[0].last_call["download"] is True
    assert result["elapsed"] >= 0


def test_worker_reports_download_error(tmp_path, fake_ydl):
    fake_ydl.responses[VIDEO] = yt_dlp.utils.DownloadError("ERROR: [youtube] 10azSRvOv_E: Video unavailable")
    result = backend.download_worker(make_task(tmp_path))
    assert result["ok"] is False and result["error"] == "[youtube] 10azSRvOv_E: Video unavailable"


def test_worker_reports_missing_output(tmp_path, fake_ydl):
    fake_ydl.responses[VIDEO] = {"id": "10azSRvOv_E", "requested_downloads": [{"filepath": "/nope.mp3"}]}
    result = backend.download_worker(make_task(tmp_path))
    assert result["ok"] is False and "output file was not found" in result["error"]


# --- listing ---------------------------------------------------------------

def test_single_listing_needs_no_network(tmp_path, fake_ydl):
    listing = backend.fetch_listing(make_job(tmp_path, mode="single", url=VIDEO))
    assert [e.id for e in listing.entries] == ["10azSRvOv_E"] and fake_ydl.instances == []


def test_playlist_listing(tmp_path, fake_ydl):
    fake_ydl.responses[PLAYLIST] = {
        "id": "PLfP6i5T0-DkK52drjlLM2wEGXSL-rqF4T", "title": "Jazz", "_type": "playlist",
        "entries": [
            {"id": "aaaaaaaaaaa", "title": "A", "duration": 100, "ie_key": "Youtube"},
            None,
            {"id": "bbbbbbbbbbb", "title": "B", "timestamp": 1704067200, "ie_key": "Youtube"},
            {"id": "UCxxxxxxxxxxxxxxxxxxxxxx", "title": "Shorts", "ie_key": "YoutubeTab"},
        ],
    }
    listing = backend.fetch_listing(make_job(tmp_path))
    assert [e.id for e in listing.entries] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert listing.playlist_title == "Jazz" and listing.playlist_id.startswith("PL") and not listing.is_mix
    assert listing.entries[0].duration == 100 and listing.entries[1].upload_date == "20240101"
    opts = fake_ydl.instances[0].opts
    assert opts["extract_flat"] == "in_playlist" and "playlistend" not in opts


def test_mix_listing_is_capped(tmp_path, fake_ydl):
    fake_ydl.responses[MIX] = {"id": "RD10azSRvOv_E", "title": "Mix", "entries": []}
    listing = backend.fetch_listing(make_job(tmp_path, url=MIX, mix_limit=7))
    assert listing.is_mix and fake_ydl.instances[0].opts["playlistend"] == 7


def test_listing_errors(tmp_path, fake_ydl):
    fake_ydl.responses[PLAYLIST] = yt_dlp.utils.DownloadError("ERROR: [youtube:tab] PLx: The playlist does not exist.")
    with pytest.raises(backend.ListingError, match="The playlist does not exist"):
        backend.fetch_listing(make_job(tmp_path))
    channel = "https://www.youtube.com/@SomeChannel"
    fake_ydl.responses[channel] = {"id": "UCx", "entries": [{"id": "UCx-videos", "ie_key": "YoutubeTab"}]}
    with pytest.raises(backend.ListingError, match="channel's /videos URL"):
        backend.fetch_listing(make_job(tmp_path, url=channel))


def test_lookup_upload_dates(fake_ydl):
    from youtube_downloader.sync import Entry
    fake_ydl.responses.update({
        backend.watch_url("aaaaaaaaaaa"): {"upload_date": "20240101"},
        backend.watch_url("bbbbbbbbbbb"): {"timestamp": 1735689600},
        backend.watch_url("ccccccccccc"): yt_dlp.utils.DownloadError("private"),
    })
    dates = backend.lookup_upload_dates([Entry(v) for v in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")],
                                        make_settings())
    assert dates == {"aaaaaaaaaaa": "20240101", "bbbbbbbbbbb": "20250101"}
    assert all(i.last_call["process"] is False for i in fake_ydl.instances)


# --- live (network) --------------------------------------------------------

@pytest.mark.live
def test_live_playlist_listing(tmp_path):
    listing = backend.fetch_listing(make_job(tmp_path, js_runtime="auto"))
    assert len(listing.entries) >= 10 and listing.playlist_title
    assert all(len(e.id) == 11 and e.title for e in listing.entries)


@pytest.mark.live
def test_live_mix_listing_respects_mix_limit(tmp_path):
    listing = backend.fetch_listing(make_job(tmp_path, url=MIX, js_runtime="auto", mix_limit=5))
    assert listing.is_mix and len(listing.entries) == 5
    assert listing.entries[0].id == "10azSRvOv_E"


@pytest.mark.live
def test_live_upload_dates(tmp_path, skip_if_youtube_blocks):
    from youtube_downloader.sync import Entry
    dates = backend.lookup_upload_dates([Entry("10azSRvOv_E")], make_settings(js_runtime="auto"))
    if not dates:
        skip_if_youtube_blocks()
    assert len(dates.get("10azSRvOv_E", "")) == 8


def test_blocked_hint():
    assert backend.blocked_hint("[youtube] x: Sign in to confirm you’re not a bot. Use --cookies")
    assert backend.blocked_hint("Unable to download webpage: HTTP Error 429: Too Many Requests")
    assert backend.blocked_hint("[youtube] x: Video unavailable") is None
