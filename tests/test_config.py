from pathlib import Path

import pytest
import yaml

import youtube_downloader.config as d

VIDEO = "https://www.youtube.com/watch?v=10azSRvOv_E"
PLAYLIST = "https://www.youtube.com/playlist?list=PLfP6i5T0-DkK52drjlLM2wEGXSL-rqF4T"
MIX = "https://www.youtube.com/watch?v=10azSRvOv_E&list=RD10azSRvOv_E&start_radio=1"


def write_config(tmp_path: Path, data) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False))
    return path


def base_config(**defaults):
    return {
        "defaults": {"format": "mp3", **defaults},
        "jobs": [
            {"tag": "song-single", "mode": "single", "url": VIDEO},
            {"tag": "song-playlist", "mode": "playlist", "url": PLAYLIST, "count": 3},
            {"tag": "video-single", "mode": "single", "url": VIDEO, "format": "mp4",
             "max_resolution": 360},
        ],
    }


def errors_for(tmp_path, data, **kwargs) -> list[str]:
    with pytest.raises(d.ConfigError) as info:
        d.load_config(write_config(tmp_path, data), **kwargs)
    return info.value.errors


# --- happy path and merging ------------------------------------------------

def test_valid_config_loads(tmp_path):
    cfg = d.load_config(write_config(tmp_path, base_config()))
    assert [j.tag for j in cfg.jobs] == ["song-single", "song-playlist", "video-single"]
    single, playlist, video = cfg.jobs
    assert single.count is None and playlist.count == 3
    assert playlist.mode == "playlist" and single.enabled
    assert video.format == "mp4" and video.settings["max_resolution"] == 360
    assert cfg.warnings == []


def test_merge_precedence_cli_over_job_over_defaults_over_builtin(tmp_path):
    data = base_config(max_workers=2, audio_quality=320)
    data["jobs"][1]["max_workers"] = 5
    cfg = d.load_config(write_config(tmp_path, data), overrides={"max_workers": 7})
    single, playlist, video = cfg.jobs
    # built-in default untouched by config
    assert single.settings["retries"] == d.BUILTIN_DEFAULTS["retries"]
    # defaults override built-in
    assert single.settings["audio_quality"] == 320
    # CLI beats both job (5) and defaults (2)
    assert {j.settings["max_workers"] for j in cfg.jobs} == {7}

    cfg = d.load_config(write_config(tmp_path, data))
    assert [j.settings["max_workers"] for j in cfg.jobs] == [2, 5, 2]


def test_job_level_override_of_a_default(tmp_path):
    cfg = d.load_config(write_config(tmp_path, base_config()))
    assert cfg.defaults["format"] == "mp3"
    assert cfg.jobs[2].format == "mp4"
    assert cfg.jobs[0].format == "mp3"


def test_cli_format_override_applies_to_every_job(tmp_path):
    cfg = d.load_config(write_config(tmp_path, base_config()), overrides={"format": "mp4"})
    assert {j.format for j in cfg.jobs} == {"mp4"}


def test_paths_resolve_against_config_folder(tmp_path):
    cfg = d.load_config(write_config(tmp_path, base_config(output_dir="media")))
    assert cfg.jobs[0].output_dir == (tmp_path / "media").resolve()
    assert cfg.jobs[0].tag_dir == (tmp_path / "media" / "song-single").resolve()
    assert cfg.metadata_file == (tmp_path / "media" / "metadata.json").resolve()


def test_cli_output_dir_is_relative_to_cwd_and_moves_default_metadata(tmp_path):
    cwd = tmp_path / "elsewhere"
    cfg = d.load_config(write_config(tmp_path, base_config()), overrides={"output_dir": "out"}, cwd=cwd)
    assert all(j.output_dir == (cwd / "out").resolve() for j in cfg.jobs)
    assert cfg.metadata_file == (cwd / "out" / "metadata.json").resolve()


def test_explicit_metadata_file(tmp_path):
    cfg = d.load_config(write_config(tmp_path, base_config(metadata_file="state/meta.json")))
    assert cfg.metadata_file == (tmp_path / "state" / "meta.json").resolve()


@pytest.mark.parametrize("raw, expected", [
    ("2M", 2 * 1024 ** 2), ("500K", 500 * 1024), ("1.5MiB", int(1.5 * 1024 ** 2)),
    (1000, 1000), (None, None),
])
def test_rate_limit_parsing(tmp_path, raw, expected):
    cfg = d.load_config(write_config(tmp_path, base_config(rate_limit=raw)))
    assert cfg.jobs[0].settings["rate_limit"] == expected


@pytest.mark.parametrize("raw, expected", [("720p", 720), ("best", "best"), (1080, 1080)])
def test_max_resolution_parsing(tmp_path, raw, expected):
    cfg = d.load_config(write_config(tmp_path, base_config(max_resolution=raw)))
    assert cfg.jobs[0].settings["max_resolution"] == expected


