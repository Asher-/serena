"""filesystem lifecycle primitives: typed, never-raising list/find/stat/create/delete/rename over a project root.

Implements the reflex filesystem quartet+ (spec-v2 §5.4): a low-ceremony filesystem floor an agent
reaches for instead of raw ``ls`` / ``find`` / ``stat`` / ``mv`` / ``rm``. Every operation resolves
relative paths against a single project root (read-root == write-root) and returns a typed result;
nothing raises to the caller (spec-v2 §5.9). The mutators carry the §5.5 optimistic-concurrency
``expect_version`` compare-and-swap: a caller passes the ``version`` a prior ``stat`` reported, and a
write that would clobber changed bytes is refused with a typed stale state that hands back the current
bytes as a recovery ramp.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat as stat_module
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from serena.util.file_system import GitignoreParser
from solidlsp.structural.backends.plaintext import PlaintextFloor


def content_version(raw_bytes: bytes) -> str:
    """The optimistic-concurrency token for a blob of content -- ``sha256(bytes)[:16]`` (spec-v2 §5.5).

    :param raw_bytes: the exact bytes to fingerprint.
    :return: the first 16 hex characters of the SHA-256 digest; the compare-and-swap currency every
        lifecycle mutator accepts as ``expect_version`` and every :meth:`FilesystemLifecycle.stat`
        reports.
    """
    return hashlib.sha256(raw_bytes).hexdigest()[:16]


class EntryKind(Enum):
    """The kind of a filesystem entry, classified without following symlinks."""

    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"


@dataclass(frozen=True)
class DirEntry:
    """A single typed directory entry (spec-v2 §5.4 ``list_dir``).

    :ivar name: the entry's path relative to the listed directory -- a bare base name for a
        non-recursive listing, a nested relative path (``"sub/file.txt"``) when recursive.
    :ivar kind: file / dir / symlink; a symlink is reported as such and never followed.
    :ivar size: the entry's own byte size (from ``lstat``; the link itself for a symlink).
    :ivar mtime: last-modification time, epoch seconds.
    :ivar symlink_target: the raw link target when :attr:`kind` is :attr:`EntryKind.SYMLINK`, else ``None``.
    """

    name: str
    kind: EntryKind
    size: int
    mtime: float
    symlink_target: str | None = None


@dataclass(frozen=True)
class DirListing:
    """The typed result of ``list_dir``: the entries plus a truncation flag and a not-found/error state.

    :ivar relative_path: the listed directory, relative to the project root.
    :ivar entries: the visible entries (gitignored and hidden entries omitted unless opted in).
    :ivar truncated: whether ``max_entries`` cut the listing short.
    :ivar exists: whether the listed path exists.
    :ivar error: a rendered error state (missing / not-a-directory / escapes-root), or ``None``.
    """

    relative_path: str
    entries: tuple[DirEntry, ...]
    truncated: bool
    exists: bool = True
    error: str | None = None

    def render(self) -> str:
        """Render the agent-facing listing (or the typed error state)."""
        if self.error is not None:
            return f"{self.relative_path}: {self.error}"
        count = len(self.entries)
        lines = [f"{self.relative_path}: {count} entr{'y' if count == 1 else 'ies'}"]
        for entry in self.entries:
            if entry.kind is EntryKind.DIR:
                lines.append(f"  {entry.name}/")
            elif entry.kind is EntryKind.SYMLINK:
                lines.append(f"  {entry.name} -> {entry.symlink_target} (symlink)")
            else:
                lines.append(f"  {entry.name} ({entry.size} bytes)")
        if self.truncated:
            lines.append("  ... (truncated; raise max_entries or narrow relative_path)")
        return "\n".join(lines)


@dataclass(frozen=True)
class FileMatches:
    """The typed result of ``find_file``: matched project-root-relative paths plus a truncation flag."""

    pattern: str
    relative_path: str
    matches: tuple[str, ...]
    truncated: bool

    def render(self) -> str:
        """Render the agent-facing match list; never routes to the deleted content-search tool."""
        if not self.matches:
            return f"No files match {self.pattern!r} under {self.relative_path}."
        lines = [f"{len(self.matches)} file(s) match {self.pattern!r} under {self.relative_path}:"]
        lines.extend(f"  {match}" for match in self.matches)
        if self.truncated:
            lines.append("  ... (truncated; raise max_results or tighten the pattern)")
        return "\n".join(lines)


@dataclass(frozen=True)
class FileStat:
    """The typed result of ``stat`` (spec-v2 §5.4/§5.9): existence, kind, and writer-preservation metadata.

    :ivar relative_path: the target, relative to the project root.
    :ivar exists: whether the target exists.
    :ivar kind: file / dir / symlink, classified without following symlinks.
    :ivar size: the target's own byte size (``lstat``).
    :ivar permissions: the mode bits rendered as an octal string (e.g. ``"0o644"``).
    :ivar symlink_target: the raw link target when :attr:`kind` is a symlink, else ``None``.
    :ivar encoding: the declared decode of a regular file's bytes, else ``None``.
    :ivar eol: the line-ending style of a regular file (``"LF"`` / ``"CRLF"`` / ...), else ``None``.
    :ivar trailing_newline: whether a regular file ends in a newline, else ``None``.
    :ivar is_binary: whether a regular file is binary, else ``None``.
    :ivar version: the content version of a regular file (the ``expect_version`` token), else ``None``.
    :ivar error: a rendered error state (missing / escapes-root / unreadable), or ``None``.
    """

    relative_path: str
    exists: bool
    kind: EntryKind | None = None
    size: int = 0
    permissions: str = ""
    symlink_target: str | None = None
    encoding: str | None = None
    eol: str | None = None
    trailing_newline: bool | None = None
    is_binary: bool | None = None
    version: str | None = None
    error: str | None = None

    def render(self) -> str:
        """Render the agent-facing stat line (or the typed not-found / error state)."""
        if not self.exists:
            return f"{self.relative_path}: {self.error or 'not found'}"
        assert self.kind is not None
        parts = [f"{self.relative_path}: {self.kind.value}", f"{self.size} bytes", self.permissions]
        if self.kind is EntryKind.SYMLINK:
            parts.append(f"-> {self.symlink_target}")
        if self.kind is EntryKind.FILE:
            parts.extend(
                [
                    self.encoding or "",
                    self.eol or "",
                    "trailing newline" if self.trailing_newline else "no trailing newline",
                    f"version {self.version}",
                ]
            )
        return ", ".join(part for part in parts if part)


@dataclass(frozen=True)
class StaleVersion:
    """A typed optimistic-concurrency mismatch (spec-v2 §5.5): expected vs actual + the current bytes.

    Returned by a mutator whose ``expect_version`` no longer matches the target on disk. The current
    bytes ride along as a recovery ramp so the caller can re-read, re-base its intent, and retry.
    """

    relative_path: str
    expected: str
    actual: str
    current_bytes: bytes

    def render(self) -> str:
        """Render the agent-facing stale state, pointing the caller at a re-read."""
        return (
            f"Stale: {self.relative_path} changed under you (expected version {self.expected}, "
            f"actual {self.actual}); re-read {self.relative_path} and retry with the current version. "
            f"Current content is {len(self.current_bytes)} bytes."
        )


@dataclass(frozen=True)
class MutationResult:
    """The typed result of a mutator (create / delete / rename): an outcome plus an optional stale ramp."""

    ok: bool
    message: str
    stale: StaleVersion | None = None

    def render(self) -> str:
        """Render the agent-facing outcome (the stale state when a compare-and-swap failed)."""
        if self.stale is not None:
            return self.stale.render()
        return self.message


class FilesystemLifecycle:
    """Object-oriented filesystem lifecycle service bound to a single project root (spec-v2 §5.4).

    Resolves every relative path against ``root`` (read-root == write-root) and returns a typed result;
    no operation raises to the caller (spec-v2 §5.9). Reads (``list_dir`` / ``find_file`` / ``stat``)
    skip gitignored and hidden entries by default with explicit opt-outs; mutators (``create`` /
    ``delete`` / ``rename``) support optimistic-concurrency compare-and-swap via ``expect_version``.
    """

    def __init__(self, root: str, is_ignored: Callable[[str], bool] | None = None) -> None:
        """
        :param root: the project root; read-root and write-root are the same directory.
        :param is_ignored: a predicate taking an absolute path and returning whether it is gitignored.
            Defaults to a :class:`GitignoreParser` over ``root`` so a bare service still honors
            ``.gitignore``; the real tools inject ``Project.is_ignored_path`` for the full ignore policy.
        """
        self._root = os.path.abspath(root)
        self._floor = PlaintextFloor()
        self._is_ignored = is_ignored if is_ignored is not None else GitignoreParser(root).should_ignore

    # --- path resolution / containment ---

    def _abs(self, relative_path: str) -> str | None:
        """Resolve a relative path against the root; return ``None`` if it escapes the root."""
        combined = os.path.normpath(os.path.join(self._root, relative_path))
        if combined == self._root or combined.startswith(self._root + os.sep):
            return combined
        return None

    @staticmethod
    def _lstat(abs_path: str) -> os.stat_result | None:
        # a never-raising lstat: filters out entries that vanish or deny access mid-scan
        try:
            return os.lstat(abs_path)
        except OSError:
            return None

    @staticmethod
    def _readlink(abs_path: str) -> str | None:
        try:
            return os.readlink(abs_path)
        except OSError:
            return None

    # --- list_dir ---

    def list_dir(
        self,
        relative_path: str = ".",
        recursive: bool = False,
        max_entries: int = 1000,
        include_ignored: bool = False,
        include_hidden: bool = False,
    ) -> DirListing:
        """List a directory's entries as typed :class:`DirEntry` records; never raise (spec-v2 §5.4).

        Gitignored and hidden (dot-prefixed) entries are omitted by default -- silent omission would be
        false-inaccessibility, so the opt-outs make them explicit. Symlinks are reported and never
        followed. A not-found or not-a-directory target yields a typed error state, not an exception.
        """
        abs_path = self._abs(relative_path)
        if abs_path is None:
            return DirListing(relative_path, (), False, exists=False, error="path escapes the project root")
        if not os.path.exists(abs_path):
            return DirListing(relative_path, (), False, exists=False, error="not found")
        if not os.path.isdir(abs_path):
            return DirListing(relative_path, (), False, exists=True, error="not a directory")

        entries: list[DirEntry] = []
        truncated = self._walk(abs_path, abs_path, recursive, max_entries, include_ignored, include_hidden, entries)
        return DirListing(relative_path, tuple(entries), truncated)

    def _walk(
        self,
        base_abs: str,
        dir_abs: str,
        recursive: bool,
        max_entries: int,
        include_ignored: bool,
        include_hidden: bool,
        out: list[DirEntry],
    ) -> bool:
        # scan one directory (sorted for stable output), appending visible entries; recurse into real
        # sub-directories only. Returns True once max_entries is reached (the truncation signal).
        try:
            with os.scandir(dir_abs) as scanner:
                scanned = sorted(scanner, key=lambda entry: entry.name)
        except OSError:
            return False

        for entry in scanned:
            # apply the default hidden/gitignore filters (each with an explicit opt-out)
            if not include_hidden and entry.name.startswith("."):
                continue
            if not include_ignored and self._is_ignored(entry.path):
                continue

            # the truncation boundary: stop once the cap is hit, before adding another entry
            if len(out) >= max_entries:
                return True

            name = os.path.relpath(entry.path, base_abs)
            st = self._lstat(entry.path)
            size = st.st_size if st is not None else 0
            mtime = st.st_mtime if st is not None else 0.0

            # a symlink is reported with its target and never followed
            if entry.is_symlink():
                out.append(DirEntry(name, EntryKind.SYMLINK, size, mtime, self._readlink(entry.path)))
                continue

            # a real directory is listed and, when recursive, descended into
            if entry.is_dir(follow_symlinks=False):
                out.append(DirEntry(name, EntryKind.DIR, size, mtime))
                if recursive and self._walk(
                    base_abs, entry.path, recursive, max_entries, include_ignored, include_hidden, out
                ):
                    return True
                continue

            # everything else is a regular file
            out.append(DirEntry(name, EntryKind.FILE, size, mtime))

        return False

    # --- find_file ---

    def find_file(
        self,
        pattern: str,
        relative_path: str = ".",
        max_results: int = 1000,
        include_ignored: bool = False,
    ) -> FileMatches:
        """Find files whose PATH matches ``pattern`` (glob or substring); gitignore-filtered (spec-v2 §5.4).

        This searches paths, not contents (that is ``search_for_pattern``'s job): a pattern with glob
        metacharacters is matched with :func:`fnmatch.fnmatch` against both the root-relative path and
        the base name; a plain pattern is matched as a path substring. Hidden and gitignored files are
        skipped; symlinked directories are not traversed.
        """
        abs_path = self._abs(relative_path)
        if abs_path is None or not os.path.isdir(abs_path):
            return FileMatches(pattern, relative_path, (), False)

        has_glob = any(char in pattern for char in "*?[")
        matches: list[str] = []
        truncated = False

        # walk the subtree, pruning hidden/ignored directories in place; os.walk does not follow symlinks
        for current_dir, dir_names, file_names in os.walk(abs_path):
            dir_names[:] = [
                name
                for name in dir_names
                if not name.startswith(".")
                and (include_ignored or not self._is_ignored(os.path.join(current_dir, name)))
            ]
            for file_name in sorted(file_names):
                if file_name.startswith("."):
                    continue
                file_abs = os.path.join(current_dir, file_name)
                if not include_ignored and self._is_ignored(file_abs):
                    continue
                rel = os.path.relpath(file_abs, self._root)
                if self._path_matches(rel, file_name, pattern, has_glob):
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    matches.append(rel)
            if truncated:
                break

        return FileMatches(pattern, relative_path, tuple(sorted(matches)), truncated)

    @staticmethod
    def _path_matches(rel: str, base_name: str, pattern: str, has_glob: bool) -> bool:
        # glob patterns match the whole relative path or the base name; plain patterns are a substring
        if has_glob:
            return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base_name, pattern)
        return pattern in rel

    # --- stat ---

    def stat(self, relative_path: str) -> FileStat:
        """Describe a target: kind, size, permissions, and (for a file) content metadata + version (spec-v2 §5.4).

        Symlinks are described without following. A regular file's line-ending / encoding / trailing-newline
        state comes from the plaintext floor (the same projection the read surface uses) and its content
        version is the ``expect_version`` token the mutators accept. A missing target is a typed state.
        """
        abs_path = self._abs(relative_path)
        if abs_path is None:
            return FileStat(relative_path, exists=False, error="path escapes the project root")

        st = self._lstat(abs_path)
        if st is None:
            return FileStat(relative_path, exists=False, error="not found")

        permissions = oct(stat_module.S_IMODE(st.st_mode))
        if stat_module.S_ISLNK(st.st_mode):
            return FileStat(
                relative_path,
                exists=True,
                kind=EntryKind.SYMLINK,
                size=st.st_size,
                permissions=permissions,
                symlink_target=self._readlink(abs_path),
            )
        if stat_module.S_ISDIR(st.st_mode):
            return FileStat(relative_path, exists=True, kind=EntryKind.DIR, size=st.st_size, permissions=permissions)

        # a regular file: reuse the plaintext floor for content metadata and compute the CAS version
        try:
            with open(abs_path, "rb") as handle:
                raw = handle.read()
        except OSError as error:
            return FileStat(
                relative_path, exists=True, kind=EntryKind.FILE, size=st.st_size, permissions=permissions, error=str(error)
            )
        view = self._floor.render(raw, relative_path)
        return FileStat(
            relative_path,
            exists=True,
            kind=EntryKind.FILE,
            size=st.st_size,
            permissions=permissions,
            encoding=view.encoding,
            eol=view.eol,
            trailing_newline=view.trailing_newline,
            is_binary=view.is_binary,
            version=content_version(raw),
        )

    # --- version / compare-and-swap ---

    def _check_version(self, abs_path: str, relative_path: str, expected: str) -> StaleVersion | None:
        # compare the caller's expected version against the target's current content; on mismatch,
        # return a stale state carrying the current bytes as a recovery ramp
        try:
            with open(abs_path, "rb") as handle:
                raw = handle.read()
        except OSError:
            raw = b""
        actual = content_version(raw)
        if actual != expected:
            return StaleVersion(relative_path, expected, actual, raw)
        return None

    def _atomic_write(self, abs_path: str, data: bytes) -> str | None:
        # write via a temp file in the target directory, then os.replace: the swap is atomic and
        # crash-safe (a failure before replace leaves the original intact and never litters a temp).
        parent = os.path.dirname(abs_path)
        # capture the target's prior mode on an OVERWRITE: mkstemp creates the temp at 0o600, so
        # replacing an existing file would silently strip its permission bits (notably +x)
        existing = self._lstat(abs_path)
        prior_mode = (
            stat_module.S_IMODE(existing.st_mode)
            if existing is not None and stat_module.S_ISREG(existing.st_mode)
            else None
        )
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=parent or None, prefix=".serena-tmp-")
        except OSError as error:
            return str(error)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # restore the overwritten file's mode (mkstemp reset it to 0o600); a brand-new file
            # keeps the default mode
            if prior_mode is not None:
                os.chmod(tmp_path, prior_mode)
            os.replace(tmp_path, abs_path)
            return None
        except BaseException as error:
            # clean up the temp so a failed write never leaves litter beside the target
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return str(error)

    # --- create ---

    def create(
        self, relative_path: str, content: str = "", overwrite: bool = False, expect_version: str = ""
    ) -> MutationResult:
        """Create (or, with intent, overwrite) a UTF-8 file atomically (spec-v2 §5.4/§5.5).

        Creating a brand-new file needs no ceremony. Overwriting requires explicit ``overwrite=True`` --
        a create against an existing path is refused with the original bytes intact (no silent clobber).
        Pass ``expect_version`` (from ``stat``) for a safe compare-and-swap overwrite; omit it to
        overwrite unconditionally. The write is atomic (temp + ``os.replace``).
        """
        abs_path = self._abs(relative_path)
        if abs_path is None:
            return MutationResult(False, f"Refused: {relative_path} escapes the project root.")

        exists = os.path.lexists(abs_path)
        if exists and not overwrite:
            return MutationResult(
                False,
                f"Refused: {relative_path} already exists. Pass overwrite=True to replace it "
                f"(optionally with expect_version from stat for a safe compare-and-swap).",
            )
        if exists and overwrite and expect_version:
            stale = self._check_version(abs_path, relative_path, expect_version)
            if stale is not None:
                return MutationResult(False, stale.render(), stale=stale)

        error = self._atomic_write(abs_path, content.encode("utf-8"))
        if error is not None:
            return MutationResult(False, f"Failed to write {relative_path}: {error}")
        verb = "Overwrote" if exists else "Created"
        return MutationResult(True, f"{verb} {relative_path} ({len(content)} characters).")

    # --- delete ---

    def delete(self, relative_path: str, expect_version: str = "") -> MutationResult:
        """Delete a file, idempotently and (optionally) under compare-and-swap (spec-v2 §5.4/§5.5).

        A missing target is a success (idempotent). Pass ``expect_version`` (from ``stat``) to refuse
        the delete if the content changed under you; omit it to delete unconditionally.
        """
        abs_path = self._abs(relative_path)
        if abs_path is None:
            return MutationResult(False, f"Refused: {relative_path} escapes the project root.")
        if not os.path.lexists(abs_path):
            return MutationResult(True, f"{relative_path} does not exist (nothing to delete).")
        if expect_version:
            stale = self._check_version(abs_path, relative_path, expect_version)
            if stale is not None:
                return MutationResult(False, stale.render(), stale=stale)

        try:
            if os.path.isdir(abs_path) and not os.path.islink(abs_path):
                os.rmdir(abs_path)
            else:
                os.unlink(abs_path)
            return MutationResult(True, f"Deleted {relative_path}.")
        except OSError as error:
            return MutationResult(False, f"Failed to delete {relative_path}: {error}")

    # --- rename == move ---

    def rename(self, source: str, destination: str, expect_version: str = "") -> MutationResult:
        """Rename (== move) a path; one verb for both, dumb-mv semantics (spec-v2 §5.4/§5.5).

        Both endpoints must resolve under the project root. Missing destination parents are created.
        Pass ``expect_version`` (from ``stat`` of the source) for a compare-and-swap move; omit it to move
        unconditionally. Reference-aware companion edits are deferred -- this is a plain move.
        """
        src_abs = self._abs(source)
        dst_abs = self._abs(destination)
        if src_abs is None:
            return MutationResult(False, f"Refused: source {source} escapes the project root.")
        if dst_abs is None:
            return MutationResult(False, f"Refused: destination {destination} escapes the project root.")
        if not os.path.lexists(src_abs):
            return MutationResult(False, f"Refused: source {source} does not exist.")
        if expect_version:
            stale = self._check_version(src_abs, source, expect_version)
            if stale is not None:
                return MutationResult(False, stale.render(), stale=stale)

        try:
            parent = os.path.dirname(dst_abs)
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.replace(src_abs, dst_abs)
            return MutationResult(True, f"Renamed {source} -> {destination}.")
        except OSError as error:
            return MutationResult(False, f"Failed to rename {source} -> {destination}: {error}")


__all__ = [
    "DirEntry",
    "DirListing",
    "EntryKind",
    "FileMatches",
    "FileStat",
    "FilesystemLifecycle",
    "MutationResult",
    "StaleVersion",
    "content_version",
]
