"""Incremental sync planning: decide what to download (pure, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .metadata import STATUS_DOWNLOADED, STATUS_FAILED


REASON_NEW = "new"
REASON_RETRY = "retrying previously failed"
REASON_MISSING = "file missing on disk"
REASON_FORCED = "forced"


@dataclass
class Entry:
    """One video in a listing."""
    id: str
    title: str | None = None
    url: str | None = None
    duration: float | None = None
    upload_date: str | None = None  # YYYYMMDD
    uploader: str | None = None

    @property
    def label(self) -> str:
        return f"{self.title or '(title not fetched yet)'} [{self.id}]"


@dataclass
class Listing:
    entries: list[Entry]
    playlist_id: str | None = None
    playlist_title: str | None = None
    is_mix: bool = False


@dataclass
class SyncPlan:
    listed: int                                  # unique videos in the listing
    selected: list[Entry]                        # after `order` and `count`
    download: list[tuple[Entry, str]] = field(default_factory=list)  # (entry, reason)
    already: list[Entry] = field(default_factory=list)
    previously_failed: list[Entry] = field(default_factory=list)
    gone: list[tuple[str, str | None]] = field(default_factory=list)  # (id, title) not listed now

    @property
    def up_to_date(self) -> bool:
        return not self.download


def dedupe_entries(entries) -> list[Entry]:
    seen, unique = set(), []
    for entry in entries:
        if entry.id not in seen:
            seen.add(entry.id)
            unique.append(entry)
    return unique


def order_entries(entries, order: str, upload_dates: dict | None = None) -> list[Entry]:
    """Sort a listing by `order`. Sorting is stable, so ties keep playlist order;
    entries missing the sort key (unknown date or duration) go last."""
    entries = list(entries)
    if order == "playlist":
        return entries
    if order in ("title_asc", "title_desc"):
        return sorted(entries, key=lambda e: (e.title or "").casefold(), reverse=order == "title_desc")
    if order in ("newest", "oldest"):
        dates = upload_dates or {}
        key = lambda e: e.upload_date or dates.get(e.id)  # noqa: E731
    elif order in ("shortest", "longest"):
        key = lambda e: e.duration  # noqa: E731
    else:
        raise ValueError(f"unknown order {order!r}")
    known = [e for e in entries if key(e) is not None]
    unknown = [e for e in entries if key(e) is None]
    known.sort(key=key, reverse=order in ("newest", "longest"))
    return known + unknown


def entries_missing_dates(entries, order: str, upload_dates: dict) -> list[Entry]:
    """Entries whose upload date must be looked up before sorting by date."""
    if order not in ("newest", "oldest"):
        return []
    return [e for e in entries if not (e.upload_date or upload_dates.get(e.id))]


def plan_sync(entries, job_entry: dict | None, fmt: str, *, output_dir: Path,
              count: int | None = None, order: str = "playlist",
              upload_dates: dict | None = None, retry_failed: bool = False,
              force: bool = False, verify_files: bool = True,
              report_removed: bool = True) -> SyncPlan:
    """Decide what to download, by video ID and format.

    The listing is de-duplicated, sorted by `order` and cut to the first
    `count` entries. A selected video is skipped only if it has a
    `downloaded` record in this format (and, with verify_files, its file
    still exists). Failed videos are retried only with retry_failed.
    """
    unique = dedupe_entries(entries)
    ordered = order_entries(unique, order, upload_dates)
    selected = ordered if count is None else ordered[:count]
    videos = (job_entry or {}).get("videos", {})
    plan = SyncPlan(listed=len(unique), selected=selected)

    for entry in selected:
        record = videos.get(entry.id, {}).get(fmt)
        status = record.get("status") if record else None
        if force:
            plan.download.append((entry, REASON_FORCED))
        elif status == STATUS_DOWNLOADED:
            path = record.get("file_path")
            if verify_files and not (path and (Path(output_dir) / path).is_file()):
                plan.download.append((entry, REASON_MISSING))
            else:
                plan.already.append(entry)
        elif status == STATUS_FAILED:
            if retry_failed:
                plan.download.append((entry, REASON_RETRY))
            else:
                plan.previously_failed.append(entry)
        else:
            plan.download.append((entry, REASON_NEW))

    if report_removed:
        listed_ids = {e.id for e in unique}
        for video_id, formats in videos.items():
            if video_id not in listed_ids:
                title = next((r.get("title") for r in formats.values() if r.get("title")), None)
                plan.gone.append((video_id, title))
    return plan