def test_count_all_and_case_insensitive(tmp_path):
    data = base_config()
    data["jobs"][1]["count"] = "ALL"
    cfg = d.load_config(write_config(tmp_path, data))
    assert cfg.jobs[1].count is None


def test_count_in_single_mode_is_a_warning(tmp_path):
    data = base_config()
    data["jobs"][0]["count"] = 3
    cfg = d.load_config(write_config(tmp_path, data))
    assert any("count is ignored in single mode" in w for w in cfg.warnings)


def test_disabled_job_is_loaded_but_marked(tmp_path):
    data = base_config()
    data["jobs"][1]["enabled"] = False
    cfg = d.load_config(write_config(tmp_path, data))
    assert [j.enabled for j in cfg.jobs] == [True, False, True]


def test_mix_url_is_accepted_as_playlist(tmp_path):
    data = base_config()
    data["jobs"][1]["url"] = MIX
    cfg = d.load_config(write_config(tmp_path, data))
    assert cfg.jobs[1].url == MIX
    assert d.is_mix_playlist(d.parse_youtube_url(MIX)[1])
    assert not d.is_mix_playlist(d.parse_youtube_url(PLAYLIST)[1])


# --- tag sanitization ------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("lofi-songs", "lofi-songs"),
    ("Lofi Songs!", "Lofi-Songs"),
    ("tech  talks / 2024", "tech-talks-2024"),
    ("../../etc/passwd", "etcpasswd"),
    ("  café mix  ", "café-mix"),
    ("...", ""),
    (2024, "2024"),
])
def test_sanitize_tag(raw, expected):
    assert d.sanitize_tag(raw) == expected


def test_sanitized_tag_is_used_for_folder_and_lookup(tmp_path):
    data = base_config()
    data["jobs"][0]["tag"] = "My Songs!"
    cfg = d.load_config(write_config(tmp_path, data))
    job = cfg.jobs[0]
    assert job.tag == "My-Songs" and job.name == "My Songs!"
    assert job.tag_dir.name == "My-Songs"
    assert cfg.find_job("my songs!") is job and cfg.find_job("My-Songs") is job


def test_sanitize_folder_name():
    assert d.sanitize_folder_name('Mix: "Best" <of> 2024?') == "Mix_ _Best_ _of_ 2024_"
    assert d.sanitize_folder_name("  ...  ") == "playlist"


# --- validation errors -----------------------------------------------------

def test_duplicate_tags_fail_and_name_both_jobs(tmp_path):
    data = base_config()
    data["jobs"][2]["tag"] = "Song Single"  # same folder as 'song-single' (case-insensitive)
    errors = errors_for(tmp_path, data)
    assert any("jobs[2] (tag 'Song Single'): duplicate tag" in e and "jobs[0]" in e for e in errors)


def test_bad_format_value_names_the_job(tmp_path):
    data = base_config()
    data["jobs"][1]["format"] = "flac"
    errors = errors_for(tmp_path, data)
    assert errors == ["jobs[1] (tag 'song-playlist'): format: must be one of mp3, mp4 (got 'flac')"]


def test_bad_default_value(tmp_path):
    errors = errors_for(tmp_path, base_config(format="wav"))
    assert errors == ["defaults: format: must be one of mp3, mp4 (got 'wav')"]


@pytest.mark.parametrize("count", [0, -1, "ten", 1.5, True])
def test_invalid_count(tmp_path, count):
    data = base_config()
    data["jobs"][1]["count"] = count
    errors = errors_for(tmp_path, data)
    assert len(errors) == 1 and errors[0].startswith("jobs[1] (tag 'song-playlist'): count:")


def test_missing_url_and_mode(tmp_path):
    data = base_config()
    del data["jobs"][0]["url"]
    del data["jobs"][1]["mode"]
    errors = errors_for(tmp_path, data)
    assert "jobs[0] (tag 'song-single'): missing required key 'url'" in errors
    assert "jobs[1] (tag 'song-playlist'): missing required key 'mode'" in errors


def test_missing_tag(tmp_path):
    data = base_config()
    del data["jobs"][0]["tag"]
    assert "jobs[0]: missing required key 'tag'" in errors_for(tmp_path, data)


@pytest.mark.parametrize("url, fragment", [
    ("https://vimeo.com/12345", "is not a YouTube URL"),
    ("not a url", "is not a YouTube URL"),
    ("https://www.youtube.com/watch?v=short", "invalid video id"),
])
def test_invalid_urls(tmp_path, url, fragment):
    data = base_config()
    data["jobs"][0]["url"] = url
    errors = errors_for(tmp_path, data)
    assert len(errors) == 1 and errors[0].startswith("jobs[0] (tag 'song-single'): url:")
    assert fragment in errors[0]


def test_mode_url_mismatch(tmp_path):
    data = base_config()
    data["jobs"][0]["url"] = PLAYLIST          # single mode, no video id
    data["jobs"][1]["url"] = VIDEO             # playlist mode, no list=
    errors = errors_for(tmp_path, data)
    assert any("single mode needs a video URL" in e for e in errors)
    assert any("playlist mode needs a playlist URL" in e for e in errors)


