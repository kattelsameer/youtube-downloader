import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest

import downloader as d

ROOT = Path(__file__).resolve().parent.parent


def make_job(tmp_path, tag="lofi", fmt="mp3", mode="playlist"):
    settings = {**d.BUILTIN_DEFAULTS, "format": fmt, "output_dir": str(tmp_path / "out")}
    return d.Job(tag=tag, name=tag, mode=mode, url="https://www.youtube.com/playlist?list=PLx",
                 enabled=True, count=None, settings=settings)


def ok_result(video_id, job, title="Song"):
    rel = f"{job.tag}/{title} [{video_id}].{job.format}"
    path = job.output_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    return {"video_id": video_id, "ok": True, "title": title, "uploader": "Artist",
            "duration": 120, "upload_date": "20240102", "file_path": rel, "file_size": 10}


def fail_result(video_id, error="HTTP Error 403"):
    return {"video_id": video_id, "ok": False, "error": error}


def test_missing_file_gives_empty_store_and_save_creates_it(tmp_path):
    path = tmp_path / "out" / "metadata.json"
    store = d.MetadataStore(path).load()
    assert store.data["jobs"] == {} and not path.exists()
    store.save()
    data = json.loads(path.read_text())
    assert data["schema_version"] == d.SCHEMA_VERSION and data["jobs"] == {}
    assert data["app_version"] == d.read_version()


def test_record_success_roundtrip(tmp_path):
    job = make_job(tmp_path)
    path = tmp_path / "metadata.json"
    store = d.MetadataStore(path).load()
    store.record_result(job, ok_result("aaaaaaaaaaa", job))
    reloaded = d.MetadataStore(path).load()
    entry = reloaded.job_entry("lofi")
    assert entry["url"] == job.url and entry["mode"] == "playlist" and entry["format"] == "mp3"
    record = reloaded.video_record("lofi", "aaaaaaaaaaa", "mp3")
    assert record["status"] == "downloaded" and record["attempts"] == 1
    assert record["file_path"] == "lofi/Song [aaaaaaaaaaa].mp3" and record["file_size"] == 10
    assert record["title"] == "Song" and record["uploader"] == "Artist" and record["duration"] == 120
    assert record["downloaded_at"] and record["last_error"] is None
    assert reloaded.upload_dates["aaaaaaaaaaa"] == "20240102"


def test_failure_then_success_counts_attempts(tmp_path):
    job = make_job(tmp_path)
    store = d.MetadataStore(tmp_path / "metadata.json").load()
    rec = store.record_result(job, fail_result("bbbbbbbbbbb"))
    assert rec["status"] == "failed" and rec["attempts"] == 1 and rec["last_error"] == "HTTP Error 403"
    rec = store.record_result(job, fail_result("bbbbbbbbbbb", "timeout"))
    assert rec["status"] == "failed" and rec["attempts"] == 2 and rec["last_error"] == "timeout"
    rec = store.record_result(job, ok_result("bbbbbbbbbbb", job))
    assert rec["status"] == "downloaded" and rec["attempts"] == 3 and rec["last_error"] is None


def test_formats_are_kept_side_by_side(tmp_path):
    path = tmp_path / "metadata.json"
    store = d.MetadataStore(path).load()
    mp3_job, mp4_job = make_job(tmp_path, fmt="mp3"), make_job(tmp_path, fmt="mp4")
    store.record_result(mp3_job, ok_result("ccccccccccc", mp3_job))
    store.record_result(mp4_job, ok_result("ccccccccccc", mp4_job))
    reloaded = d.MetadataStore(path).load()
    formats = reloaded.job_entry("lofi")["videos"]["ccccccccccc"]
    assert set(formats) == {"mp3", "mp4"}
    assert formats["mp3"]["file_path"].endswith(".mp3") and formats["mp4"]["file_path"].endswith(".mp4")
    assert reloaded.job_entry("lofi")["format"] == "mp4"


def test_failed_redownload_keeps_good_existing_file(tmp_path):
    job = make_job(tmp_path)
    store = d.MetadataStore(tmp_path / "metadata.json").load()
    good = ok_result("ddddddddddd", job)
    store.record_result(job, good)
    rec = store.record_result(job, fail_result("ddddddddddd"))
    assert rec["status"] == "downloaded" and rec["last_error"] == "HTTP Error 403"
    (job.output_dir / good["file_path"]).unlink()
    rec = store.record_result(job, fail_result("ddddddddddd"))
    assert rec["status"] == "failed"


def test_jobs_coexist_and_unknown_tags_are_preserved(tmp_path):
    path = tmp_path / "metadata.json"
    store = d.MetadataStore(path).load()
    a, b = make_job(tmp_path, tag="a"), make_job(tmp_path, tag="b", fmt="mp4")
    store.record_result(a, ok_result("eeeeeeeeeee", a))
    store.record_result(b, ok_result("eeeeeeeeeee", b))
    store.record_result(b, fail_result("fffffffffff"))
    store.data["jobs"]["removed-tag"] = {"url": "u", "videos": {"x": {"mp3": {"status": "downloaded"}}}}
    store.save()

    store = d.MetadataStore(path).load()
    store.record_result(a, ok_result("ggggggggggg", a))
    data = json.loads(path.read_text())
    assert set(data["jobs"]) == {"a", "b", "removed-tag"}
    assert set(data["jobs"]["a"]["videos"]) == {"eeeeeeeeeee", "ggggggggggg"}
    assert set(data["jobs"]["b"]["videos"]) == {"eeeeeeeeeee", "fffffffffff"}
    assert data["jobs"]["removed-tag"] == {"url": "u", "videos": {"x": {"mp3": {"status": "downloaded"}}}}


