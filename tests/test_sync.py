"""Incremental sync rules, tested on the pure planner (no network, no files written)."""
import pytest

import youtube_downloader.sync as d


def entries(*ids, **fields):
    return [d.Entry(id=i, title=f"Title {i}", **fields) for i in ids]


def record(status="downloaded", fmt="mp3", file_path=None, title=None):
    return {"status": status, "format": fmt, "file_path": file_path, "title": title}


def job_entry(**videos):
    return {"videos": videos}


@pytest.fixture
def out(tmp_path):
    return tmp_path


def touch(out, rel):
    (out / rel).parent.mkdir(parents=True, exist_ok=True)
    (out / rel).write_bytes(b"x")
    return rel


def ids(items):
    return [e.id if isinstance(e, d.Entry) else e[0].id for e in items]


def test_everything_new_on_first_run(out):
    plan = d.plan_sync(entries("a", "b", "c"), None, "mp3", output_dir=out)
    assert ids(plan.download) == ["a", "b", "c"]
    assert {reason for _, reason in plan.download} == {d.REASON_NEW}
    assert not plan.up_to_date and plan.listed == 3


def test_second_run_is_up_to_date(out):
    meta = job_entry(**{v: {"mp3": record(file_path=touch(out, f"t/{v}.mp3"))} for v in "abc"})
    plan = d.plan_sync(entries("a", "b", "c"), meta, "mp3", output_dir=out)
    assert plan.up_to_date and ids(plan.already) == ["a", "b", "c"]


def test_playlist_grows_from_three_to_five(out):
    meta = job_entry(**{v: {"mp3": record(file_path=touch(out, f"t/{v}.mp3"))} for v in "abc"})
    plan = d.plan_sync(entries("a", "b", "c", "d", "e"), meta, "mp3", output_dir=out)
    assert ids(plan.download) == ["d", "e"] and ids(plan.already) == ["a", "b", "c"]


def test_removed_video_is_reported_not_touched(out):
    meta = job_entry(a={"mp3": record(file_path=touch(out, "t/a.mp3"), title="Gone Song")},
                     b={"mp3": record(file_path=touch(out, "t/b.mp3"))})
    plan = d.plan_sync(entries("b"), meta, "mp3", output_dir=out)
    assert plan.gone == [("a", "Gone Song")]
    assert plan.up_to_date
    assert meta["videos"]["a"]["mp3"]["status"] == "downloaded"  # planner never mutates
    assert (out / "t/a.mp3").exists()


def test_single_mode_does_not_report_removed(out):
    meta = job_entry(old={"mp3": record(file_path=touch(out, "t/old.mp3"))})
    plan = d.plan_sync(entries("new"), meta, "mp3", output_dir=out, report_removed=False)
    assert plan.gone == [] and ids(plan.download) == ["new"]


def test_previously_failed_is_skipped_unless_retry(out):
    meta = job_entry(a={"mp3": record(status="failed")})
    plan = d.plan_sync(entries("a", "b"), meta, "mp3", output_dir=out)
    assert ids(plan.previously_failed) == ["a"] and ids(plan.download) == ["b"]
    plan = d.plan_sync(entries("a", "b"), meta, "mp3", output_dir=out, retry_failed=True)
    assert plan.download[0] == (plan.selected[0], d.REASON_RETRY) and ids(plan.download) == ["a", "b"]
    assert plan.previously_failed == []


def test_force_redownloads_everything_selected(out):
    meta = job_entry(a={"mp3": record(file_path=touch(out, "t/a.mp3"))}, b={"mp3": record("failed")})
    plan = d.plan_sync(entries("a", "b", "c"), meta, "mp3", output_dir=out, force=True)
    assert ids(plan.download) == ["a", "b", "c"]
    assert {reason for _, reason in plan.download} == {d.REASON_FORCED}


