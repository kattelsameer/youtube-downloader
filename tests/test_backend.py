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
    assert opts["outtmpl"]["default"] == "%(title)s [%(id)s].%(ext)s"
    assert opts["noplaylist"] is True and opts["overwrites"] is None


def test_work_happens_in_a_staging_folder(tmp_path):
    staging = str(tmp_path / "t" / ".incomplete" / "10azSRvOv_E")
    opts = backend.build_download_options(make_task(tmp_path))
    assert opts["paths"] == {"home": str(tmp_path / "t"), "temp": staging}
    for task in (make_task(tmp_path, force=True), make_task(tmp_path, skip_existing=False)):
        # replacing: yt-dlp finishes in staging, the worker swaps the file in afterwards
        assert backend.build_download_options(task)["paths"] == {"home": staging, "temp": staging}


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


COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tPREF\tf6=40000000\n"


def test_network_options(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(COOKIES)
    opts = backend.build_download_options(make_task(
        tmp_path, cookies_file=str(cookies), proxy="socks5://127.0.0.1:1080", rate_limit=2048,
        retries=7, socket_timeout=12, concurrent_fragment_downloads=8))
    assert opts["cookiefile"].getvalue() == COOKIES and opts["proxy"] == "socks5://127.0.0.1:1080"
    assert opts["ratelimit"] == 2048 and opts["socket_timeout"] == 12
    assert opts["retries"] == opts["fragment_retries"] == opts["extractor_retries"] == 7
    assert opts["concurrent_fragment_downloads"] == 8
    opts = backend.build_download_options(make_task(tmp_path))
    assert "cookiefile" not in opts and "proxy" not in opts and "ratelimit" not in opts


def test_cookies_file_is_used_but_never_written(tmp_path):
    """A real YoutubeDL saves its cookie jar on close; the user's file must not change."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(COOKIES)
    before = (cookies.read_bytes(), cookies.stat().st_mtime_ns)
    opts = backend.base_ydl_options(make_settings(cookies_file=str(cookies)))
    with yt_dlp.YoutubeDL(opts) as ydl:
        assert [c.name for c in ydl.cookiejar] == ["PREF"]  # loaded
        ydl.cookiejar.set_cookie(next(iter(ydl.cookiejar)))  # touched, as during a download
    assert (cookies.read_bytes(), cookies.stat().st_mtime_ns) == before


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

def fake_download(ydl, url, content=b"\0" * 1234):
    """Behaves like yt-dlp with `paths`: work in temp, then move to home."""
    name = (ydl.opts["outtmpl"]["default"].replace("%(title)s", "Chaahat").replace("%(uploader)s", "Artist")
            .replace("%(id)s", "10azSRvOv_E").replace("%(ext)s", ydl.opts["final_ext"]))
    temp = Path(ydl.opts["paths"]["temp"]) / name
    home = Path(ydl.opts["paths"]["home"]) / name
    if ydl.opts["overwrites"] is None and home.exists():
        pass  # "has already been downloaded"
    else:
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(content)
        home.parent.mkdir(parents=True, exist_ok=True)
        temp.replace(home)
    for hook in ydl.opts["progress_hooks"]:
        hook({"status": "downloading", "info_dict": {"title": "Chaahat"}})
    for hook in ydl.opts["post_hooks"]:
        hook(str(home))
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


def test_worker_cleans_staging_after_success(tmp_path, fake_ydl):
    fake_ydl.responses[VIDEO] = fake_download
    backend.download_worker(make_task(tmp_path))
    assert not (tmp_path / "t" / ".incomplete").exists()
    assert [p.name for p in (tmp_path / "t").iterdir()] == ["Chaahat [10azSRvOv_E].mp3"]


def test_truncated_output_from_an_interrupted_run_is_not_reused(tmp_path, fake_ydl):
    staging = tmp_path / "t" / ".incomplete" / "10azSRvOv_E"
    staging.mkdir(parents=True)
    (staging / "Chaahat [10azSRvOv_E].mp3").write_bytes(b"trunc")    # killed ffmpeg output
    (staging / "Chaahat [10azSRvOv_E].webm.part").write_bytes(b"p")   # resumable download
    seen = {}

    def check_then_download(ydl, url):
        seen["files"] = sorted(p.name for p in staging.iterdir())
        return fake_download(ydl, url)

    fake_ydl.responses[VIDEO] = check_then_download
    result = backend.download_worker(make_task(tmp_path))
    assert seen["files"] == ["Chaahat [10azSRvOv_E].webm.part"]
    assert result["ok"] and (tmp_path / "t" / "Chaahat [10azSRvOv_E].mp3").read_bytes() == b"\0" * 1234


def test_failure_keeps_part_files_for_resume(tmp_path, fake_ydl):
    staging = tmp_path / "t" / ".incomplete" / "10azSRvOv_E"

    def partial_then_fail(ydl, url):
        staging.mkdir(parents=True)
        (staging / "Chaahat [10azSRvOv_E].webm.part").write_bytes(b"half")
        raise yt_dlp.utils.DownloadError("ERROR: connection reset")

    fake_ydl.responses[VIDEO] = partial_then_fail
    assert backend.download_worker(make_task(tmp_path))["ok"] is False
    assert (staging / "Chaahat [10azSRvOv_E].webm.part").read_bytes() == b"half"


def test_failed_force_redownload_keeps_the_old_file(tmp_path, fake_ydl):
    old = tmp_path / "t" / "Chaahat [10azSRvOv_E].mp3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old good file")
    fake_ydl.responses[VIDEO] = yt_dlp.utils.DownloadError("ERROR: HTTP Error 403")
    result = backend.download_worker(make_task(tmp_path, force=True))
    assert result["ok"] is False and old.read_bytes() == b"old good file"
    assert not (tmp_path / "t" / ".incomplete").exists()


def test_successful_force_redownload_replaces_the_old_file(tmp_path, fake_ydl):
    old = tmp_path / "t" / "Chaahat [10azSRvOv_E].mp3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old good file")
    fake_ydl.responses[VIDEO] = lambda ydl, url: fake_download(ydl, url, content=b"new file")
    result = backend.download_worker(make_task(tmp_path, force=True))
    assert result["ok"] and result["file_path"] == "t/Chaahat [10azSRvOv_E].mp3"
    assert old.read_bytes() == b"new file"
    assert sorted(p.name for p in (tmp_path / "t").iterdir()) == ["Chaahat [10azSRvOv_E].mp3"]


def test_template_subfolders_survive_staging_and_force(tmp_path, fake_ydl):
    template = "%(uploader)s/%(title)s [%(id)s].%(ext)s"
    staging = tmp_path / "t" / ".incomplete" / "10azSRvOv_E"
    (staging / "Artist").mkdir(parents=True)
    (staging / "Artist" / "Chaahat [10azSRvOv_E].mp3").write_bytes(b"trunc")  # stale, nested
    seen = {}

    def check_then_download(ydl, url):
        seen["stale"] = (staging / "Artist" / "Chaahat [10azSRvOv_E].mp3").exists()
        return fake_download(ydl, url)

    fake_ydl.responses[VIDEO] = check_then_download
    result = backend.download_worker(make_task(tmp_path, filename_template=template))
    assert seen["stale"] is False and result["file_path"] == "t/Artist/Chaahat [10azSRvOv_E].mp3"

    fake_ydl.responses[VIDEO] = lambda ydl, url: fake_download(ydl, url, content=b"forced")
    result = backend.download_worker(make_task(tmp_path, force=True, filename_template=template))
    assert result["file_path"] == "t/Artist/Chaahat [10azSRvOv_E].mp3"
    assert (tmp_path / "t" / "Artist" / "Chaahat [10azSRvOv_E].mp3").read_bytes() == b"forced"
    assert sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "t").rglob("*")) == [
        "t/Artist", "t/Artist/Chaahat [10azSRvOv_E].mp3"]


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
