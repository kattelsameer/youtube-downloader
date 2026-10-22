"""CLI tests. In-process tests call cli.main() with yt-dlp faked; Ctrl+C and
two-instance tests run tests/run_fake_cli.py as real subprocesses."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

import fakes
from youtube_downloader import backend, cli
from youtube_downloader.config import load_config

ROOT = Path(__file__).resolve().parent.parent
FAKE_CLI = ROOT / "tests" / "run_fake_cli.py"
PLAYLIST = "https://www.youtube.com/playlist?list=PLtestlist"
VIDEO = "https://www.youtube.com/watch?v=10azSRvOv_E"


def vid(n: int) -> str:
    return f"vid{n:08d}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Config + fake YouTube state; returns a helper namespace."""
    class Env:
        root = tmp_path
        state_path = tmp_path / "state.json"
        config_path = tmp_path / "config.yaml"
        meta_path = tmp_path / "out" / "metadata.json"
        state = {"playlists": {}, "fail": [], "sleep": 0, "calls": str(tmp_path / "calls.jsonl")}
        defaults = {"output_dir": "out", "max_workers": 3, "js_runtime": "none"}
        jobs = [{"tag": "songs", "mode": "playlist", "url": PLAYLIST},
                {"tag": "single", "mode": "single", "url": VIDEO}]

        def write(self):
            self.state_path.write_text(json.dumps(self.state))
            self.config_path.write_text(yaml.safe_dump({"defaults": self.defaults, "jobs": self.jobs}))
            return self

        def playlist(self, ids):
            self.state["playlists"][PLAYLIST] = {"id": "PLtestlist", "title": "Test List",
                                                 "entries": [{"id": i, "title": f"Song {i[-2:]}"} for i in ids]}
            return self.write()

        def meta(self):
            return json.loads(self.meta_path.read_text())

        def cli(self, *args):
            return cli.main(["--config", str(self.config_path), *args])

        def spawn(self, *args):
            return subprocess.Popen(
                [sys.executable, str(FAKE_CLI), "--config", str(self.config_path), *args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, fakes.STATE_ENV: str(self.state_path)},
                start_new_session=True,  # own process group, like a terminal job
            )

    monkeypatch.setenv(fakes.STATE_ENV, str(tmp_path / "state.json"))
    monkeypatch.setattr(backend, "fetch_listing", fakes.fake_listing)
    monkeypatch.setattr(backend, "download_worker", fakes.fake_download)
    monkeypatch.chdir(tmp_path)
    e = Env()
    e.state = json.loads(json.dumps(Env.state))
    e.defaults, e.jobs = dict(Env.defaults), [dict(j) for j in Env.jobs]
    return e.playlist([vid(1), vid(2), vid(3)])


def test_example_config_is_valid():
    cfg = load_config(ROOT / "config.yaml")
    kinds = {(j.mode, j.format) for j in cfg.jobs}
    assert {("single", "mp3"), ("playlist", "mp3"), ("playlist", "mp4"), ("single", "mp4")} <= kinds
    assert [j.tag for j in cfg.jobs if not j.enabled] == ["song-mix"]
    assert cfg.find_job("video-single").settings["max_resolution"] == 360


def test_run_then_list(env, capsys):
    assert env.cli() == cli.EXIT_OK
    capsys.readouterr()
    assert env.cli("--list") == cli.EXIT_OK
    out = capsys.readouterr().out
    lines = {line.split()[0]: line.split() for line in out.splitlines()[2:]}
    assert lines["TAG"][:6] == ["TAG", "MODE", "FORMAT", "ENABLED", "TRACKED", "DOWNLOADED"]
    assert lines["songs"][:7] == ["songs", "playlist", "mp3", "yes", "3", "3", "mp3"]
    assert lines["single"][:7] == ["single", "single", "mp3", "yes", "1", "1", "mp3"]


def test_list_does_not_download_or_write(env, capsys):
    assert env.cli("--list") == cli.EXIT_OK
    assert not env.meta_path.exists() and not (env.root / "calls.jsonl").exists()


def test_job_flag_runs_only_selected_jobs(env, capsys):
    assert env.cli("--job", "single") == cli.EXIT_OK
    assert list(env.meta()["jobs"]) == ["single"]
    assert env.cli("--job", "songs", "--job", "single") == cli.EXIT_OK
    assert set(env.meta()["jobs"]) == {"songs", "single"}


