"""Running jobs: list, plan, download in parallel, record, summarize.

Jobs run one after another; the videos inside a job download in parallel in
a process pool. Workers only download and return a result dict. The main
process is the only one that touches the metadata file, and it flushes it
after every finished video, so Ctrl+C or a crash never loses progress.
"""

from __future__ import annotations

import logging
import signal
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

from . import backend
from .config import Job, sanitize_folder_name
from .logs import configure_logging, set_level
from .metadata import STATUS_DOWNLOADED, MetadataStore
from .sync import REASON_NEW, Listing, SyncPlan, entries_missing_dates, plan_sync

log = logging.getLogger("ytdl")

UP_TO_DATE = "Up to date, nothing to download"


@dataclass
class RunOptions:
    retry_failed: bool = False
    force: bool = False
    dry_run: bool = False


@dataclass
class JobSummary:
    tag: str
    downloaded: int = 0          # dry run: would download
    already: int = 0
    failed: int = 0
    previously_failed: int = 0
    gone: int = 0
    elapsed: float = 0.0
    dry_run: bool = False
    error: str | None = None     # the job itself failed (e.g. listing failed)
    disabled: bool = False
    interrupted: bool = False

    def count_rows(self) -> list[tuple[str, int]]:
        return [
            ("would download" if self.dry_run else "new downloaded", self.downloaded),
            ("already downloaded (skipped)", self.already),
            ("failed", self.failed),
            ("previously failed (skipped)", self.previously_failed),
            ("no longer in playlist", self.gone),
        ]


def emit(message: str = "") -> None:
    print(message, flush=True)


def display_path(path: Path) -> str:
    """Path relative to the current folder when it is inside it."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"


def format_size(size: int | None) -> str:
    if not size:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def target_dir(job: Job, listing: Listing) -> Path:
    if job.mode == "playlist" and job.settings["use_playlist_subfolder"] and listing.playlist_title:
        return job.tag_dir / sanitize_folder_name(listing.playlist_title)
    return job.tag_dir


def _init_worker(log_level: str) -> None:
    """Pool initializer. Workers ignore Ctrl+C: the main process catches it,
    saves what finished and stops the workers itself."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_logging(log_level)


def _stop_pool(pool: ProcessPoolExecutor) -> None:
    """Cancel queued downloads and stop running ones right away. yt-dlp keeps
    its .part files, so an interrupted video resumes on the next run."""
    terminate = getattr(pool, "terminate_workers", None)  # Python 3.14+
    if terminate is not None:
        terminate()  # also cancels queued work
        return
    # shutdown() forgets the worker processes, so take the list first.
    processes = list((getattr(pool, "_processes", None) or {}).values())
    pool.shutdown(wait=False, cancel_futures=True)
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=5)


def make_plan(job: Job, listing: Listing, store: MetadataStore, options: RunOptions) -> SyncPlan:
    s = job.settings
    playlist = job.mode == "playlist"
    order = s["order"] if playlist else "playlist"
    dates = dict(store.upload_dates)
    missing = entries_missing_dates(listing.entries, order, dates)
    if missing:
        log.info("Looking up upload dates for %d video(s) to sort by %s...", len(missing), order)
        found = backend.lookup_upload_dates(missing, s)
        dates.update(found)
        if not options.dry_run:
            store.upload_dates.update(found)
        if len(found) < len(missing):
            log.warning("%d video(s) have no known upload date; they sort last",
                        len(missing) - len(found))
    return plan_sync(
        listing.entries, store.job_entry(job.tag), job.format,
        output_dir=job.output_dir,
        count=job.count if playlist else None,
        order=order,
        upload_dates=dates,
        retry_failed=options.retry_failed or s["retry_failed"],
        force=options.force,
        verify_files=s["verify_files"],
        report_removed=playlist,
    )


def _describe_listing(job: Job, listing: Listing, plan: SyncPlan) -> None:
    if job.mode != "playlist":
        return
    title = listing.playlist_title or listing.playlist_id or job.url
    count = "all" if job.count is None else job.count
    emit(f"Playlist: {title} - {plan.listed} video(s) listed, order {job.settings['order']}, "
         f"count {count} -> {len(plan.selected)} selected")
    if listing.is_mix:
        emit(f"  (Mix playlist: YouTube changes it on every fetch; read the first "
             f"{job.settings['mix_limit']} entries)")
    if job.count is not None and job.count > plan.listed:
        emit(f"  (count {job.count} is more than the {plan.listed} video(s) available; taking all)")


