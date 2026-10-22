"""Incremental sync end to end through the real process pool, with the yt-dlp
listing and download replaced by tests/fakes.py."""
import json
import time
from pathlib import Path

import pytest
import yaml

import fakes
from youtube_downloader import backend, runner
from youtube_downloader.config import load_config, select_jobs
from youtube_downloader.metadata import MetadataStore

PLAYLIST = "https://www.youtube.com/playlist?list=PLtestlist"
OTHER = "https://www.youtube.com/playlist?list=PLotherlist"
MISSING = "https://www.youtube.com/playlist?list=PLdoesnotexist"
VIDEO = "https://www.youtube.com/watch?v=10azSRvOv_E"


def vid(n: int) -> str:
    return f"vid{n:08d}"  # 11 characters, like a YouTube ID


class World:
    """A config file, a fake YouTube and helpers to run jobs and inspect results."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys):
        self.tmp = tmp_path
        self.capsys = capsys
        self.state_path = tmp_path / "state.json"
        self.state = {"playlists": {}, "fail": [], "crash": [], "sleep": 0,
                      "calls": str(tmp_path / "calls.jsonl")}
        self.save_state()
        monkeypatch.setenv(fakes.STATE_ENV, str(self.state_path))
        monkeypatch.setattr(backend, "fetch_listing", fakes.fake_listing)
        monkeypatch.setattr(backend, "download_worker", fakes.fake_download)
        self.defaults = {"output_dir": "out", "max_workers": 3, "js_runtime": "none"}
        self.jobs = [{"tag": "songs", "mode": "playlist", "url": PLAYLIST}]

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def set_playlist(self, ids, url=PLAYLIST, title="Test List", **extra):
        self.state["playlists"][url] = {
            "id": url.rsplit("=", 1)[1], "title": title,
            "entries": [{"id": i, "title": f"Song {i[-2:]}", **extra.get(i, {})} for i in ids],
        }
        self.save_state()

    def config(self):
        path = self.tmp / "config.yaml"
        path.write_text(yaml.safe_dump({"defaults": self.defaults, "jobs": self.jobs}, sort_keys=False))
        return load_config(path)

    def run(self, *tags, **options):
        cfg = self.config()
        store = MetadataStore(cfg.metadata_file, read_only=options.get("dry_run", False)).load()
        self.calls_before = len(self.calls())
        summaries = runner.run_jobs(select_jobs(cfg, list(tags) or None), store, runner.RunOptions(**options))
        self.output = self.capsys.readouterr().out
        return summaries

    def calls(self):
        return fakes.calls(self.state_path)

    def new_calls(self):
        return self.calls()[self.calls_before:]

    def metadata(self):
        return json.loads((self.tmp / "out" / "metadata.json").read_text())

    def record(self, video_id, tag="songs", fmt="mp3"):
        return self.metadata()["jobs"][tag]["videos"][video_id][fmt]


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


def test_first_run_downloads_all_second_run_is_up_to_date(world):
    world.set_playlist([vid(1), vid(2), vid(3)])
    world.jobs = [
        {"tag": "song-single", "mode": "single", "url": VIDEO},
        {"tag": "song-playlist", "mode": "playlist", "url": PLAYLIST, "count": 3},
        {"tag": "video-single", "mode": "single", "url": VIDEO, "format": "mp4", "max_resolution": 360},
    ]
    first = world.run()
    assert [s.downloaded for s in first] == [1, 3, 1]
    assert (world.tmp / "out/song-single/Video 10azSRvOv_E [10azSRvOv_E].mp3").is_file()
    assert (world.tmp / "out/song-playlist/Song 03 [vid00000003].mp3").is_file()
    assert (world.tmp / "out/video-single/Video 10azSRvOv_E [10azSRvOv_E].mp4").is_file()

    second = world.run()
    assert [s.downloaded for s in second] == [0, 0, 0]
    assert [s.already for s in second] == [1, 3, 1]
    assert world.output.count(runner.UP_TO_DATE) == 3
    assert world.new_calls() == []


def test_playlist_grows_from_three_to_five(world):
    world.set_playlist([vid(i) for i in (1, 2, 3)])
    world.run()
    world.set_playlist([vid(i) for i in (1, 2, 3, 4, 5)])
    [summary] = world.run()
    assert summary.downloaded == 2 and summary.already == 3
    assert sorted(c["id"] for c in world.new_calls()) == [vid(4), vid(5)]
    videos = world.metadata()["jobs"]["songs"]["videos"]
    assert sorted(videos) == [vid(i) for i in range(1, 6)]
    assert all(v["mp3"]["status"] == "downloaded" for v in videos.values())


def test_removed_video_is_kept_and_reported(world):
    world.set_playlist([vid(1), vid(2), vid(3)])
    world.run()
    before = world.record(vid(2))
    world.set_playlist([vid(1), vid(3)])
    [summary] = world.run()
    assert summary.gone == 1 and summary.downloaded == 0
    assert "No longer in playlist (1), kept as they are:" in world.output
    assert f"Song 02 [{vid(2)}]" in world.output
    assert world.record(vid(2)) == before
    assert (world.tmp / "out" / before["file_path"]).is_file()


def test_failed_video_recorded_skipped_then_retried(world):
    world.set_playlist([vid(1), vid(2), vid(3)])
    world.state["fail"] = [vid(2)]
    world.save_state()
    [summary] = world.run()
    assert summary.downloaded == 2 and summary.failed == 1
    rec = world.record(vid(2))
    assert rec["status"] == "failed" and rec["attempts"] == 1
    assert rec["last_error"] == f"fake failure for {vid(2)}"

    [summary] = world.run()  # still failing upstream, but we don't even try
    assert summary.previously_failed == 1 and summary.downloaded == 0 and world.new_calls() == []
    assert "use --retry-failed to retry" in world.output

    world.state["fail"] = []
    world.save_state()
    [summary] = world.run(retry_failed=True)
    assert summary.downloaded == 1 and [c["id"] for c in world.new_calls()] == [vid(2)]
    rec = world.record(vid(2))
    assert rec["status"] == "downloaded" and rec["attempts"] == 2 and rec["last_error"] is None


def test_retry_failed_job_setting(world):
    world.set_playlist([vid(1)])
    world.state["fail"] = [vid(1)]
    world.save_state()
    world.run()
    world.jobs[0]["retry_failed"] = True
    [summary] = world.run()
    assert summary.failed == 1 and world.record(vid(1))["attempts"] == 2


def test_format_change_downloads_again_and_keeps_mp3_records(world):
    world.set_playlist([vid(1), vid(2)])
    world.run()
    world.jobs[0]["format"] = "mp4"
    [summary] = world.run()
    assert summary.downloaded == 2
    for n in (1, 2):
        formats = world.metadata()["jobs"]["songs"]["videos"][vid(n)]
        assert set(formats) == {"mp3", "mp4"}
        for fmt in ("mp3", "mp4"):
            assert (world.tmp / "out" / formats[fmt]["file_path"]).is_file()
    assert world.metadata()["jobs"]["songs"]["format"] == "mp4"


def test_verify_files(world):
    world.set_playlist([vid(1), vid(2)])
    world.run()
    path = world.tmp / "out" / world.record(vid(1))["file_path"]
    path.unlink()
    [summary] = world.run()  # verify_files defaults to true
    assert summary.downloaded == 1 and "(file missing on disk)" in world.output
    assert path.is_file()

    path.unlink()
    world.defaults["verify_files"] = False
    [summary] = world.run()
    assert summary.downloaded == 0 and summary.already == 2 and not path.exists()


def test_force_redownloads(world):
    world.set_playlist([vid(1), vid(2)])
    world.run()
    [summary] = world.run(force=True)
    assert summary.downloaded == 2 and all(c["force"] for c in world.new_calls())
    assert world.record(vid(1))["attempts"] == 2


def test_jobs_share_one_metadata_file_without_interfering(world):
    world.set_playlist([vid(1), vid(2)])
    world.set_playlist([vid(2), vid(3)], url=OTHER, title="Other")
    world.jobs = [{"tag": "a", "mode": "playlist", "url": PLAYLIST},
                  {"tag": "b", "mode": "playlist", "url": OTHER, "format": "mp4"}]
    world.run()
    jobs = world.metadata()["jobs"]
    assert sorted(jobs["a"]["videos"]) == [vid(1), vid(2)] and sorted(jobs["b"]["videos"]) == [vid(2), vid(3)]
    assert jobs["a"]["videos"][vid(2)]["mp3"]["file_path"].startswith("a/")
    assert jobs["b"]["videos"][vid(2)]["mp4"]["file_path"].startswith("b/")

    world.set_playlist([vid(1), vid(2), vid(4)])
    b_before = world.metadata()["jobs"]["b"]
    world.run("a")
    jobs = world.metadata()["jobs"]
    assert jobs["b"] == b_before and sorted(jobs["a"]["videos"]) == [vid(1), vid(2), vid(4)]


def test_failed_listing_does_not_stop_other_jobs(world):
    world.set_playlist([vid(1)])
    world.jobs = [{"tag": "broken", "mode": "playlist", "url": MISSING},
                  {"tag": "songs", "mode": "playlist", "url": PLAYLIST}]
    broken, songs = world.run()
    assert broken.error and "The playlist does not exist" in broken.error
    assert songs.downloaded == 1
    assert "jobs that failed: broken" in world.output


def test_disabled_job_is_skipped(world):
    world.set_playlist([vid(1)])
    world.jobs.append({"tag": "off", "mode": "single", "url": VIDEO, "enabled": False})
    songs, off = world.run()
    assert off.disabled and songs.downloaded == 1
    assert "== off == skipped: disabled in config" in world.output
    assert "off" not in world.metadata()["jobs"]


def test_dry_run_changes_nothing(world):
    world.set_playlist([vid(1), vid(2)])
    [summary] = world.run(dry_run=True)
    assert summary.downloaded == 2 and "Would download (2):" in world.output
    assert not (world.tmp / "out").exists() and world.calls() == []

    world.run()
    world.set_playlist([vid(1), vid(2), vid(3)])
    meta = (world.tmp / "out" / "metadata.json").read_bytes()
    [summary] = world.run(dry_run=True)
    assert summary.downloaded == 1 and summary.already == 2
    assert f"+ Song 03 [{vid(3)}]  (new)" in world.output
    assert (world.tmp / "out" / "metadata.json").read_bytes() == meta
    assert world.new_calls() == []


def test_count_window_and_count_larger_than_playlist(world):
    world.set_playlist([vid(i) for i in range(1, 6)])
    world.jobs[0]["count"] = 2
    [summary] = world.run()
    assert summary.downloaded == 2 and sorted(world.metadata()["jobs"]["songs"]["videos"]) == [vid(1), vid(2)]
    world.jobs[0]["count"] = 50
    [summary] = world.run()
    assert summary.downloaded == 3
    assert "count 50 is more than the 5 video(s) available" in world.output
    world.jobs[0]["count"] = "all"
    [summary] = world.run()
    assert summary.downloaded == 0 and summary.already == 5


def test_order_by_title_and_by_date(world, monkeypatch):
    dates = {vid(1): "20250101", vid(2): "20260101", vid(3): "20240101"}
    world.state["dates"] = dates
    world.set_playlist([vid(1), vid(2), vid(3)])
    world.jobs[0].update(count=1, order="title_desc")
    world.run()
    assert list(world.metadata()["jobs"]["songs"]["videos"]) == [vid(3)]

    looked_up = []

    def fake_dates(entries, settings, max_threads=8):
        looked_up.append([e.id for e in entries])
        return {e.id: dates[e.id] for e in entries}

    monkeypatch.setattr(backend, "lookup_upload_dates", fake_dates)
    world.jobs[0].update(count=1, order="newest")
    world.run()
    assert vid(2) in world.metadata()["jobs"]["songs"]["videos"]
    assert looked_up == [[vid(1), vid(2)]]  # vid(3)'s date was cached from its download
    world.run()
    assert looked_up == [[vid(1), vid(2)]] and runner.UP_TO_DATE in world.output  # all cached now


def test_use_playlist_subfolder(world):
    world.set_playlist([vid(1)], title='Best: of "2024"')
    world.defaults["use_playlist_subfolder"] = True
    world.run()
    assert world.record(vid(1))["file_path"] == f'songs/Best_ of _2024_/Song 01 [{vid(1)}].mp3'
    entry = world.metadata()["jobs"]["songs"]
    assert entry["playlist_title"] == 'Best: of "2024"' and entry["last_run"]


def test_worker_crash_is_recorded_and_next_job_runs(world):
    world.set_playlist([vid(1), vid(2), vid(3)])
    world.set_playlist([vid(5)], url=OTHER)
    world.state["crash"] = [vid(2)]
    world.save_state()
    world.defaults["max_workers"] = 1
    world.jobs.append({"tag": "other", "mode": "playlist", "url": OTHER})
    songs, other = world.run()
    assert world.record(vid(1))["status"] == "downloaded"
    assert world.record(vid(2))["status"] == "failed"
    assert "worker process crashed" in world.record(vid(2))["last_error"]
    assert songs.downloaded + songs.failed == 3 and other.downloaded == 1


def test_summary_counts_match_metadata(world):
    world.set_playlist([vid(i) for i in range(1, 6)])
    world.state["fail"] = [vid(4)]
    world.save_state()
    world.run()
    world.set_playlist([vid(i) for i in (1, 2, 3, 4, 6)])  # vid5 removed, vid6 added
    [summary] = world.run()
    videos = world.metadata()["jobs"]["songs"]["videos"]
    statuses = [v["mp3"]["status"] for v in videos.values()]
    assert summary.downloaded == 1 and summary.already == 3
    assert summary.previously_failed == statuses.count("failed") == 1
    assert summary.gone == 1 and statuses.count("downloaded") == 5
    assert "  new downloaded:                1" in world.output
    assert "  no longer in playlist:         1" in world.output


def test_parallel_downloads_overlap(world):
    """max_workers 3 really runs downloads at the same time; 1 worker doesn't."""
    ids = [vid(i) for i in range(1, 7)]
    world.state["sleep"] = 0.5
    world.save_state()

    def span(calls):
        return max(c["end"] for c in calls) - min(c["start"] for c in calls)

    world.set_playlist(ids)
    world.defaults["max_workers"] = 1
    started = time.monotonic()
    world.run()
    serial_wall, serial = time.monotonic() - started, world.new_calls()

    world.set_playlist(ids, url=OTHER)
    world.jobs = [{"tag": "parallel", "mode": "playlist", "url": OTHER}]
    world.defaults["max_workers"] = 3
    started = time.monotonic()
    world.run()
    parallel_wall, parallel = time.monotonic() - started, world.new_calls()

    assert len({c["pid"] for c in serial}) == 1 and len({c["pid"] for c in parallel}) == 3
    assert span(serial) >= 3.0 and span(parallel) < 2.0
    assert parallel_wall < serial_wall
    print(f"\n  max_workers=1: {serial_wall:.2f}s wall, downloads span {span(serial):.2f}s"
          f"\n  max_workers=3: {parallel_wall:.2f}s wall, downloads span {span(parallel):.2f}s")


def test_output_uses_relative_paths_and_skips_empty_sections(world, monkeypatch):
    monkeypatch.chdir(world.tmp)
    world.set_playlist([vid(1)])
    world.run(dry_run=True)
    assert "Would download (1):" in world.output
    assert "already downloaded (0)" not in world.output
    world.run()
    assert "Downloading 1 video(s) with 1 worker(s) into out/songs\n" in world.output
    assert str(world.tmp) not in world.output