def test_unknown_job_is_a_usage_error(env, capsys):
    assert env.cli("--job", "nope") == cli.EXIT_USAGE
    assert "--job 'nope': no such job (available: songs, single)" in capsys.readouterr().err


def test_config_errors_exit_2_and_name_the_job(env, capsys):
    env.jobs[1]["format"] = "flac"
    env.jobs.append({"tag": "songs", "mode": "single", "url": VIDEO})
    env.write()
    assert env.cli() == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "jobs[1] (tag 'single'): format: must be one of mp3, mp4 (got 'flac')" in err
    assert "jobs[2] (tag 'songs'): duplicate tag 'songs'" in err


def test_dry_run_reports_and_changes_nothing(env, capsys):
    env.cli()
    env.playlist([vid(1), vid(2), vid(3), vid(4)])
    before = env.meta_path.read_bytes()
    capsys.readouterr()
    assert env.cli("--dry-run") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"+ Song 04 [{vid(4)}]  (new)" in out and "Would skip, already downloaded (3):" in out
    assert env.meta_path.read_bytes() == before


def test_status_report(env, capsys):
    env.state["fail"] = [vid(2)]
    env.write()
    assert env.cli() == cli.EXIT_FAILURES  # a failed video makes the exit code 1
    meta = env.meta()
    meta["jobs"]["old-tag"] = {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "videos": {}}
    env.meta_path.write_text(json.dumps(meta))
    capsys.readouterr()
    assert env.cli("--status") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "songs\n" in out and "mp3:       2 downloaded, 1 failed" in out
    assert f"Song 02 [{vid(2)}] mp3, 1 attempt(s): fake failure for {vid(2)}" in out
    assert "old-tag  (not in config any more; kept untouched)" in out
    assert env.cli("--status", "--job", "single") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "single\n" in out and "songs\n" not in out


def test_retry_failed_and_force_flags(env, capsys):
    env.state["fail"] = [vid(2)]
    env.write()
    env.cli()
    env.state["fail"] = []
    env.write()
    assert env.cli("--retry-failed") == cli.EXIT_OK
    assert env.meta()["jobs"]["songs"]["videos"][vid(2)]["mp3"]["status"] == "downloaded"
    calls_before = len(fakes.calls(env.state_path))
    assert env.cli("--force", "--job", "songs") == cli.EXIT_OK
    new = fakes.calls(env.state_path)[calls_before:]
    assert len(new) == 3 and all(c["force"] for c in new)


def test_cli_overrides(env, capsys, tmp_path):
    assert env.cli("--format", "mp4", "--output-dir", "elsewhere", "--max-workers", "1") == cli.EXIT_OK
    meta = json.loads((tmp_path / "elsewhere" / "metadata.json").read_text())
    rec = meta["jobs"]["songs"]["videos"][vid(1)]
    assert set(rec) == {"mp4"} and (tmp_path / "elsewhere" / rec["mp4"]["file_path"]).is_file()
    assert len({c["pid"] for c in fakes.calls(env.state_path)}) <= 2  # 1 worker per job


def test_corrupt_metadata_is_backed_up_and_run_recovers(env, capsys):
    env.meta_path.parent.mkdir(parents=True)
    env.meta_path.write_text('{"schema_version": 1, "jobs": {"songs": ')
    assert env.cli("--status") == cli.EXIT_OK          # read-only: warns, leaves file
    assert "treating it as empty" in capsys.readouterr().err
    assert list(env.meta_path.parent.glob("metadata.json.bak.*")) == []

    assert env.cli() == cli.EXIT_OK
    err = capsys.readouterr().err
    backups = list(env.meta_path.parent.glob("metadata.json.bak.*"))
    assert len(backups) == 1 and backups[0].read_text().startswith('{"schema_version": 1, "jobs": {"songs": ')
    assert "Backed it up to" in err
    assert len(env.meta()["jobs"]["songs"]["videos"]) == 3


def test_metadata_path_that_is_a_folder_exits_4_and_is_left_alone(env, capsys):
    env.meta_path.mkdir(parents=True)
    (env.meta_path / "keep.txt").write_text("x")
    assert env.cli() == cli.EXIT_METADATA
    assert "exists but is not a file" in capsys.readouterr().err
    assert (env.meta_path / "keep.txt").is_file()
    assert list(env.meta_path.parent.glob("metadata.json.bak.*")) == []