def _print_plan_details(plan: SyncPlan, store: MetadataStore, job: Job, dry_run: bool) -> None:
    if dry_run:
        emit(f"Would download ({len(plan.download)}):")
        for entry, reason in plan.download:
            emit(f"  + {entry.label}  ({reason})")
        if plan.already:
            emit(f"Would skip, already downloaded ({len(plan.already)}):")
        for entry in plan.already:
            emit(f"  = {_known_label(entry, store, job)}")
    if plan.previously_failed:
        emit(f"Previously failed, skipped ({len(plan.previously_failed)}); use --retry-failed to retry:")
        for entry in plan.previously_failed:
            record = store.video_record(job.tag, entry.id, job.format) or {}
            emit(f"  ! {_known_label(entry, store, job)}: {record.get('last_error')}")
    if plan.gone:
        emit(f"No longer in playlist ({len(plan.gone)}), kept as they are:")
        for video_id, title in plan.gone:
            emit(f"  - {title or '(unknown title)'} [{video_id}]")


def _known_label(entry, store: MetadataStore, job: Job) -> str:
    if entry.title:
        return entry.label
    record = store.video_record(job.tag, entry.id, job.format) or {}
    return f"{record.get('title') or '(unknown title)'} [{entry.id}]"


def run_job(job: Job, store: MetadataStore, options: RunOptions, worker=None) -> JobSummary:
    """Sync one job. Never raises for download problems; a listing failure
    is reported in the summary. Ctrl+C sets summary.interrupted."""
    worker = worker or backend.download_worker
    started = time.monotonic()
    summary = JobSummary(tag=job.tag, dry_run=options.dry_run)
    emit(f"== {job.name} ({job.mode}, {job.format}){' [dry run]' if options.dry_run else ''} ==")

    try:
        listing = backend.fetch_listing(job)
    except backend.ListingError as exc:
        summary.error = f"could not list {job.url}: {exc}"
        hint = backend.blocked_hint(str(exc))
        log.error("[%s] %s%s", job.tag, summary.error, f" ({hint})" if hint else "")
        summary.elapsed = time.monotonic() - started
        return summary

    plan = make_plan(job, listing, store, options)
    _describe_listing(job, listing, plan)
    summary.already = len(plan.already)
    summary.previously_failed = len(plan.previously_failed)
    summary.gone = len(plan.gone)
    _print_plan_details(plan, store, job, options.dry_run)

    if options.dry_run:
        summary.downloaded = len(plan.download)
        if plan.up_to_date:
            emit(UP_TO_DATE)
    else:
        store.ensure_job(job)
        if plan.up_to_date:
            emit(UP_TO_DATE)
        else:
            _download(job, plan, listing, store, options, worker, summary)
        store.finish_job(job, listing.playlist_id, listing.playlist_title)
    summary.elapsed = time.monotonic() - started
    return summary


