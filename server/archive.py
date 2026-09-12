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
of rewriting the whole file.

``energy.archive_filename`` names the archive (a relative path resolves against
the repository root) and may carry ``{...}`` date tokens, which rotate the file
whenever the token value changes - the shipped config asks for a monthly file::

    archive_filename: state/energy-{YYYYmm}.json.tar   # state/energy-202609.json.tar

so a year ends up as twelve hour-by-hour archives instead of one file that
grows forever. A brace group is a sequence of tokens plus literal separators,
so ``{YYYY}-{MM}-{DD}`` works as well. Recognised tokens (case-insensitive):
``YYYY YY MMM MM DD HH NN SS`` - ``MM``/``mm`` is the month (filenames are
normally month-stamped) and minutes are spelled ``NN``. An unknown token fails
at startup, and the resolved filename is logged at startup, so a pattern that
does not match your intent is visible immediately. Without tokens the archive
is a single growing file. There is no retention policy either way - prune old
tars by hand, they are only ever written, never read back by the server.

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
import re
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

#: Date tokens accepted inside ``{...}`` in ``energy.archive_filename``
#: (``{token}`` -> strftime directive). Matching is case-insensitive and
#: ``MM``/``mm`` *both* mean month, because a filename is normally
#: month-stamped - the shipped ``{YYYYmm}.json.tar`` yields ``...-202609.json.tar``.
#: Minutes are therefore spelled ``NN``. (The hourly member names keep the ISO
#: habit instead: ``energy-YYYYmmddHHMMSS.json``, lowercase ``mm`` = minute.)
#: One group may combine several tokens and literal separators
#: (``{YYYY-MM-DD}``).
DATE_TOKENS = {
    "YYYY": "%Y",  # 4-digit year
    "YY": "%y",  # 2-digit year
    "MMM": "%b",  # month name (Sep)
    "MM": "%m",  # month 01-12
    "DD": "%d",  # day of month 01-31
    "HH": "%H",  # hour 00-23
    "NN": "%M",  # minute 00-59
    "SS": "%S",  # second 00-59
}

#: Longest token length, for left-to-right matching inside a group (``MMM``
#: must win over ``MM``).
_MAX_TOKEN_LEN = max(len(token) for token in DATE_TOKENS)

_TOKEN_GROUP_RE = re.compile(r"\{([^{}]+)\}")


def _match_token(text: str, start: int) -> Optional[str]:
    """Return the longest known date token at ``text[start:]``, else None."""
    for length in range(_MAX_TOKEN_LEN, 0, -1):
        candidate = text[start : start + length].upper()
        if candidate in DATE_TOKENS:
            return candidate
    return None


