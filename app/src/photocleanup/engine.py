"""Backend engine: a thin orchestration layer over the reused photo_cleanup
package. Mirrors the CLI's flows but returns plain dicts the service can
serialise, caches per-layer candidate payloads from an `analyze` pass, and keeps
an in-memory uuid->Record index for thumbnails.

No curation logic lives here — it all stays in photo_cleanup/.
"""
from __future__ import annotations

import io
import os
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Optional

from photo_cleanup.apply import KW_REVIEWED
from photo_cleanup.model import Config, Record

# Reuse the CLI's embedding cache so the app inherits already-computed vectors.
DEFAULT_EMB_CACHE = os.path.expanduser("~/.cache/photo-cleanup/embeddings.db")

# Heavy per-image work (Vision embeds/face passes, PIL decodes) runs in a small
# thread pool: pyobjc and PIL release the GIL during the actual work, so this
# is real parallelism, bounded so a background scan doesn't saturate the Mac.
MAX_WORKERS = min(8, (os.cpu_count() or 4))

ALL_LAYERS = ("dedup", "videos", "screenshots", "expired")
# Group-based layers (keep best of a set) vs flat layers (all flagged to remove).
GROUPED_LAYERS = ("dedup", "videos")


class AnalysisCancelled(Exception):
    """Raised inside analyze() when the user cancels the scan."""


def _filter_by_date(records, since: Optional[str], until: Optional[str]):
    if not since and not until:
        return records
    lo = datetime.strptime(since, "%Y-%m-%d").date() if since else None
    hi = datetime.strptime(until, "%Y-%m-%d").date() if until else None
    out = []
    for r in records:
        if r.timestamp is None:
            continue
        d = datetime.fromtimestamp(r.timestamp).date()
        if (lo is None or d >= lo) and (hi is None or d <= hi):
            out.append(r)
    return out


