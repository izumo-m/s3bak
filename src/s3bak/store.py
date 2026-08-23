# Requires Python 3.10+
"""The S3 backend: a thin wrapper over the boto3-s3 library.

Everything reaches S3 through boto3-s3 in-process: the ``S3`` orchestrator
drives cp / sync / ls, and every S3-side location is handed to the library as
an ``S3Storage`` bound to one shared boto3 client (see ``_s3_loc``), which is
also what the single-key head / get / put and the batched delete are
addressed through. That client is used directly only by ``iter_objects``,
whose paginated listing carries the ordering check verify's merge-join needs.
The orchestrator and the client are built up front, client construction not
being thread-safe.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from s3bak.console import console

if TYPE_CHECKING:
    from boto3_s3 import (
        FileFilter,
        FileInfo,
        LocalFileGenerator,
        MergedPairFilter,
        OpResult,
        PairFilter,
        ResultCallback,
        S3Storage,
    )
    from boto3_s3.etagcompare import EtagComparison


@dataclass
class ObjectMeta:
    """Subset of S3 head-object / list-objects response that callers use."""

    key: str
    size: int = 0
    etag: str | None = None  # dequoted S3 ETag
    storage_class: str | None = None  # None = STANDARD (S3 omits it on head)


@dataclass
class TransferResult:
    """Result of a sync/copy operation. The operation's own output (upload /
    download / delete lines, failures) is printed as it happens - see
    `Boto3S3Store._transfer` - so this carries only the outcome: `returncode`
    (0/1), and `results`, the count of SUCCEEDED/DRYRUN lines printed."""

    returncode: int
    results: int = 0


class Boto3S3Store:
    """Object storage backend for s3bak, built on the boto3-s3 library.

    Transfers (cp / sync) and listing go through the boto3-s3 ``S3`` API
    in-process; head-object / list-objects-v2 use the underlying boto3 client.
    Endpoint and credentials come from the AWS environment/profile, so the
    MinIO dev profile and real AWS both work without special-casing.

    `rel_key` is a path relative to the configured prefix (e.g. "bin",
    "bin/foo.txt", "bin-manifest.jsonl"). The store internally prepends
    `path_prefix` for boto3 calls and `prefix` (the s3:// URL) for cp / sync.
    """

    def __init__(
        self,
        profile: str,
        prefix: str,
        bucket: str,
        path_prefix: str,
        *,
        max_concurrency: int | None = None,
    ):
        self.profile = profile
        self.prefix = prefix  # full s3:// URL
        self.bucket = bucket
        self.path_prefix = path_prefix
        # Concurrency knob (None = library default): max_concurrency tunes the
        # transfer thread pool (cp / sync) and sizes verify's hashing pool.
        self.max_concurrency = max_concurrency

        # Build the S3 orchestrator and ONE boto3 client up front, on whatever
        # thread constructs the store - always sequentially (client
        # construction is not thread-safe; cli.run_entries builds all worker
        # stores on the main thread before the pool starts). Every S3-side
        # location is handed to the library as an S3Storage bound to this one
        # client (see _s3_loc), so no client is ever built lazily on a worker
        # thread; head_object shares it too. boto3-s3's contract: a client
        # must not be shared across concurrently transferring threads, so one
        # store serves one entry at a time (s3transfer's own worker threads
        # under a single transfer are the library's business).
        from boto3_s3 import S3, TransferConfig, session

        # One TransferConfig always, set max_concurrency or not: it is the
        # default for every cp / sync, and _multipart reads multipart_threshold
        # back off it. A None argument is dropped rather
        # than forwarded, so an unset max_concurrency still lands on the
        # library's own default (boto3's 10 - ~/.aws/config is never read for
        # it). Handing S3 a None instead would take its separate no-config
        # branch, which need not resolve to these same values.
        self._transfer_config = TransferConfig(max_concurrency=max_concurrency)
        # boto3_s3.session is a boto3.Session whose clients parse response
        # timestamps at C speed - the listings that drive sync and verify are
        # severalfold faster on large trees.
        self._s3 = S3(
            session=session(profile_name=profile),
            transfer_config=self._transfer_config,
        )
        self._client = self._s3.client()

    def clone(self) -> Boto3S3Store:
        """A fresh store - its own boto3-s3 orchestrator and client - with this
        store's configuration. Entry workers each get one (cli.run_entries):
        boto3-s3's contract is one client per concurrently transferring
        thread, built sequentially up front - never shared across transfers,
        never built on a worker thread."""
        return Boto3S3Store(
            self.profile,
            self.prefix,
            self.bucket,
            self.path_prefix,
            max_concurrency=self.max_concurrency,
        )

    # --- internal ----------------------------------------------------------
    def _s3_loc(self, rel_key: str = "", *, is_dir: bool = False) -> S3Storage:
        """An ``s3://`` location for ``rel_key`` as an ``S3Storage`` bound to
        this store's one shared client.

        Passing an ``S3Storage`` (not a bare URL string) makes cp / sync / ls
        reuse the pre-built client: ``S3.resolve`` returns a ``Storage``
        unchanged, whereas a bare ``s3://`` string is resolved by building a
        fresh client per call - the thread-unsafe path this exists to avoid.
        ``is_dir`` appends the trailing ``/`` a directory sync expects.
        """
        from boto3_s3 import S3Storage

        url = self._s3_url(rel_key)
        if is_dir:
            url += "/"
        return S3Storage(url, client=self._client)

    def content_compare(self) -> EtagComparison:
        """The `--checksum` update-lane strategy: ETag content comparison.

        The bare `EtagComparison` PairFilter, run inline on the sync's own
        thread: push's journal emission needs every lane decision serial and
        in ascending key order (docs/journal.md), so no filter is wrapped in
        `ParallelFilter` - pull's checksum compare runs serially too. It
        copies a pair only when the S3 ETag differs from the
        local file's reconstructed ETag, so a same-size, same-mtime content
        change is still transferred and an mtime-only drift is not; it reads
        and hashes every candidate file locally, which is why it is opt-in
        (the default is the stat-only compare). `part_size` is read from the
        same profile the uploads use, so multipart ETags reconstruct to a
        matching value.
        """
        from boto3_s3.etagcompare import EtagComparison

        return EtagComparison(self._s3)

    def compare_pool_size(self) -> int:
        """Worker count for verify's local hashing pool: the transfer
        `max_concurrency`, else boto3's default of 10."""
        return self.max_concurrency or 10

    def _api_key(self, rel_key: str) -> str:
        return f"{self.path_prefix}/{rel_key}" if self.path_prefix else rel_key

    def _s3_url(self, rel_key: str = "") -> str:
        return f"{self.prefix}/{rel_key}" if rel_key else self.prefix

    def _multipart(self, size: int) -> bool:
        """Whether an object of ``size`` takes S3.cp's multipart engine rather
        than the single-request lane (``S3Storage.get_file`` / ``put_file``).

        s3transfer's own ``multipart_threshold``, and nothing but a
        performance split: the single-request lane is one correct GetObject /
        PutObject at any size, so where the line falls decides only whether
        the transfer is parallelized.
        """
        return size >= self._transfer_config.multipart_threshold

    def _transfer(
        self, verbose: bool, label: str, op: Callable[[ResultCallback], None]
    ) -> TransferResult:
        """Run a boto3-s3 transfer op, printing aws-style result lines as they
        arrive.

        `op(on_result)` calls the S3 method with the given result callback;
        SUCCEEDED/DRYRUN items print immediately as 'upload:'/'download:'/
        'delete:' stdout lines, failures print immediately to stderr, and any
        boto3-s3 error also prints to stderr and sets returncode 1.
        The console serializes its writes by line, so printing straight from
        on_result - which runs on s3transfer worker threads - is safe; a line
        arriving while an interactive --delete question is on screen simply
        waits until the answer is in (`console.Console.prompt`). `results`
        (the SUCCEEDED/DRYRUN count) still needs its own lock since the
        increment itself is not.

        s3transfer calls on_result as a transfer's "done" callback and, in its
        own futures.py (`_run_callback`), catches any exception the callback
        raises and only logs it at debug level - it never propagates past
        s3transfer. A closed stdout (``s3bak push data | head``) makes
        console.out raise BrokenPipeError, which would otherwise vanish
        right there: the documented exit 141 would silently become 0, and
        the transfer would look complete (post_hook would even run) though
        nothing after the closed pipe was ever reported. So on_result catches
        BrokenPipeError itself, remembers it in `broken_pipe`, and this
        method re-raises it once `op` returns - after the transfer/delete
        engine has actually finished, same as the pre-streaming code (which
        raised BrokenPipeError from its post-sync print, once `op` had
        already completed) so a concurrent Boto3S3Error never masks it.
        """
        from boto3_s3 import Boto3S3Error, OpOutcome, TransferType

        if verbose:
            console.diag(f"+ (boto3-s3) {label}\n")
        results = 0
        broken_pipe = False
        lock = threading.Lock()

        def on_result(r: OpResult) -> None:
            nonlocal results, broken_pipe
            try:
                if r.outcome in (OpOutcome.SUCCEEDED, OpOutcome.DRYRUN):
                    pre = "(dry-run) " if r.outcome is OpOutcome.DRYRUN else ""
                    if r.transfer_type is TransferType.DELETE:
                        # A delete record's src is the display endpoint
                        # (s3://bucket/key), matching aws-cli's delete line.
                        line = f"{pre}delete: {r.src}"
                    elif r.src is not None and r.dest is not None:
                        line = f"{pre}{r.transfer_type.value}: {r.src} to {r.dest}"
                    else:
                        return
                    # Count before printing: the transfer already happened, so
                    # the tally must stand even if the print below is what
                    # discovers the broken pipe.
                    with lock:
                        results += 1
                    console.out(f"{line}\n")
                elif r.outcome is OpOutcome.FAILED:
                    # A delete names the display endpoint its success line
                    # would have named; every other transfer type names the
                    # relative compare key, as the sync's own lines do.
                    where = r.src if r.transfer_type is TransferType.DELETE else r.compare_key
                    console.diag(f"{where}: {r.error}\n")
                elif r.outcome is OpOutcome.WARNED:
                    console.warn(
                        f"warning: {r.error}" if r.error else f"warning: skipped {r.compare_key}"
                    )
                elif r.outcome is OpOutcome.NOTICE:
                    if r.error:
                        console.diag(f"{r.error}\n")
                # CANCELLED (a fatal elsewhere revoked the item) is dropped,
                # like aws-cli, which omits cancelled items from its output;
                # the fatal itself surfaces through the Boto3S3Error the
                # operation raises.
            except BrokenPipeError:
                broken_pipe = True

        try:
            op(on_result)
            rc = 0
        except Boto3S3Error as e:
            rc = 1
            console.diag(f"{e}\n")
        if broken_pipe:
            raise BrokenPipeError
        return TransferResult(returncode=rc, results=results)

    # --- Public API --------------------------------------------------------
    def head_object(self, rel_key: str, *, verbose: bool = False) -> ObjectMeta | None:
        """HeadObject one key, or None when it is definitively absent (404).
        Any other failure - a denied head, a transport error - propagates:
        "could not tell" must never read as "not there"."""
        if verbose:
            console.diag(f"+ (boto3-s3) head_object {self._s3_url(rel_key)}\n")
        info = self._s3_loc(rel_key).get_fileinfo()
        if info is None:
            return None
        return ObjectMeta(
            key=rel_key,
            size=info.size or 0,
            etag=info.etag,
            storage_class=info.storage_class,
        )

    def etag_checker(self) -> Callable[[str, int, str | None], bool]:
        """A thread-safe ``(local_path, s3_size, s3_etag) -> differs`` content
        check against an S3 ETag the caller already holds (a listing or head
        result), so it costs no S3 call. One shared EtagComparison
        (thread-safe by contract) serves every call; part_size comes from the
        same profile the uploads use, so multipart ETags reconstruct to a
        matching value. A missing ETag reports "differs" - verification must
        fail loudly rather than silently pass."""
        from boto3_s3.etagcompare import EtagComparison

        comparison = EtagComparison(self._s3)

        def differs(local_path: str, s3_size: int, s3_etag: str | None) -> bool:
            return comparison.content_differs(local_path, etag=s3_etag, s3_size=s3_size)

        return differs

    def needs_upload(self, rel_key: str, local_path: str, *, verbose: bool = False) -> bool:
        """True when local_path should be (re)uploaded to rel_key, by content.

        The single-object counterpart of `--checksum`: no stored object (or no
        ETag) means upload; otherwise the shared ETag check (etag_checker) so
        the decision matches a dir entry's `--checksum` sync - an unchanged
        file is skipped, a same-size/same-mtime content change is not. The
        default (non-checksum) single-file decision is the manifest size+mtime
        check, not this.
        """
        head = self.head_object(rel_key, verbose=verbose)
        if head is None:
            return True
        return self.etag_checker()(local_path, head.size, head.etag)

    def iter_objects(self, rel_prefix: str, *, verbose: bool = False) -> Iterator[ObjectMeta]:
        """Stream the objects below ``rel_prefix/`` in listing (key byte) order,
        keys relative to ``rel_prefix`` - the same compare keys the manifest
        stream uses, so verify merge-joins the two without buffering. The
        listing is slash-bounded (``docs`` never scans ``docs.txt``); the exact
        object at ``rel_prefix`` itself is a caller-side head_object probe.
        Size, ETag, and storage class ride along on the listing for free."""
        api_base = self._api_key(rel_prefix)
        prefix = api_base + "/"
        if verbose:
            console.diag(f"+ (boto3) list objects s3://{self.bucket}/{prefix}\n")
        paginator = self._client.get_paginator("list_objects_v2")
        prev_key: str | None = None
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                # The merge-join against the manifest relies on strict ascending
                # key order, which standard S3 and MinIO guarantee (UTF-8 byte
                # order). A bucket that does not - an S3 Express directory bucket,
                # or a broken S3-compatible endpoint - would silently make verify
                # report phantom missing/unrecorded objects, so fail loudly.
                if prev_key is not None and key <= prev_key:
                    from boto3_s3 import Boto3S3Error

                    raise Boto3S3Error(
                        f"S3 listing under {prefix} is not in ascending key order "
                        f"({prev_key!r} then {key!r}); s3bak needs an order-preserving bucket"
                    )
                prev_key = key
                yield ObjectMeta(
                    key=key[len(prefix) :],
                    size=int(item.get("Size", 0)),
                    etag=str(item.get("ETag") or "").strip('"') or None,
                    storage_class=item.get("StorageClass"),
                )

    def list_top_level(self, *, verbose: bool = False) -> tuple[list[str], list[str]]:
        """Basenames directly under the prefix as ``(objects, prefixes)``:
        top-level object names (manifests and single-file data keys) and the
        common-prefix names the entry data trees appear as."""
        from boto3_s3 import FileKind

        if verbose:
            console.diag(f"+ (boto3-s3) ls {self.prefix}/\n")
        objects: list[str] = []
        prefixes: list[str] = []

        def collect(info: FileInfo) -> None:
            name = info.key.rstrip("/").rsplit("/", 1)[-1]
            if info.kind is FileKind.FILE:
                objects.append(name)
            else:
                prefixes.append(name)

        self._s3.ls(self._s3_loc(is_dir=True), recursive=False, on_entry=collect)
        return objects, prefixes

    def multipart_download(self, size: int | None) -> bool:
        """Which lane ``get_object`` takes for an object of ``size``: True for
        S3.cp's parallel multipart download, False for a single GetObject.
        Callers report the lane a single-object transfer used."""
        return size is not None and self._multipart(size)

    def get_object(
        self,
        rel_key: str,
        dest_path: str,
        *,
        size: int | None = None,
        verbose: bool = False,
    ) -> bool:
        """Download to dest_path; False if the object is absent (other errors
        propagate to run()). A large object (``size`` >= the multipart
        threshold) goes through S3.cp for a parallel multipart download;
        everything else - and any object of unknown size - is one streamed
        GetObject through ``S3Storage.get_file``, which skips s3transfer's
        machinery (a thread pool, and a pre-transfer HeadObject probe) and
        still lands the bytes atomically: a sibling temp file replaced into
        place, an existing destination's mode carried over, and the previous
        content left whole if the read fails."""
        from boto3_s3 import NotFoundError

        loc = self._s3_loc(rel_key)
        try:
            if self.multipart_download(size):
                if verbose:
                    console.diag(f"+ (boto3-s3) cp {self._s3_url(rel_key)} {dest_path}\n")
                # force_glacier_transfer: without it cp WARN-skips a GLACIER /
                # DEEP_ARCHIVE source and returns as if it succeeded, so pull would
                # apply the record's metadata over stale (or absent) local content
                # and exit 0. Forcing the transfer makes an archived-not-restored
                # object fail loudly (InvalidObjectState) - matching the small
                # object path's direct GetObject - and lets a restored one through.
                self._s3.cp(loc, dest_path, force_glacier_transfer=True)
            else:
                if verbose:
                    console.diag(f"+ (boto3-s3) get_file {self._s3_url(rel_key)}\n")
                loc.get_file(dest_path)
        except NotFoundError as e:
            if e.bucket is None:
                # A LOCAL not-found (the destination's directory raced away
                # mid-write): the object side said nothing, so this is not
                # "no such object" - let it propagate to run() like any other
                # local I/O failure.
                raise
            # A genuinely-absent object is "not present"; other errors
            # (access denied, transport, config) propagate to run().
            return False
        return True

    def delete_objects(
        self, rel_keys: Iterable[str], *, dryrun: bool = False, verbose: bool = False
    ) -> TransferResult:
        """Delete the objects at ``rel_keys`` (entry-relative), streaming.

        boto3-s3's ``S3Deleter`` batches within S3's 1,000-key limit and
        dispatches each batch on its own worker thread, so a generator of keys
        keeps producing - ``delete_subtree`` streams a listing straight in -
        while the previous batch deletes; every key's ``delete:`` line, or its
        failure, prints as its batch completes. That worker deletes through
        this store's one client while the caller lists on it, which is the
        shape boto3-s3's own ``rm`` runs (the one-client-per-thread contract
        is about transfers, which a batched delete is not). ``dryrun`` reports
        the would-be deletions inline, without sending anything.

        A non-zero result is what stops the caller from publishing a manifest
        that has already dropped the records (docs/recovery.md), so it must
        cover every key the response cannot vouch for: the deleter fails a key
        whose batch carried an error naming no submitted key, rather than
        inferring its success from the key's absence among the errors.

        Not to be called from inside a console prompt session: a dispatch
        waits for the previous batch (the deleter's backpressure point) while
        that batch's worker prints through the console the prompt holds.
        """
        from boto3_s3 import FileInfo, S3Deleter

        failed = 0

        def op(on_result: ResultCallback) -> None:
            nonlocal failed
            with S3Deleter(self._s3_loc(), on_result=on_result, dryrun=dryrun) as deleter:
                for rel in rel_keys:
                    deleter.submit(FileInfo(key=self._api_key(rel)))
            failed = deleter.failed

        # `verbose and not dryrun`: the + trace echoes the requests a run makes,
        # and a dryrun deleter sends none - it reports each submission inline.
        label = f"delete objects under {self.prefix}/ (batched)"
        result = self._transfer(verbose and not dryrun, label, op)
        if failed:
            return TransferResult(returncode=1, results=result.results)
        return result

    def delete_subtree(
        self, rel_key: str, *, dryrun: bool = False, verbose: bool = False
    ) -> TransferResult:
        """Delete the object at ``rel_key`` and objects below ``rel_key/``.

        Used by an explicit missing sub-path push with ``--delete`` and by the
        entry type-change migration. The exact object is probed separately and
        the listing is slash-bounded (``iter_objects``), so a request for
        ``docs`` can never remove - or even scan - a sibling such as
        ``docs.txt``.
        """
        if verbose:
            console.diag(f"+ (boto3) delete subtree s3://{self.bucket}/{self._api_key(rel_key)}\n")

        def doomed_keys() -> Iterator[str]:
            if self.head_object(rel_key, verbose=verbose) is not None:
                yield rel_key
            for meta in self.iter_objects(rel_key, verbose=verbose):
                yield f"{rel_key}/{meta.key}"

        return self.delete_objects(doomed_keys(), dryrun=dryrun, verbose=verbose)

    def stream_object_to_stdout(self, rel_key: str, *, verbose: bool = False) -> bool:
        """Write the object to stdout; False if it is absent, so the caller can
        say why in its own words (other errors propagate to run())."""
        from boto3_s3 import NotFoundError

        if verbose:
            console.diag(f"+ (boto3-s3) open {self._s3_url(rel_key)}\n")
        try:
            body = self._s3_loc().open(self._api_key(rel_key), "rb")
        except NotFoundError:
            return False
        with body:
            shutil.copyfileobj(body, sys.stdout.buffer)
        sys.stdout.buffer.flush()
        return True

    def sync_down(
        self,
        rel_prefix: str,
        dest_dir: str,
        *,
        compare: PairFilter | None = None,
        create: bool | FileFilter = True,
        dryrun: bool = False,
        verbose: bool = False,
    ) -> TransferResult:
        """``create`` is the create-lane value: True downloads every S3-only
        key (the default); a callable vetoes per key - pull's exclusion, which
        must not download an excluded path (docs/excludes.md)."""
        from boto3_s3 import LocalStorage

        src = self._s3_loc(rel_prefix, is_dir=True)
        # follow_symlinks moved onto the Storage in 0.5: the local dest is walked
        # to find existing / orphan files, and a symlink there stays a symlink
        # (never descended into), matching the push side.
        dest = LocalStorage(dest_dir, follow_symlinks=False)
        return self._transfer(
            verbose,
            f"sync {src} {dest_dir}/",
            lambda cb: self._s3.sync(
                src,
                dest,
                create_filter=create,
                dryrun=dryrun,
                update_filter=compare,
                on_result=cb,
                # force_glacier_transfer: the gate that would otherwise WARN-skip
                # a GLACIER / DEEP_ARCHIVE source checks info.head["Restore"], but
                # a recursive listing never populates a HeadObject - so a directory
                # sync can't see a completed restore and skips the object anyway,
                # no matter how it was restored. Forcing the transfer drops that
                # client-side guess and lets S3 decide: a restored object comes
                # down, an unrestored one fails loudly (InvalidObjectState) -
                # matching the single-object cp path above.
                force_glacier_transfer=True,
            ),
        )

    def put_file(self, rel_key: str, src_path: str, *, verbose: bool = False) -> None:
        """Upload a local file without result-line collection (manifests): one
        PutObject, whatever the size - a manifest is read and rewritten on
        nearly every command, and the single-request lane costs it no transfer
        engine. Errors (Boto3S3Error) surface to run()."""
        if verbose:
            console.diag(f"+ (boto3-s3) put_file {self._s3_url(rel_key)}\n")
        self._s3_loc(rel_key).put_file(src_path)

    def put_object(self, rel_key: str, src_path: str, *, verbose: bool = False) -> TransferResult:
        dst = self._s3_url(rel_key)
        loc = self._s3_loc(rel_key)
        if self._multipart(os.path.getsize(src_path)):
            return self._transfer(
                verbose,
                f"cp {src_path} {dst}",
                lambda cb: self._s3.cp(src_path, loc, on_result=cb),
            )
        # Small file: a single PutObject, and print the result line the
        # s3transfer callback would have printed (see _transfer.on_result).
        from boto3_s3 import Boto3S3Error

        if verbose:
            console.diag(f"+ (boto3-s3) put_file {dst}\n")
        try:
            loc.put_file(src_path)
        except Boto3S3Error as e:
            console.diag(f"{e}\n")
            return TransferResult(returncode=1, results=0)
        console.out(f"upload: {src_path} to {dst}\n")
        return TransferResult(returncode=0, results=1)

    def sync_up(
        self,
        src_dir: str,
        rel_prefix: str,
        *,
        pair_filter: MergedPairFilter,
        walker: LocalFileGenerator | None = None,
        dryrun: bool = False,
        verbose: bool = False,
    ) -> TransferResult:
        """`walker` is the excludes-pruning local walk (localwalk.sync_walker):
        excludes prune the LOCAL side only, through the same walker the
        manifest walk uses, so the data sync and the manifest can never
        disagree on what an exclude means. The S3 listing stays complete, so
        an object under an excluded path is an ordinary delete-lane orphan
        rather than invisible (see sync_walker).

        The local side enumerates the COMPLETE view (docs/journal.md): the
        root leads at compare key ``""``, and directories, symlink leaves,
        and special files enter the pair stream alongside regular files. The
        caller's lane filters are responsible for vetoing what the transfer
        cannot consume - push's PushJournal observes these entries for its
        journal and never lets them reach the transfer engine.

        `pair_filter` decides every merged pair - a new local item, a
        both-sides pair, an S3 orphan - through one callable (push's
        PushJournal.decide), called serially in ascending compare-key order
        on this thread. True takes the pair's default action: upload the new
        or changed file, delete the orphan."""
        from boto3_s3 import LocalStorage

        # follow_symlinks moved onto the Storage in 0.5: symlinks are not
        # uploaded as data; the manifest records them and apply_manifest
        # recreates them on restore.
        src = LocalStorage(
            src_dir, walker=walker, follow_symlinks=False, enumerate_all_entries=True
        )
        dst = self._s3_loc(rel_prefix, is_dir=True)
        return self._transfer(
            verbose,
            f"sync {src_dir} {dst}",
            lambda cb: self._s3.sync(
                src,
                dst,
                pair_filter=pair_filter,
                dryrun=dryrun,
                on_result=cb,
            ),
        )
