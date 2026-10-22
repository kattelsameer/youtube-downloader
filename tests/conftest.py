import pytest
import yt_dlp

from youtube_downloader import backend


def youtube_block_reason(video_id: str = "10azSRvOv_E") -> str | None:
    """If YouTube is rate-limiting or bot-checking this network, say so."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True,
                               "logger": backend.YtdlLogger(),
                               "js_runtimes": backend.js_runtimes_option("auto")}) as ydl:
            ydl.extract_info(backend.watch_url(video_id), download=False, process=False)
    except yt_dlp.utils.DownloadError as exc:
        message = backend.clean_error(exc)
        if backend.blocked_hint(message):
            return message
    return None


@pytest.fixture
def skip_if_youtube_blocks():
    """Call with no args after a live check comes back empty: skips the test
    when the cause is YouTube blocking this network, not our code."""
    def check():
        reason = youtube_block_reason()
        if reason:
            pytest.skip(f"YouTube is blocking this network: {reason[:120]}")
    return check