def test_finish_job_sets_last_run_and_playlist(tmp_path):
    job = make_job(tmp_path)
    store = d.MetadataStore(tmp_path / "metadata.json").load()
    store.finish_job(job, "PLx", "My List")
    entry = d.MetadataStore(tmp_path / "metadata.json").load().job_entry("lofi")
    assert entry["playlist_id"] == "PLx" and entry["playlist_title"] == "My List" and entry["last_run"]


# --- atomic writes ---------------------------------------------------------

def test_atomic_write_keeps_old_file_when_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    d.atomic_write_json(path, {"v": 1})

    def boom(data, fh, **kwargs):
        fh.write('{"v": 2, "trunc')
        raise KeyboardInterrupt  # e.g. Ctrl+C in the middle of a write

    monkeypatch.setattr(d.json, "dump", boom)
    with pytest.raises(KeyboardInterrupt):
        d.atomic_write_json(path, {"v": 2})
    assert json.loads(path.read_text()) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["metadata.json"]  # no temp file left


# --- corrupt files ---------------------------------------------------------

@pytest.mark.parametrize("content", [
    "{not json",
    "",
    '["a list"]',
    '{"schema_version": 1, "jobs": []}',
    '{"schema_version": 1, "jobs": {"t": {"videos": {"v": "oops"}}}}',
])
def test_corrupt_file_is_backed_up_and_store_starts_fresh(tmp_path, caplog, content):
    path = tmp_path / "metadata.json"
    path.write_text(content)
    with caplog.at_level(logging.WARNING, logger="ytdl"):
        store = d.MetadataStore(path).load()
    assert store.data["jobs"] == {}
    assert store.recovered_from is not None and store.recovered_from.read_text() == content
    assert store.recovered_from.name.startswith("metadata.json.bak.")
    assert "corrupt or unreadable" in caplog.text and str(store.recovered_from) in caplog.text
    assert not path.exists()
    store.save()
    assert json.loads(path.read_text())["schema_version"] == d.SCHEMA_VERSION


def test_two_backups_in_the_same_second_do_not_collide(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text("bad 1")
    first = d.MetadataStore(path).load().recovered_from
    path.write_text("bad 2")
    second = d.MetadataStore(path).load().recovered_from
    assert first != second and first.read_text() == "bad 1" and second.read_text() == "bad 2"


def test_read_only_store_does_not_touch_a_corrupt_file(tmp_path, caplog):
    path = tmp_path / "metadata.json"
    path.write_text("{bad")
    with caplog.at_level(logging.WARNING, logger="ytdl"):
        store = d.MetadataStore(path, read_only=True).load()
    assert store.data["jobs"] == {} and store.recovered_from is None
    assert path.read_text() == "{bad" and len(list(tmp_path.iterdir())) == 1
    assert "treating it as empty" in caplog.text
    with pytest.raises(RuntimeError):
        store.save()


def test_newer_schema_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text('{"schema_version": 99, "jobs": {}}')
    with pytest.raises(d.MetadataError, match="newer than this tool supports"):
        d.MetadataStore(path).load()
    assert path.read_text() == '{"schema_version": 99, "jobs": {}}'


# --- lock file -------------------------------------------------------------

HOLD_LOCK = """
import sys, time
sys.path.insert(0, {root!r})
import downloader as d
lock = d.MetadataLock(d.Path({path!r})).acquire()
print("locked", flush=True)
time.sleep(30)
"""


def start_lock_holder(path):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK.format(root=str(ROOT), path=str(path))],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_lock_blocks_a_second_instance(tmp_path):
    path = tmp_path / "metadata.json"
    holder = start_lock_holder(path)
    try:
        with pytest.raises(d.LockError, match=r"another instance is already running \(pid \d+"):
            d.MetadataLock(path).acquire()
    finally:
        holder.kill()
        holder.wait()


def test_lock_is_released_when_holder_dies(tmp_path):
    path = tmp_path / "metadata.json"
    holder = start_lock_holder(path)
    holder.kill()
    holder.wait()
    with d.MetadataLock(path) as lock:
        assert lock.path.read_text().startswith("pid ")


def test_lock_released_after_context_and_reusable(tmp_path):
    path = tmp_path / "metadata.json"
    with d.MetadataLock(path):
        with pytest.raises(d.LockError):
            d.MetadataLock(path).acquire()
    with d.MetadataLock(path) as lock:
        assert lock.path.name == "metadata.json.lock"
    assert lock.path.exists() and lock.path.read_text() == ""