def test_format_change_downloads_again_in_new_format(out):
    meta = job_entry(**{v: {"mp3": record(file_path=touch(out, f"t/{v}.mp3"))} for v in "ab"})
    plan = d.plan_sync(entries("a", "b"), meta, "mp4", output_dir=out)
    assert ids(plan.download) == ["a", "b"] and plan.already == []
    assert set(meta["videos"]["a"]) == {"mp3"}  # mp3 records untouched
    plan = d.plan_sync(entries("a", "b"), meta, "mp3", output_dir=out)
    assert plan.up_to_date


def test_verify_files_redownloads_missing_file(out):
    meta = job_entry(a={"mp3": record(file_path="t/deleted.mp3")})
    plan = d.plan_sync(entries("a"), meta, "mp3", output_dir=out, verify_files=True)
    assert plan.download == [(plan.selected[0], d.REASON_MISSING)]
    plan = d.plan_sync(entries("a"), meta, "mp3", output_dir=out, verify_files=False)
    assert plan.up_to_date and ids(plan.already) == ["a"]


def test_count_window(out):
    plan = d.plan_sync(entries("a", "b", "c", "d", "e"), None, "mp3", output_dir=out, count=3)
    assert ids(plan.selected) == ["a", "b", "c"] and ids(plan.download) == ["a", "b", "c"]


def test_count_larger_than_playlist_takes_all(out):
    plan = d.plan_sync(entries("a", "b"), None, "mp3", output_dir=out, count=10)
    assert ids(plan.download) == ["a", "b"] and plan.listed == 2


def test_new_video_at_top_pushes_oldest_out_of_window(out):
    meta = job_entry(**{v: {"mp3": record(file_path=touch(out, f"t/{v}.mp3"))} for v in "abc"})
    plan = d.plan_sync(entries("new", "a", "b", "c"), meta, "mp3", output_dir=out, count=3)
    assert ids(plan.selected) == ["new", "a", "b"]
    assert ids(plan.download) == ["new"] and ids(plan.already) == ["a", "b"]
    assert plan.gone == []  # 'c' is still in the playlist, just outside the window


def test_duplicate_ids_in_listing_count_once(out):
    plan = d.plan_sync(entries("a", "b", "a", "c"), None, "mp3", output_dir=out, count=3)
    assert ids(plan.selected) == ["a", "b", "c"] and plan.listed == 3


# --- order -----------------------------------------------------------------

def dated():
    return [
        d.Entry("a", "Charlie", duration=300, upload_date="20240101"),
        d.Entry("b", "alpha", duration=100),
        d.Entry("c", "Bravo", duration=200, upload_date="20250101"),
        d.Entry("e", "echo", duration=None, upload_date="20240101"),
    ]


@pytest.mark.parametrize("order, expected", [
    ("playlist", ["a", "b", "c", "e"]),
    ("newest", ["c", "a", "e", "b"]),      # ties keep playlist order, undated last
    ("oldest", ["a", "e", "c", "b"]),
    ("title_asc", ["b", "c", "a", "e"]),   # case-insensitive
    ("title_desc", ["e", "a", "c", "b"]),
    ("shortest", ["b", "c", "a", "e"]),    # unknown duration last
    ("longest", ["a", "c", "b", "e"]),
])
def test_order_entries(order, expected):
    assert [e.id for e in d.order_entries(dated(), order)] == expected


def test_order_uses_cached_upload_dates():
    cached = {"b": "20260101"}
    assert [e.id for e in d.order_entries(dated(), "newest", cached)] == ["b", "c", "a", "e"]


def test_order_then_count(out):
    plan = d.plan_sync(dated(), None, "mp3", output_dir=out, order="newest", count=2)
    assert ids(plan.selected) == ["c", "a"]


def test_entries_missing_dates():
    assert [e.id for e in d.entries_missing_dates(dated(), "newest", {})] == ["b"]
    assert d.entries_missing_dates(dated(), "newest", {"b": "20200101"}) == []
    assert d.entries_missing_dates(dated(), "title_asc", {}) == []