def expand_tokens(pattern: str, when: datetime) -> str:
    """Replace every ``{...}`` date group in ``pattern`` with its ``when`` value.

    A group is a sequence of tokens and literal separators (``{YYYYmm}``,
    ``{YYYY}-{MM}-{DD}``); tokens are case-insensitive and ``MM``/``mm`` both
    mean month (minutes are ``NN``). Raises :class:`ValueError` for an unknown
    token so a typo in ``energy.archive_filename`` fails at startup instead of
    quietly writing to the wrong file (same fail-fast policy as the allowlists).
    """

    def _replace(match: "re.Match[str]") -> str:
        group = match.group(1)
        parts = []
        position = 0
        while position < len(group):
            char = group[position]
            if not char.isalpha():
                parts.append(char)  # literal separator ("-", "_", ".")
                position += 1
                continue
            token = _match_token(group, position)
            if token is None:
                raise ValueError(
                    f"unknown date token '{group[position:]}' in '{{{group}}}' "
                    f"(known: {' '.join(DATE_TOKENS)})"
                )
            parts.append(when.strftime(DATE_TOKENS[token]))
            position += len(token)
        return "".join(parts)

    return _TOKEN_GROUP_RE.sub(_replace, pattern)


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
        archive_file: str,
        payload: Callable[[], Dict[str, object]],
        base_dir: Path = Path("."),
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        #: ``energy.archive_filename`` - the archive path, possibly carrying
        #: ``{token}`` date fields (see :data:`DATE_TOKENS`).
        self._file_pattern = str(archive_file)
        #: Directory a relative pattern resolves against (the repository root).
        self._base_dir = Path(base_dir)
        #: Callable returning the current state document, e.g.
        #: :meth:`server.energy.EnergyStore.payload` (kept as a callback so the
        #: archiver never holds a stale copy of the counters).
        self._payload = payload
        self._interval_s = max(1.0, float(interval_s))
        self._clock = clock  # injectable wall clock (epoch seconds)
        self._next_slot: Optional[float] = None  # epoch sec of the next snapshot
        self._tar: Optional[tarfile.TarFile] = None  # open append handle
        self._data_end: Optional[int] = None  # offset after the last member
        self._current_file: Optional[Path] = None  # file the handle belongs to
        self.path_for(datetime.now())  # fail fast on a bad pattern/token

    @property
    def current_file(self) -> Path:
        """Archive file the next snapshot goes to (pattern resolved for now)."""
        return self.path_for()

    def path_for(self, when: Optional[datetime] = None) -> Path:
        """Resolve the archive file for ``when``, expanding ``{...}`` tokens."""
        path = Path(expand_tokens(self._file_pattern, when or datetime.now()))
        return path if path.is_absolute() else self._base_dir / path

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
        info.mode = 0o644
        when = when or datetime.now()
        info.mtime = int(when.timestamp())
        archive_file = self.path_for(when)
        try:
            if self._current_file is not None and archive_file != self._current_file:
                # A date token rolled over (``{YYYYmm}`` crossing a month, say):
                # finish the old archive, then start the new one with this
                # member. Without tokens the path never changes, so nothing
                # rotates and the archive is a single growing file.
                LOGGER.info("Energy archive rotating -> %s", archive_file)
                self.close()
            tar = self._open(archive_file)
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
                "Could not append %s to %s: %s", name, archive_file, exc
            )
            self._rollback()
            return None
        LOGGER.info("Archived energy state as %s -> %s", name, archive_file)
        return name

    def close(self) -> None:
        """Finish the archive (write its marker) and release the handle."""
        tar = self._tar
        archive_file = self._current_file
        self._tar = None
        self._current_file = None
        if tar is None:
            return
        try:
            if self._data_end is not None:
                tar.fileobj.seek(self._data_end)
            tar.close()
        except (OSError, tarfile.TarError) as exc:
            LOGGER.warning("Could not close %s: %s", archive_file, exc)
        self._data_end = None

    # -- handle / recovery --------------------------------------------------

    def _open(self, archive_file: Path) -> tarfile.TarFile:
        """Return the append handle for ``archive_file``, opening it on first use.

        ``tarfile``'s own append scan (O(n) over the existing members) runs
        only here, once per process per file, and leaves the file positioned
        after the last member's data - exactly where the next member goes. A
        file that is not a tar raises ReadError (a TarError) and is reported,
        never clobbered.
        """
        if self._tar is not None:
            return self._tar
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        if archive_file.exists() and archive_file.stat().st_size == 0:
            # tarfile refuses to append to a zero-byte file (it reports an
            # "empty header"); such a file holds no data, so start a fresh
            # archive instead of leaving archiving permanently broken.
            LOGGER.warning("Discarding empty archive file %s", archive_file)
            archive_file.unlink()
        tar = tarfile.open(archive_file, "a")
        self._data_end = tar.fileobj.tell()
        self._tar = tar
        self._current_file = archive_file
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
            LOGGER.warning("Could not repair %s: %s", self._current_file, exc)
            self._tar = None  # reopen / re-scan on the next attempt
            self._current_file = None
            try:
                tar.fileobj.close()
            except OSError:
                pass
