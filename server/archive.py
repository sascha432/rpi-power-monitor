"""Hourly tar snapshots of the energy state.

``state/energy.json`` is the *live* document: it is atomically overwritten
every ``server.main.SAVE_EVERY_SECONDS`` (and at midnight rollover), so it only
ever holds the current totals + the rolling per-day log. When
``energy.archive: true`` is set in ``config/server.yaml`` the server also
appends a copy of the current document to ``state/energy.json.tar`` once an
hour, as a member named::

    energy-YYYYmmddHHMMSS.json

so an operator can diff or restore any hourly sample:

.. code-block:: console

    tar -tf state/energy.json.tar                  # list the snapshots
    tar -xf state/energy.json.tar energy-20260912140000.json

The archive is an *uncompressed* tar on purpose: only plain tars can be
appended to in place, so each hour tacks one small member onto the end instead
of rewriting the whole file. There is no retention policy - the archive grows
by one member per hour (~9k members a year, a few MiB) - which is the point of
an append-only log, but it is worth watching on a small SD card.

Snapshots are taken on whole-``interval`` boundaries of the wall clock (the
first one lands at the next ``HH:00`` after startup, not at process start), so
the member timestamp always names the hour it belongs to. If the process is
busy, or was down, across a boundary the missed slots are skipped - a monitor
cannot reconstruct an hour it never observed.

Implementation note: the tar handle stays open for the lifetime of the process.
``tarfile`` in append mode rescans every member to find the end of the archive
(57 ms for 3000 members, growing with the file), which would stall the sampling
loop for up to seconds per snapshot on a long-running Pi. Instead the handle is
opened once and each snapshot is a plain ``seek`` back over the previous
end-of-archive marker, one ``addfile`` and a rewritten marker: O(1), always a
complete archive on disk, and unchanged in behaviour across restarts.
"""
from __future__ import annotations

import io
import logging
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from .energy import dump_state

LOGGER = logging.getLogger("server.archive")

#: Seconds between snapshots (one hour).
DEFAULT_INTERVAL_S = 3600.0

#: End-of-archive marker: two zero blocks padded to a full 20-block record, as
#: written by ``tarfile``/GNU tar. Rewritten after every append (so ``tar`` can
#: always read the archive) and seeked back over before the next one.
END_OF_ARCHIVE = b"\0" * tarfile.RECORDSIZE


def member_name(when: Optional[datetime] = None) -> str:
    """Return the tar member name for a snapshot taken at ``when``.

    Defaults to *now*; the local-time timestamp makes every member unique and
    readable (``energy-20260912140000.json`` = 2026-09-12 14:00:00).
    """
    stamp = (when or datetime.now()).strftime("%Y%m%d%H%M%S")
    return f"energy-{stamp}.json"