def _download(job: Job, plan: SyncPlan, listing: Listing, store: MetadataStore,
              options: RunOptions, worker, summary: JobSummary) -> None:
    folder = target_dir(job, listing)
    labels = {entry.id: entry.label for entry, _ in plan.download}
    tasks = [{
        "video_id": entry.id, "tag": job.tag, "title": entry.title, "force": options.force,
        "target_dir": str(folder), "output_dir": str(job.output_dir), "settings": job.settings,
    } for entry, _ in plan.download]
    total = len(tasks)
    workers = min(job.settings["max_workers"], total)
    emit(f"Downloading {total} video(s) with {workers} worker(s) into {display_path(folder)}")
    for entry, reason in plan.download:
        if reason != REASON_NEW:
            emit(f"  ({reason}) {entry.label}")

    pool = ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                               initargs=(job.settings["log_level"],))
    done = 0
    try:
        futures = {pool.submit(worker, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                result = future.result()
            except BrokenProcessPool:
                result = {"video_id": task["video_id"], "ok": False,
                          "error": "worker process crashed (killed or out of memory)"}
            except Exception as exc:  # noqa: BLE001 - a bad result must not stop the job
                result = {"video_id": task["video_id"], "ok": False, "error": backend.clean_error(exc)}
            done += 1
            if not result.get("title"):
                result["title"] = task["title"]  # failed before yt-dlp knew it; use the listing's
            record = store.record_result(job, result)  # flushed to disk right away
            label = f"{record.get('title') or labels[task['video_id']].rsplit(' [', 1)[0]} [{task['video_id']}]"
            if result["ok"]:
                summary.downloaded += 1
                emit(f"[{done}/{total}] downloaded  {label}  "
                     f"({format_size(result.get('file_size'))} in {result.get('elapsed', 0):.1f}s)")
            else:
                summary.failed += 1
                kept = " (previous file kept)" if record["status"] == STATUS_DOWNLOADED else ""
                emit(f"[{done}/{total}] FAILED      {label}: {result['error']}{kept}")
                hint = backend.blocked_hint(result["error"])
                if hint:
                    emit(f"              hint: {hint}")
    except KeyboardInterrupt:
        summary.interrupted = True
        emit(f"\nInterrupted: stopping workers. {done}/{total} finished video(s) are saved "
             f"in the metadata; the rest will be picked up on the next run.")
        _stop_pool(pool)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def print_job_summary(summary: JobSummary) -> None:
    emit(f"Summary for {summary.tag}:")
    if summary.disabled:
        emit("  skipped: job is disabled in the config")
        return
    if summary.error:
        emit(f"  job failed: {summary.error}")
    for label, value in summary.count_rows():
        emit(f"  {label + ':':<30} {value}")
    emit(f"  {'time:':<30} {format_duration(summary.elapsed)}")
    if summary.interrupted:
        emit("  (interrupted)")
    emit()


def print_overall_summary(summaries: list[JobSummary], elapsed: float, dry_run: bool) -> None:
    ran = [s for s in summaries if not s.disabled]
    emit(f"==== Overall summary: {len(ran)} job(s) run{' (dry run)' if dry_run else ''} ====")
    totals = {}
    for s in ran:
        for label, value in s.count_rows():
            totals[label] = totals.get(label, 0) + value
    for label, value in totals.items():
        emit(f"  {label + ':':<30} {value}")
    emit(f"  {'total time:':<30} {format_duration(elapsed)}")
    disabled = [s.tag for s in summaries if s.disabled]
    failed = [s.tag for s in summaries if s.error]
    if disabled:
        emit(f"  skipped (disabled): {', '.join(disabled)}")
    if failed:
        emit(f"  jobs that failed: {', '.join(failed)}")
    if any(s.interrupted for s in summaries):
        emit("  run was interrupted with Ctrl+C")


def run_jobs(jobs: list[Job], store: MetadataStore, options: RunOptions,
             worker=None) -> list[JobSummary]:
    """Run jobs in order. A failing job never stops the others; Ctrl+C stops all."""
    started = time.monotonic()
    summaries: list[JobSummary] = []
    base_level = logging.getLogger("ytdl").level
    for job in jobs:
        if not job.enabled:
            emit(f"== {job.name} == skipped: disabled in config")
            summaries.append(JobSummary(tag=job.tag, disabled=True))
            continue
        set_level(job.settings["log_level"])
        try:
            summary = run_job(job, store, options, worker)
        except KeyboardInterrupt:
            emit("\nInterrupted.")
            summary = JobSummary(tag=job.tag, dry_run=options.dry_run, interrupted=True)
        except Exception as exc:  # noqa: BLE001 - one broken job must not stop the rest
            log.exception("[%s] unexpected error", job.tag)
            summary = JobSummary(tag=job.tag, dry_run=options.dry_run, error=f"unexpected error: {exc}")
        finally:
            set_level(base_level or "INFO")
        summaries.append(summary)
        print_job_summary(summary)
        if summary.interrupted:
            break
    print_overall_summary(summaries, time.monotonic() - started, options.dry_run)
    return summaries
