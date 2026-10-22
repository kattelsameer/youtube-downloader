"""Fake yt-dlp listing and download for tests.

Lives in its own module so worker processes can import it: the process pool
pickles `fake_download` by name. Behavior is driven by a JSON state file
named in $YTDL_FAKE_STATE, which tests rewrite between runs:

    {"playlists": {url: {"id", "title", "entries": [{"id", "title", ...}]}},
     "fail": [video ids], "crash": [video ids], "sleep": seconds,
     "calls": "path/to/calls.jsonl"}
"""

import json
import os
import time
from pathlib import Path

from youtube_downloader import backend
from youtube_downloader.sync import Entry, Listing

STATE_ENV = "YTDL_FAKE_STATE"
_real_fetch_listing = backend.fetch_listing  # tests monkeypatch backend.fetch_listing


def load_state() -> dict:
    path = os.environ.get(STATE_ENV)
    return json.loads(Path(path).read_text()) if path else {}


def fake_listing(job) -> Listing:
    if job.mode == "single":
        return _real_fetch_listing(job)  # no network in single mode
    playlist = load_state().get("playlists", {}).get(job.url)
    if playlist is None:
        raise backend.ListingError("[youtube:tab] fake: The playlist does not exist.")
    return Listing(entries=[Entry(**e) for e in playlist["entries"]],
                   playlist_id=playlist.get("id"), playlist_title=playlist.get("title"),
                   is_mix=bool(playlist.get("mix")))


def fake_download(task: dict) -> dict:
    state = load_state()
    started = time.time()
    video_id = task["video_id"]
    if video_id in state.get("crash", []):
        os._exit(1)  # simulate a worker killed by the OS
    time.sleep(state.get("sleep", 0))
    if state.get("calls"):
        with open(state["calls"], "a") as fh:
            fh.write(json.dumps({"id": video_id, "pid": os.getpid(), "start": started,
                                 "end": time.time(), "force": task["force"]}) + "\n")
    if video_id in state.get("fail", []):
        return {"video_id": video_id, "ok": False, "error": f"fake failure for {video_id}",
                "elapsed": time.time() - started}
    title = task.get("title") or f"Video {video_id}"
    path = Path(task["target_dir"]) / f"{title} [{video_id}].{task['settings']['format']}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"fake {task['settings']['format']} {video_id}".encode())
    return {"video_id": video_id, "ok": True, "title": title, "uploader": "Fake Artist",
            "duration": 60, "upload_date": state.get("dates", {}).get(video_id, "20240101"),
            "file_path": path.relative_to(task["output_dir"]).as_posix(),
            "file_size": path.stat().st_size, "elapsed": time.time() - started}


def calls(state_path) -> list[dict]:
    calls_path = json.loads(Path(state_path).read_text()).get("calls")
    if not calls_path or not Path(calls_path).exists():
        return []
    return [json.loads(line) for line in Path(calls_path).read_text().splitlines()]