def test_newer_metadata_schema_is_refused(env, capsys):
    env.meta_path.parent.mkdir(parents=True)
    env.meta_path.write_text('{"schema_version": 99, "jobs": {}}')
    assert env.cli() == cli.EXIT_METADATA
    assert "newer than this tool supports" in capsys.readouterr().err


def test_ctrl_c_saves_progress_and_next_run_continues(env):
    ids = [vid(i) for i in range(1, 9)]
    env.jobs = [{"tag": "songs", "mode": "playlist", "url": PLAYLIST}]
    env.defaults["max_workers"] = 2
    env.state["sleep"] = 0.8
    env.playlist(ids)

    proc = env.spawn()
    deadline = time.monotonic() + 30
    done = 0
    while time.monotonic() < deadline and done < 3:
        time.sleep(0.1)
        if env.meta_path.exists():
            try:
                done = len(env.meta()["jobs"]["songs"]["videos"])
            except (json.JSONDecodeError, KeyError):
                pass  # never happens with atomic writes, but don't flake
    assert done >= 3, "downloads did not start"
    os.killpg(proc.pid, signal.SIGINT)  # what Ctrl+C in a terminal does
    out, err = proc.communicate(timeout=30)

    assert proc.returncode == cli.EXIT_INTERRUPTED, err
    assert "Interrupted: stopping workers." in out
    saved = env.meta()["jobs"]["songs"]["videos"]
    assert 3 <= len(saved) < len(ids)
    assert all(r["mp3"]["status"] == "downloaded" for r in saved.values())
    for record in saved.values():
        assert (env.root / "out" / record["mp3"]["file_path"]).is_file()

    env.state["sleep"] = 0
    env.write()
    calls_before = len(fakes.calls(env.state_path))
    rerun = env.spawn()
    out, err = rerun.communicate(timeout=60)
    assert rerun.returncode == cli.EXIT_OK, err
    resumed = [c["id"] for c in fakes.calls(env.state_path)[calls_before:]]
    assert sorted(resumed) == sorted(set(ids) - set(saved))
    assert sorted(env.meta()["jobs"]["songs"]["videos"]) == ids


def test_ctrl_c_stops_in_flight_downloads_promptly(env):
    """Downloads running at Ctrl+C time are killed, not waited for."""
    env.jobs = [{"tag": "songs", "mode": "playlist", "url": PLAYLIST}]
    env.defaults["max_workers"] = 2
    env.state["sleep"] = 3.0
    env.playlist([vid(i) for i in range(1, 7)])

    proc = env.spawn()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(fakes.calls(env.state_path)) < 2:
        time.sleep(0.05)
    time.sleep(0.5)  # the next two downloads are now in flight, ~2.5s from done
    os.killpg(proc.pid, signal.SIGINT)
    interrupted_at = time.monotonic()
    proc.communicate(timeout=30)
    took = time.monotonic() - interrupted_at

    assert proc.returncode == cli.EXIT_INTERRUPTED
    assert took < 2.0, f"exit took {took:.1f}s: in-flight workers were waited for"
    time.sleep(3.5)  # a surviving worker would finish and log its call by now
    assert len(fakes.calls(env.state_path)) == 2
    files = sorted((env.root / "out" / "songs").glob("*.mp3"))
    assert len(files) == len(env.meta()["jobs"]["songs"]["videos"]) == 2


def test_second_instance_is_locked_out(env):
    env.jobs = [{"tag": "songs", "mode": "playlist", "url": PLAYLIST}]
    env.state["sleep"] = 1.0
    env.defaults["max_workers"] = 1
    env.playlist([vid(1), vid(2), vid(3)])
    lock = env.meta_path.with_name("metadata.json.lock")

    first = env.spawn()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (lock.exists() and lock.read_text().startswith("pid")):
        time.sleep(0.05)
    second = env.spawn()
    out2, err2 = second.communicate(timeout=30)
    out1, err1 = first.communicate(timeout=60)

    assert second.returncode == cli.EXIT_LOCKED
    assert "another instance is already running (pid" in err2
    assert "lock file: out/metadata.json.lock" in err2  # relative to the current folder
    assert first.returncode == cli.EXIT_OK, err1
    assert sorted(env.meta()["jobs"]["songs"]["videos"]) == [vid(1), vid(2), vid(3)]
    assert len(fakes.calls(env.state_path)) == 3  # the second instance downloaded nothing