class EnergyArchive:
    """Append periodic snapshots of the energy state to a tar archive.

    Only ever driven from the sampling loop, so no locking of its own is
    needed; the payload callback (:meth:`server.energy.EnergyStore.payload`)
    does its own locking.
    """

    def __init__(
        self,
        archive_file: Path,
        payload: Callable[[], Dict[str, object]],
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        #: ``state/energy.json.tar`` - created on the first snapshot.
        self._archive_file = Path(archive_file)
        #: Callable returning the current state document, e.g.
        #: :meth:`server.energy.EnergyStore.payload` (kept as a callback so the
        #: archiver never holds a stale copy of the counters).
        self._payload = payload
        self._interval_s = max(1.0, float(interval_s))
        self._clock = clock  # injectable wall clock (epoch seconds)
        self._next_slot: Optional[float] = None  # epoch sec of the next snapshot
        self._tar: Optional[tarfile.TarFile] = None  # open append handle
        self._data_end: Optional[int] = None  # offset after the last member

    @property
    def archive_file(self) -> Path:
        """Path of the tar archive being appended to."""
        return self._archive_file

    # -- cadence ------------------------------------------------------------

    def _slot_after(self, now: float) -> float:
        """Epoch second of the first whole-``interval`` boundary after ``now``."""
        return (int(now // self._interval_s) + 1) * self._interval_s

    def maybe_write(self, now: Optional[float] = None) -> Optional[str]:
        """Append a snapshot if an hourly slot has been reached.

        Returns the member name written, or ``None`` when it is not time yet
        (or the write failed - failures are logged, never raised, so a full
        disk cannot interrupt sampling). The first call only *arms* the timer,
        so the initial snapshot lands on the next whole hour rather than at
        process start.
        """
        current = self._clock() if now is None else now
        if self._next_slot is None:
            # First call only arms the timer (next boundary, not "now"), so a
            # restarting server does not spawn an off-hour snapshot.
            self._next_slot = self._slot_after(current)
            return None
        if current < self._next_slot:
            return None
        # Advance to the next *future* boundary: if the loop stalled past a
        # slot, that hour was never observed and is skipped rather than
        # back-filled with a duplicate of the current values.
        self._next_slot = self._slot_after(current)
        return self.write(when=datetime.fromtimestamp(current))

    # -- writing ------------------------------------------------------------

    def write(self, when: Optional[datetime] = None) -> Optional[str]:
        """Append the current state document as ``energy-YYYYmmddHHMMSS.json``.

        Returns the member name on success, ``None`` if the archive could not
        be written (logged as a warning; the archive is rolled back to its
        previous, still-readable state).
        """
        name = member_name(when)
        body = dump_state(self._payload()).encode("utf-8")
        info = tarfile.TarInfo(name)
        info.size = len(body)
        info.mtime = int(when.timestamp()) if when is not None else int(time.time())
        info.mode = 0o644
        try:
            tar = self._open()
            # Overwrite the end-of-archive marker with the new member, then
            # restore it, so on disk the file is a complete tar before and
            # after the append.
            tar.fileobj.seek(self._data_end)
            tar.addfile(info, io.BytesIO(body))
            self._data_end = tar.fileobj.tell()
            tar.fileobj.write(END_OF_ARCHIVE)
            tar.fileobj.flush()
        except (OSError, tarfile.TarError) as exc:
            LOGGER.warning(
                "Could not append %s to %s: %s", name, self._archive_file, exc
            )
            self._rollback()
            return None
        LOGGER.info("Archived energy state as %s -> %s", name, self._archive_file)
        return name

    def close(self) -> None:
        """Finish the archive (write its marker) and release the handle."""
        tar = self._tar
        self._tar = None
        if tar is None:
            return
        try:
            if self._data_end is not None:
                tar.fileobj.seek(self._data_end)
            tar.close()
        except (OSError, tarfile.TarError) as exc:
            LOGGER.warning("Could not close %s: %s", self._archive_file, exc)
        self._data_end = None

    # -- handle / recovery --------------------------------------------------

    def _open(self) -> tarfile.TarFile:
        """Return the append handle, opening the archive on first use.

        ``tarfile``'s own append scan (O(n) over the existing members) runs
        only here, once per process, and leaves the file positioned after the
        last member's data - exactly where the next member goes. A file that is
        not a tar raises ReadError (a TarError) and is reported, never
        clobbered.
        """
        if self._tar is not None:
            return self._tar
        self._archive_file.parent.mkdir(parents=True, exist_ok=True)
        if self._archive_file.exists() and self._archive_file.stat().st_size == 0:
            # tarfile refuses to append to a zero-byte file (it reports an
            # "empty header"); such a file holds no data, so start a fresh
            # archive instead of leaving archiving permanently broken.
            LOGGER.warning("Discarding empty archive file %s", self._archive_file)
            self._archive_file.unlink()
        tar = tarfile.open(self._archive_file, "a")
        self._data_end = tar.fileobj.tell()
        self._tar = tar
        return tar

    def _rollback(self) -> None:
        """Drop a partially written member and restore the archive marker.

        A failed ``addfile`` (typically ENOSPC) leaves a torn member at the
        tail, which would make the whole archive unreadable. Truncating back to
        the known end of the last good member keeps every earlier snapshot.
        """
        tar = self._tar
        if tar is None or self._data_end is None:
            return
        try:
            tar.fileobj.seek(self._data_end)
            tar.fileobj.truncate(self._data_end)
            tar.fileobj.write(END_OF_ARCHIVE)
            tar.fileobj.flush()
        except OSError as exc:
            LOGGER.warning("Could not repair %s: %s", self._archive_file, exc)
            self._tar = None  # reopen / re-scan on the next attempt
            try:
                tar.fileobj.close()
            except OSError:
                pass