def _date_label(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%b %-d, %Y")
    except Exception:
        return ""


def _item_bytes(rec: Record) -> int:
    """Best-effort reclaimable size for a record (videos: real file; photos:
    original file, else a rough estimate from dimensions for iCloud-only assets)."""
    if rec.is_movie:
        from photo_cleanup.video import video_size
        return video_size(rec)
    if rec.path:
        try:
            return os.path.getsize(rec.path)
        except OSError:
            pass
    return int((rec.width or 0) * (rec.height or 0) * 0.20)  # ~HEIC bytes/pixel


class Engine:
    def __init__(self, cfg: Optional[Config] = None,
                 emb_cache: str = DEFAULT_EMB_CACHE,
                 dbpath: Optional[str] = None):
        self.cfg = cfg or Config()
        self.emb_cache = emb_cache
        self.dbpath = dbpath
        self._index: dict[str, Record] = {}
        self._candidates: dict[str, list] = {}   # layer -> payload (from analyze)
        # Scanned photo metadata (GPS/OCR/filenames) is held in RAM ONLY — never
        # written to disk (audit #8). Memoized per library mtime so repeat scans in
        # a session don't re-read osxphotos; auto-invalidated when the library changes.
        self._records_memo: Optional[list] = None
        self._records_mtime = None
        # Videos are memoized the same way (their own osxphotos pass is just as
        # costly a full-DB parse), so a rescan can reuse them too.
        self._videos_memo: Optional[list] = None
        self._videos_mtime = None
        # One lock guards _index + _candidates: they're mutated by the analyze
        # thread and read by request threads (thumbs, candidates) + the warmer.
        self._state_lock = threading.RLock()
        self._video_count: Optional[int] = None
        # Rendered thumbnails/previews live ONLY here, in RAM — never written to
        # disk. Bounded LRU (evicts oldest past the byte budget); cleared on quit.
        self._thumb_cache: "OrderedDict[tuple[str, int], bytes]" = OrderedDict()
        self._thumb_lock = threading.Lock()
        self._thumb_used = 0
        self._thumb_budget = 192 * 1024 * 1024
        self._warming = False
        self._cancel = threading.Event()   # set by request_cancel(), checked mid-scan

    # ---- record loading ----------------------------------------------------
    def _shared_db(self):
        """A lazy PhotosDB provider for one pass: the first REAL read constructs
        the (30–90s to parse) osxphotos.PhotosDB, every later read in the same
        pass reuses it — so photos + videos cost ONE Photos.sqlite parse, and a
        memo-served pass constructs nothing at all."""
        holder: list = []

        def provider():
            if not holder:
                import osxphotos
                holder.append(osxphotos.PhotosDB(self.dbpath) if self.dbpath
                              else osxphotos.PhotosDB())
            return holder[0]
        return provider

    def _all_records(self, force: bool = False, progress=None, db=None):
        """All photo Records via osxphotos, held in RAM ONLY (never persisted —
        the metadata includes GPS + OCR text; audit #8). Memoized per library
        mtime so repeat analyzes in a session don't re-scan; the memo invalidates
        itself automatically when the library changes. `progress(done, total)` is
        forwarded to the read loop (only fires on a real read, not a memo hit).
        `db` (a PhotosDB or lazy provider from _shared_db) is forwarded to
        scan_library so callers can share a single library parse."""
        from photo_cleanup.scan import _db_mtime, scan_library
        mt = _db_mtime(self.dbpath)
        if (not force and self._records_memo is not None
                and mt is not None and mt == self._records_mtime):
            return self._records_memo
        recs = scan_library(self.dbpath, progress=progress, db=db)
        self._records_memo, self._records_mtime = recs, mt
        return recs

    def _all_videos(self, force: bool = False, progress=None, db=None):
        """All video Records via osxphotos, RAM-only and memoized per library
        mtime — same contract as _all_records, so repeat/rescan passes don't
        re-parse the whole PhotosDB."""
        from photo_cleanup.scan import _db_mtime, scan_library
        mt = _db_mtime(self.dbpath)
        if (not force and self._videos_memo is not None
                and mt is not None and mt == self._videos_mtime):
            return self._videos_memo
        recs = scan_library(self.dbpath, movies_only=True, progress=progress, db=db)
        self._videos_memo, self._videos_mtime = recs, mt
        return recs

    def forget(self, uuids) -> None:
        """Drop just-deleted assets from the in-RAM state so the NEXT analyze
        re-clusters the survivors without re-reading the whole library via
        osxphotos. A delete only ever removes known uuids, so pruning the RAM
        memos is equivalent to — and far cheaper than — a fresh scan, and it
        persists NOTHING to disk (records stay RAM-only; audit #8 — the only
        disk touch is *deleting* the evicted vectors from the embedding cache).

        The delete just bumped the library mtime, so we adopt the new mtime for
        the pruned memos: the next analyze then sees them as current and skips
        scan_library(). If the library changed out-of-band afterwards, the mtime
        won't match on the following check and a full re-read happens anyway —
        correctness always wins over the fast path.
        """
        drop = {u for u in uuids if u}
        if not drop:
            return
        from photo_cleanup.scan import _db_mtime
        mt = _db_mtime(self.dbpath)
        with self._state_lock:
            if self._records_memo is not None:
                self._records_memo = [r for r in self._records_memo if r.uuid not in drop]
                self._records_mtime = mt
            if self._videos_memo is not None:
                before = len(self._videos_memo)
                self._videos_memo = [r for r in self._videos_memo if r.uuid not in drop]
                self._videos_mtime = mt
                if len(self._videos_memo) != before and self._video_count is not None:
                    self._video_count -= (before - len(self._videos_memo))
            for u in drop:
                self._index.pop(u, None)
        with self._thumb_lock:
            for key in [k for k in self._thumb_cache if k[0] in drop]:
                self._thumb_used -= len(self._thumb_cache.pop(key))
        # Evict the deleted assets' vectors (and derived video-frame keys) from
        # the on-disk embedding cache too — otherwise it grows forever. This
        # only DELETES derived data; no photo metadata is persisted (audit #8).
        # Best-effort: a cache hiccup must never break the delete flow.
        try:
            from photo_cleanup.embedding import EmbeddingCache
            EmbeddingCache(self.emb_cache).forget(drop)
        except Exception:  # noqa: BLE001 — cache eviction is opportunistic
            pass

    def load_records(self, since=None, until=None, excluded: Optional[set] = None,
                     force_rescan: bool = False, eligible_only: bool = True, progress=None,
                     db=None):
        """Photos in scope. `eligible_only` (default) drops the Hidden album, items
        the library already marks reviewed:keep, and any uuid in `excluded` — the set
        the curated scan should suggest on. Set it False for the manual feed, which
        shows everything in range except Hidden (incl. already-kept photos)."""
        excluded = excluded or set()
        recs = _filter_by_date(self._all_records(force=force_rescan, progress=progress, db=db),
                               since, until)
        if eligible_only:
            recs = [r for r in recs if not r.is_hidden
                    and KW_REVIEWED not in (r.keywords or []) and r.uuid not in excluded]
        else:
            recs = [r for r in recs if not r.is_hidden]
        # Drop stale cache entries whose local original vanished (photo purged from
        # the library since the cache was written). iCloud-only records (path=None)
        # can't be pre-checked and are kept — PhotoKit can still delete them.
        recs = [r for r in recs if not r.path or os.path.exists(r.path)]
        with self._state_lock:
            for r in recs:
                self._index[r.uuid] = r
        return recs

    def load_videos(self, since=None, until=None, excluded: Optional[set] = None,
                    eligible_only: bool = True, force_rescan: bool = False, progress=None,
                    db=None):
        """Videos in scope (same eligibility rules as load_records)."""
        excluded = excluded or set()
        recs = _filter_by_date(self._all_videos(force=force_rescan, progress=progress, db=db),
                               since, until)
        if eligible_only:
            recs = [r for r in recs if KW_REVIEWED not in (r.keywords or [])
                    and r.uuid not in excluded and r.path and os.path.exists(r.path)]
        else:
            recs = [r for r in recs if not r.is_hidden and r.path and os.path.exists(r.path)]
        with self._state_lock:
            for r in recs:
                self._index[r.uuid] = r
        return recs

    def record(self, uuid: str) -> Optional[Record]:
        with self._state_lock:
            return self._index.get(uuid)

    def cached_candidates(self, layer: str) -> Optional[list]:
        """The layer's payload from the last analyze, or None — never computes."""
        with self._state_lock:
            return self._candidates.get(layer)

    # ---- candidate builders (delegate to photo_cleanup) --------------------
    def dedup_groups(self, records, progress=None):
        from photo_cleanup.cluster import find_duplicate_groups, time_gps_clusters
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.feedback import inject_face_quality
        cand = [r for c in time_gps_clusters(records, self.cfg) if len(c) >= 2 for r in c]
        ec = EmbeddingCache(self.emb_cache)
        emb_prog = (lambda i, n: progress("Comparing photos", i, n)) if progress else None
        if embed_records(cand, ec, progress=emb_prog, workers=MAX_WORKERS):
            ec.save()
        inject_face_quality(cand)
        return find_duplicate_groups(records, self.cfg, embeddings=ec)

    def video_groups(self, videos, progress=None):
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.video import duplicate_takes
        ec = EmbeddingCache(self.emb_cache)
        emb_prog = (lambda i, n: progress("Comparing videos", i, n)) if progress else None
        embed_records(videos, ec, progress=emb_prog, workers=MAX_WORKERS)
        groups = duplicate_takes(videos, ec, self.cfg, workers=MAX_WORKERS)
        ec.save()          # persists poster + sampled-frame vectors (no-op if clean)
        return groups

    def screenshot_items(self, records):
        from photo_cleanup.screenshots import classify_screenshot
        return [(r, v) for r in records
                if (v := classify_screenshot(r, self.cfg)).is_work]

    def expired_items(self, records):
        from photo_cleanup.expired import classify_expired
        return [(r, v) for r in records
                if (v := classify_expired(r, self.cfg)).is_expired]

    # ---- serialisation -----------------------------------------------------
    def photo_dict(self, rec: Record, *, suggested_keep: bool, subtitle: str = "") -> dict:
        from photo_cleanup.quality import keeper_score
        b = _item_bytes(rec)
        return {
            "uuid": rec.uuid,
            "filename": rec.original_filename,
            "width": rec.width,
            "height": rec.height,
            "favorite": rec.favorite,
            "is_video": rec.is_movie,
            "duration": rec.duration,
            "suggested_keep": suggested_keep,
            "score": round(keeper_score(rec, self.cfg), 3),
            "focus": rec.laplacian,
            "timestamp": rec.timestamp,
            "subtitle": subtitle,
            "bytes": b,
            "size_mb": round(b / (1024 * 1024), 1),
            "thumb": f"/api/thumb/{rec.uuid}",
        }

    def _group_payload(self, groups) -> list[dict]:
        """Shared shape for grouped layers (dedup, videos)."""
        out = []
        for g in sorted(groups, key=lambda g: g.size, reverse=True):
            # Show the whole burst in capture order — keepers aren't floated to the
            # front, just marked — so neighbouring frames sit side by side.
            members = sorted(g.keepers + g.discards, key=lambda r: r.timestamp or 0)
            keep_ids = {r.uuid for r in g.keepers}
            ts = min((r.timestamp for r in members if r.timestamp), default=None)
            out.append({
                "group_key": min((r.uuid for r in members), default=""),
                "title": _date_label(ts) or "Group",
                "date_label": _date_label(ts),
                "size": g.size,
                "suggested_keep": len(g.keepers),
                "suggested_discard": len(g.discards),
                "photos": [self.photo_dict(r, suggested_keep=r.uuid in keep_ids)
                           for r in members],
            })
        return out

    dedup_payload = _group_payload  # alias: same shape
    video_payload = _group_payload

    def _flat_payload(self, items, key: str, title: str, snippet_len: int) -> list[dict]:
        """Shared shape for flat layers (screenshots, expired): all flagged remove."""
        photos = []
        for rec, verdict in items:
            snippet = (rec.detected_text or "").strip().replace("\n", " ")[:snippet_len]
            sub = " · ".join(verdict.reasons) + (f" — “{snippet}…”" if snippet else "")
            p = self.photo_dict(rec, suggested_keep=False, subtitle=sub)
            p["kind"] = getattr(verdict, "kind", "") or "generic"   # for the learning loop
            photos.append(p)
        if not photos:
            return []
        return [{"group_key": key, "title": title, "date_label": "",
                 "size": len(photos), "suggested_keep": 0,
                 "suggested_discard": len(photos), "photos": photos}]

    def screenshot_payload(self, items):
        return self._flat_payload(items, "screenshots", "Work screenshots", 120)

    def expired_payload(self, items):
        return self._flat_payload(items, "expired", "Expired utility photos", 90)

    # ---- analyze (the heavy pass) + summary --------------------------------
    def reset_state(self) -> None:
        """Drop the per-scan candidate payloads and record index."""
        with self._state_lock:
            self._index = {}
            self._candidates = {}

    def analyze(self, since=None, until=None, layers=None, excluded: Optional[set] = None,
                progress=None, force: bool = False):
        """Compute candidates for each requested layer, cache the payloads, and
        return a per-layer summary (counts + reclaimable bytes) for the picker.

        Starts from a clean slate and rolls back to one on failure, so an aborted
        scan can never leave stale candidates/records for the UI to act on.

        `force=True` re-reads the whole library via osxphotos instead of reusing
        the in-RAM records memo — used by the explicit "Re-scan" action. The
        implicit refreshes (after a review, on resume) leave it False so they
        reuse the pruned memo and stay fast.

        `progress(message, done, total)` is called throughout so the UI can show
        the access request, library connection, and counted/total processing.
        """
        self.reset_state()
        self._cancel.clear()
        try:
            return self._analyze(since, until, layers, excluded, progress, force)
        except BaseException:
            self.reset_state()
            raise

    def request_cancel(self) -> None:
        """Ask a running analyze to stop at the next checkpoint. The scan thread
        raises AnalysisCancelled and rolls back to a clean slate."""
        self._cancel.set()

    def _analyze(self, since=None, until=None, layers=None, excluded: Optional[set] = None,
                 progress=None, force: bool = False):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from photo_cleanup.cluster import find_duplicate_groups, time_gps_clusters
        from photo_cleanup.embedding import (EmbeddingCache, _safe_vector, embed_records,
                                             feature_print_and_face_quality)
        from photo_cleanup.expired import classify_expired
        from photo_cleanup.feedback import (face_capture_quality, face_quality_fresh,
                                            inject_face_quality, load_face_cache,
                                            save_face_cache, set_face_quality)
        from photo_cleanup.quality import _best_image_path, measure_sharpness
        from photo_cleanup.screenshots import classify_screenshot
        from photo_cleanup.video import duplicate_takes

        layers = [l for l in (layers or ALL_LAYERS) if l in ALL_LAYERS]
        excluded = excluded or set()
        photo_layers = [l for l in layers if l in ("dedup", "screenshots", "expired")]

        # ---- progress: ONE monotonic bar. The read + look-alike preamble owns the
        #      first READ_FRAC of the bar; the cost-weighted compute phases own the
        #      rest (remapped in emit_phase). Every phase reports a numeric frac so
        #      the bar always advances — no static "pulse" states.
        READ_FRAC = 0.30

        def check_cancel():
            if self._cancel.is_set():
                raise AnalysisCancelled()

        def emit_read(label, r, done=None, total=None):
            """Report progress within the read preamble: r in [0,1] maps to the
            first READ_FRAC of the bar. Also a cancellation checkpoint."""
            check_cancel()
            if progress:
                progress(label, done, total, min(READ_FRAC * max(0.0, min(r, 1.0)), READ_FRAC))

        # 1) privileges — deletion needs Photos read-write. The prompt is fired on the
        #    app's MAIN thread at launch (a background-thread request can't show the
        #    dialog and gets recorded as a denial); here we only CHECK it.
        emit_read("Checking photo access…", 0.02)
        auth_status = None
        try:
            from .delete import authorization_status, is_authorized
            authorized = is_authorized()
            if not authorized:
                auth_status = authorization_status()   # 0=undecided 1=restricted 2=denied
        except (ImportError, ModuleNotFoundError):
            # PhotoKit genuinely unavailable (e.g. tests / non-macOS) → don't block.
            # A narrow catch so a real authorization error surfaces instead of
            # silently pretending access is granted (audit #22).
            authorized = True
        if not authorized:
            raise PermissionError(f"photos-access (status={auth_status})")

        # 2) read the scope. The PhotosDB parse itself is opaque (the frontend
        #    creeps the bar through the "Opening…" step); the per-record build IS
        #    counted, so photos and videos each report live x/y. The counted read
        #    starts at r≈0.21 — just above where the frontend creep tops out during
        #    the opaque parse — so the two hand off without a visible stall.
        #    Read-budget segments: photos [0.21, 0.72], videos [0.72, 0.86],
        #    look-alike pass [0.88, 1.0].
        dedup_on, videos_on = "dedup" in layers, "videos" in layers
        emit_read("Opening your photo library…", 0.04)
        # One lazy PhotosDB for the whole pass: the photo read constructs it (a
        # 30–90s Photos.sqlite parse), the video read reuses it — the parse used
        # to happen TWICE. Memo-served loads never construct it at all.
        shared_db = self._shared_db()
        photos = []
        if photo_layers:
            photos = self.load_records(
                since, until, excluded=excluded, force_rescan=force, db=shared_db,
                progress=lambda i, n: emit_read("Reading photos…", 0.21 + 0.51 * (i / n if n else 1.0), i, n))
        videos = []
        if videos_on:
            emit_read("Reading videos…", 0.72)
            videos = self.load_videos(
                since, until, excluded=excluded, force_rescan=force, db=shared_db,
                progress=lambda i, n: emit_read("Reading videos…", 0.72 + 0.14 * (i / n if n else 1.0), i, n))
        nphotos, nvideos = len(photos), len(videos)

        # dedup candidates (photos in a multi-shot time/GPS cluster)
        ec = EmbeddingCache(self.emb_cache)
        cand = set()
        if dedup_on:
            emit_read("Finding look-alikes…", 0.88)
            for c in time_gps_clusters(photos, self.cfg):
                if len(c) >= 2:
                    cand.update(r.uuid for r in c)
            emit_read("Finding look-alikes…", 0.99)
        cand_n = len(cand)

        # Phase cost model (≈ wall-clock). The heavy per-candidate image work — ONE
        # merged Vision pass (feature print + face-capture quality on a single
        # decode) plus the sharpness decode — dominates and all lives in the photo
        # phase now, so weight by candidates, not raw photo count. The face phase
        # became a cache-hit sweep (qualities were computed alongside the embeds)
        # and grouping is vector-only — both near-zero.
        EMBED_W = 6        # a dedup candidate: merged Vision decode (embed + face) + sharpness decode
        photo_cost = (nphotos + cand_n * (EMBED_W - 1)) if photo_layers else 0
        face_cost = max(1.0, cand_n * 0.1) if dedup_on else 0    # cache-hit sweep (no image decode)
        group_cost = max(1.0, cand_n * 0.1) if dedup_on else 0   # vector-only now (no image decode)
        video_cost = (nvideos * 4) if videos_on else 0
        takes_cost = max(1.0, nvideos * 0.4) if videos_on else 0
        total_cost = (photo_cost + face_cost + group_cost + video_cost + takes_cost) or 1.0
        done_frac = 0.0   # bar fraction filled by completed COMPUTE phases (0..1 internally)

        def emit_phase(label, cost, frac_done, frac_total, count_done=None, count_total=None):
            """Overall bar = READ_FRAC (read preamble) + finished compute phases +
            this phase's share × its sub-progress, all compressed into the compute
            band [READ_FRAC, 1]. `count_*` drive the on-screen 'x / y'. Doubles as
            the cancellation checkpoint for the heavy passes (faces, grouping, takes)."""
            check_cancel()
            if not progress:
                return
            f = done_frac + (cost / total_cost) * (min(frac_done, frac_total) / frac_total if frac_total else 1.0)
            reported = READ_FRAC + (1.0 - READ_FRAC) * min(f, 1.0)
            progress(label,
                     int(min(count_done, count_total)) if count_total else None,
                     count_total or None,
                     min(reported, 0.999))

        # 3) per-photo pass: classify + the heavy per-candidate image work. The
        #    bar tracks decode work (candidates are heavier); the count shows
        #    photos. Candidates run through a thread pool — Vision and PIL both
        #    release the GIL, so up to MAX_WORKERS images decode in parallel.
        #    Workers ONLY compute; this thread applies every cache write and
        #    progress tick, so ticks stay monotonic and the caches/records never
        #    see concurrent mutation from the coordinator side.
        shots, exp, cand_recs = [], [], []
        photo_work = 0.0
        photos_done = 0
        face_cache = load_face_cache() if dedup_on else {}
        faces_computed = 0

        def tick_photo(weight):
            nonlocal photo_work, photos_done
            photos_done += 1
            photo_work += weight
            if photos_done % 50 == 0 or photos_done == nphotos:
                emit_phase("Analyzing photos…", photo_cost, photo_work, photo_cost or 1,
                           photos_done, nphotos)

        def candidate_job(rec, path, need_embed, need_face):
            """Worker thread: pure per-image compute, no shared state. Where BOTH
            the feature print and face quality are stale, ONE merged Vision pass
            computes them on a single image decode (instead of two)."""
            if self._cancel.is_set():        # scan is being torn down — skip the decode
                return rec, path, None, None
            vec = fq = None
            if need_embed and need_face:
                vec, fq = feature_print_and_face_quality(path)
            elif need_embed:
                vec = _safe_vector(path)
            elif need_face:
                fq = face_capture_quality(path)
            measure_sharpness(rec)   # decode + Laplacian NOW, in this counted phase,
            return rec, path, vec, fq   # so "Grouping photoshoots…" does no image work

        pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            pending = []
            for rec in photos:
                check_cancel()
                if "screenshots" in layers and (sv := classify_screenshot(rec, self.cfg)).is_work:
                    shots.append((rec, sv))
                if "expired" in layers and (ev := classify_expired(rec, self.cfg)).is_expired:
                    exp.append((rec, ev))
                if rec.uuid not in cand:
                    tick_photo(1)
                    continue
                cand_recs.append(rec)
                path = _best_image_path(rec)
                need_embed = bool(path) and not ec.is_fresh(rec.uuid, path)
                need_face = bool(path) and not face_quality_fresh(face_cache, rec.uuid, path)
                if need_embed or need_face or rec.laplacian is None:
                    pending.append(pool.submit(candidate_job, rec, path, need_embed, need_face))
                else:
                    tick_photo(EMBED_W)          # fully cached — no decode at all
            for fut in as_completed(pending):
                check_cancel()
                crec, cpath, vec, fq = fut.result()
                if vec is not None:
                    ec.set(crec.uuid, vec, cpath)          # records mtime like compute()
                if fq is not None:
                    set_face_quality(face_cache, crec.uuid, cpath, fq)
                    faces_computed += 1
                tick_photo(EMBED_W)
        finally:
            # On cancel/failure, drop queued decodes instead of draining the whole
            # backlog; in-flight ones finish in the background and are discarded.
            pool.shutdown(wait=False, cancel_futures=True)
        if dedup_on:
            ec.save()
            if faces_computed:
                # Persist now so inject_face_quality (next phase) finds every
                # candidate fresh and does no image work of its own.
                save_face_cache(face_cache)
        done_frac += photo_cost / total_cost

        # 4) photo post-passes: face quality (Vision) then grouping
        if dedup_on:
            emit_phase("Detecting faces…", face_cost, 0, 1, 0, cand_n or 1)
            inject_face_quality(cand_recs, progress=lambda i, n: emit_phase("Detecting faces…", face_cost, i, n, i, n))
            done_frac += face_cost / total_cost
            emit_phase("Grouping photoshoots…", group_cost, 0, 1)
            groups = find_duplicate_groups(photos, self.cfg, embeddings=ec,
                                           progress=lambda i, n: emit_phase("Grouping photoshoots…", group_cost, i, n))
            done_frac += group_cost / total_cost
            with self._state_lock:
                self._candidates["dedup"] = self.dedup_payload(groups)
        with self._state_lock:
            if "screenshots" in layers:
                self._candidates["screenshots"] = self.screenshot_payload(shots)
            if "expired" in layers:
                self._candidates["expired"] = self.expired_payload(exp)

        # 5) videos: embed poster frames (the same worker-pool pattern lives
        #    inside embed_records — cache writes stay on this thread), then group
        #    same-scene takes with pooled frame sampling. The count during the
        #    embed tracks stale posters (cache hits are free and skipped).
        if videos_on:
            emit_phase("Analyzing videos…", video_cost, 0, 1, 0, nvideos)
            embed_records(videos, ec, workers=MAX_WORKERS,
                          progress=lambda i, n: emit_phase("Analyzing videos…", video_cost, i, n, i, n))
            emit_phase("Analyzing videos…", video_cost, 1, 1, nvideos, nvideos)
            ec.save()
            done_frac += video_cost / total_cost
            emit_phase("Comparing video takes…", takes_cost, 0, 1)
            vgroups = duplicate_takes(videos, ec, self.cfg, workers=MAX_WORKERS,
                                      progress=lambda i, n: emit_phase("Comparing video takes…", takes_cost, i, n))
            ec.save()      # sampled-frame vectors computed during takes (no-op if clean)
            done_frac += takes_cost / total_cost
            with self._state_lock:
                self._candidates["videos"] = self.video_payload(vgroups)

        if progress:
            progress("Finishing up…", None, None, 1.0)
        with self._state_lock:
            return {"since": since, "until": until,
                    "summary": {l: self._summarize(self._candidates.get(l, []), grouped=(l in ("dedup", "videos")))
                                for l in layers}}

    @staticmethod
    def _summarize(payload, grouped: bool = True) -> dict:
        """Counts + reclaimable bytes for the picker, plus a per-month histogram
        (`months`) the UI uses to filter by time period without a re-scan.

        Grouped layers (dedup/videos) bucket a whole cluster into its date; flat
        layers (screenshots/expired) bucket each item by its own date."""
        removable = [p for g in payload for p in g["photos"] if not p["suggested_keep"]]
        months: dict[str, dict] = {}

        def bucket(m, items, rbytes, groups):
            if not m:
                return
            e = months.setdefault(m, {"items": 0, "bytes": 0, "groups": 0})
            e["items"] += items
            e["bytes"] += rbytes
            e["groups"] += groups

        def month_of(ts):
            return datetime.fromtimestamp(ts).strftime("%Y-%m") if ts else None

        if grouped:
            for g in payload:
                ts = min((p["timestamp"] for p in g["photos"] if p.get("timestamp")), default=None)
                rbytes = sum(p["bytes"] for p in g["photos"] if not p["suggested_keep"])
                bucket(month_of(ts), len(g["photos"]), rbytes, 1)
        else:
            for p in (q for g in payload for q in g["photos"]):
                rbytes = p["bytes"] if not p["suggested_keep"] else 0
                bucket(month_of(p.get("timestamp")), 1, rbytes, 0)

        return {
            "groups": len(payload),
            "items": sum(len(g["photos"]) for g in payload),
            "removable": len(removable),
            "reclaimable_bytes": sum(p["bytes"] for p in removable),
            "months": [{"m": k, **v} for k, v in sorted(months.items())],
        }

    def candidates(self, layer, since=None, until=None, excluded: Optional[set] = None):
        """Return cached candidates for a layer; compute that one layer if the
        analyze pass didn't cover it."""
        with self._state_lock:
            if layer in self._candidates:
                return self._candidates[layer]
        excluded = excluded or set()
        if layer == "videos":
            pl = self.video_payload(self.video_groups(self.load_videos(since, until, excluded)))
        else:
            photos = self.load_records(since, until, excluded=excluded)
            if layer == "dedup":
                pl = self.dedup_payload(self.dedup_groups(photos))
            elif layer == "screenshots":
                pl = self.screenshot_payload(self.screenshot_items(photos))
            elif layer == "expired":
                pl = self.expired_payload(self.expired_items(photos))
            else:
                raise ValueError(f"unknown layer {layer!r}")
        with self._state_lock:
            self._candidates[layer] = pl
        return pl

    def all_items(self, since=None, until=None, offset: int = 0,
                  limit: Optional[int] = None) -> list[dict]:
        """Every photo + video in range as one chronological feed, all suggested to
        keep — for the manual review flow. Shows everything except Hidden, including
        items already marked reviewed:keep (unlike the curated scan).

        `offset`/`limit` page the feed so a 50k-item library isn't serialized into
        one giant payload; omitting them returns everything. The sort key is
        (timestamp, uuid) so page windows are stable across requests, and only the
        requested window pays the photo_dict cost. `size` stays the TOTAL count
        (with a `total` alias) so callers can tell how much remains."""
        recs = self.load_records(since, until, eligible_only=False) + \
               self.load_videos(since, until, eligible_only=False)
        recs.sort(key=lambda r: (r.timestamp or 0, r.uuid))
        total = len(recs)
        window = recs[offset:offset + limit] if limit is not None else recs[offset:]
        photos = [self.photo_dict(r, suggested_keep=True) for r in window]
        return [{"group_key": "all", "title": "All photos & videos", "date_label": "",
                 "size": total, "total": total, "suggested_keep": total,
                 "suggested_discard": 0, "photos": photos}]

    def library_stats(self) -> dict:
        """Cheap-ish library totals for the status line (videos counted once).
        Served from the RAM memos when warm; a cold call shares ONE PhotosDB
        parse between the photo and video reads."""
        db = self._shared_db()
        photos = len(self._all_records(db=db))
        if self._video_count is None:
            self._video_count = len(self._all_videos(db=db))
        return {"photos": photos, "videos": self._video_count}

    # ---- thumbnails --------------------------------------------------------
    def thumb_bytes(self, uuid: str, px: int = 240) -> Optional[bytes]:
        """Rendered bytes for a uuid at a target size, served from an in-memory
        LRU so a given thumb/preview is only ever rendered once. Nothing is
        written to disk — the cache lives in RAM and dies with the process."""
        key = (uuid, px)
        with self._thumb_lock:
            hit = self._thumb_cache.get(key)
            if hit is not None:
                self._thumb_cache.move_to_end(key)
                return hit
        data = self._render_thumb(uuid, px)   # render outside the lock (PIL is slow)
        if data is not None:
            with self._thumb_lock:
                if key not in self._thumb_cache:
                    self._thumb_cache[key] = data
                    self._thumb_used += len(data)
                    while self._thumb_used > self._thumb_budget and len(self._thumb_cache) > 1:
                        _, old = self._thumb_cache.popitem(last=False)
                        self._thumb_used -= len(old)
        return data

    def _render_thumb(self, uuid: str, px: int) -> Optional[bytes]:
        rec = self.record(uuid)
        if rec is None:
            return None
        try:
            from PIL import Image, ImageOps
        except Exception:
            return None
        candidates = list(rec.derivatives)
        # For an EDITED item the full-res source must be the edited render, NOT the
        # original master: Photos' derivatives are already the cropped/adjusted
        # version, so adding the uncropped original would make the large-px preview
        # pick it and show the original composited over the cropped thumb.
        full = rec.path_edited if (rec.has_adjustments and rec.path_edited) else rec.path
        if full:
            candidates.append(full)
        # Grid thumbs (small px) take the smallest source — fastest. The detail
        # preview (large px) takes the largest source so fine differences between
        # near-identical shots actually survive; `thumbnail()` only downscales, so a
        # small source could never produce a sharp preview. Higher JPEG quality too.
        hi = px > 512
        order = sorted(set(candidates), key=_size, reverse=hi)
        quality = 90 if hi else 72
        for p in order:
            try:
                with Image.open(p) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")  # honour camera rotation
                    im.thumbnail((px, px))
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=quality)
                return buf.getvalue()
            except Exception:
                continue
        return None

    def warm_thumbnails(self, px: int = 240) -> None:
        """Pre-render grid thumbs for every current candidate into the RAM cache,
        so Review scrolls instantly (like Photos' pre-baked thumbnails). Runs in a
        background thread; one at a time so it never blocks request threads for
        long. Safe to call repeatedly — already-cached items are skipped."""
        if self._warming:
            return
        self._warming = True
        try:
            seen = set()
            with self._state_lock:                       # snapshot; analyze may swap it
                payloads = list(self._candidates.values())
            for payload in payloads:
                for g in payload:
                    for p in g["photos"]:
                        u = p["uuid"]
                        if u in seen:
                            continue
                        seen.add(u)
                        with self._thumb_lock:
                            if (u, px) in self._thumb_cache:
                                continue
                        self.thumb_bytes(u, px=px)
        finally:
            self._warming = False


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 1 << 62