def test_unknown_keys_with_suggestion(tmp_path):
    data = base_config(max_worker=3)
    data["jobs"][0]["formt"] = "mp3"
    data["extra"] = 1
    errors = errors_for(tmp_path, data)
    assert "defaults: unknown key 'max_worker' (did you mean 'max_workers'?)" in errors
    assert "jobs[0] (tag 'song-single'): unknown key 'formt' (did you mean 'format'?)" in errors
    assert "config: unknown key 'extra'" in errors


def test_metadata_file_is_global_only(tmp_path):
    data = base_config()
    data["jobs"][0]["metadata_file"] = "x.json"
    errors = errors_for(tmp_path, data)
    assert errors == ["jobs[0] (tag 'song-single'): 'metadata_file' is global only; set it under defaults"]


def test_metadata_file_must_not_be_a_folder(tmp_path):
    (tmp_path / "downloads").mkdir()
    errors = errors_for(tmp_path, base_config(metadata_file="downloads"))
    assert errors == ["defaults: metadata_file: 'downloads' is a folder; give a file path such as "
                      "downloads/metadata.json"]


def test_metadata_file_type_error_reported_with_other_errors(tmp_path):
    errors = errors_for(tmp_path, base_config(metadata_file=5, format="wav"))
    assert "defaults: metadata_file: must be a file path (got 5)" in errors and len(errors) == 2


def test_job_keys_rejected_in_defaults(tmp_path):
    errors = errors_for(tmp_path, base_config(url=VIDEO))
    assert errors == ["defaults: 'url' is a job setting; set it on each job"]


def test_every_problem_reported_at_once(tmp_path):
    data = base_config(max_workers=0)
    data["jobs"][0]["mode"] = "album"
    data["jobs"][1]["count"] = "lots"
    data["jobs"][2]["enabled"] = "yes"
    errors = errors_for(tmp_path, data)
    assert len(errors) == 4


def test_cookies_file_must_exist(tmp_path):
    errors = errors_for(tmp_path, base_config(cookies_file="cookies.txt"))
    assert len(errors) == 3 and all("cookies_file not found" in e for e in errors)
    (tmp_path / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
    cfg = d.load_config(write_config(tmp_path, base_config(cookies_file="cookies.txt")))
    assert cfg.jobs[0].settings["cookies_file"] == str((tmp_path / "cookies.txt").resolve())


def test_bad_cli_override(tmp_path):
    errors = errors_for(tmp_path, base_config(), overrides={"max_workers": 0, "format": "ogg"})
    assert "command line: format: must be one of mp3, mp4 (got 'ogg')" in errors
    assert any(e.startswith("command line: max_workers:") for e in errors)


def test_filename_template_must_stay_inside_folder(tmp_path):
    errors = errors_for(tmp_path, base_config(filename_template="../%(title)s.%(ext)s"))
    assert errors and "inside the job folder" in errors[0]
    errors = errors_for(tmp_path, base_config(filename_template="%(title)s.mp3"))
    assert errors and "%(ext)s" in errors[0]


@pytest.mark.parametrize("text, fragment", [
    ("jobs: [", "invalid YAML"),
    ("- just\n- a list\n", "top level must be a mapping"),
    ("defaults: {}\n", "jobs: must be a non-empty list"),
    ("defaults: {}\njobs: []\n", "jobs: must be a non-empty list"),
])
def test_structural_errors(tmp_path, text, fragment):
    with pytest.raises(d.ConfigError) as info:
        d.load_config(write_config(tmp_path, text))
    assert fragment in str(info.value)


def test_missing_config_file(tmp_path):
    with pytest.raises(d.ConfigError, match="config file not found"):
        d.load_config(tmp_path / "nope.yaml")


# --- job selection ---------------------------------------------------------

def test_select_jobs(tmp_path):
    cfg = d.load_config(write_config(tmp_path, base_config()))
    assert [j.tag for j in d.select_jobs(cfg, None)] == ["song-single", "song-playlist", "video-single"]
    assert [j.tag for j in d.select_jobs(cfg, ["video-single", "song-single"])] == ["song-single", "video-single"]
    with pytest.raises(d.ConfigError, match="no such job"):
        d.select_jobs(cfg, ["nope"])


def test_parse_youtube_url_variants():
    assert d.parse_youtube_url("https://youtu.be/10azSRvOv_E?t=3") == ("10azSRvOv_E", None)
    assert d.parse_youtube_url("https://www.youtube.com/shorts/10azSRvOv_E") == ("10azSRvOv_E", None)
    assert d.parse_youtube_url(MIX) == ("10azSRvOv_E", "RD10azSRvOv_E")
    assert d.is_collection_url("https://www.youtube.com/@SomeChannel/videos")
    assert not d.is_collection_url(VIDEO)
