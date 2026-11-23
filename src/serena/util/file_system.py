# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import pathspec
from pathspec import PathSpec

from solidlsp.util.descriptor_exhaustion import DescriptorExhaustion

log = logging.getLogger(__name__)


def write_file_atomic(path: str, content: str, *, encoding: str, newline: str | None = None) -> None:
    """
    Write ``content`` to ``path`` atomically: the content is written to a temporary file in the
    same directory first, then swapped into place with ``os.replace``. A plain
    ``open(path, "w")`` is not atomic: it truncates the file before the new content is complete,
    so a crash, an out-of-memory kill, or a disk-full error partway through the write leaves
    ``path`` holding neither the old content nor the new one.

    :param path: the path to write to
    :param content: the text content to write
    :param encoding: the encoding to use for the write
    :param newline: passed through to the underlying ``open()`` call to control newline translation
    """
    # ``open(path, "w")`` follows symlinks and writes through to the target, whereas replacing the
    # link path itself would swap the link out for a regular file and leave its target holding the
    # old content. Resolving first keeps this a drop-in replacement, and puts the temporary file in
    # the destination's real directory, which is where it has to be for the rename to be atomic.
    path = os.path.realpath(path)
    target_dir = os.path.dirname(path) or "."
    try:
        existing_mode: int | None = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        existing_mode = None
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(content)
        # mkstemp creates the temp file with mode 0600 regardless of umask, which would silently
        # tighten an existing file's permissions (e.g. 0644 -> 0600) on replace. Restore the
        # original mode, or fall back to what a plain open(path, "w") would have produced for a
        # new file (0666 masked by the process umask).
        os.chmod(tmp_path, existing_mode if existing_mode is not None else _new_file_mode())
        _replace_with_retry(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _new_file_mode() -> int:
    """The mode a plain ``open(path, "w")`` would give a brand-new file: 0o666 masked by the
    process umask. Reading the umask requires setting it, so the previous value is restored
    immediately after.
    """
    current_umask = os.umask(0o022)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _replace_with_retry(src: str, dst: str, *, attempts: int = 10, delay_s: float = 0.05) -> None:
    """``os.replace(src, dst)`` with a short retry on a Windows sharing violation: on Windows the
    atomic rename fails with ``PermissionError`` if another process momentarily holds ``dst`` open
    (e.g. a second Serena process reading the same memory or source file). A brief bounded retry
    rides out that contention; the temp file is still complete, so this never falls back to a
    non-atomic write.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_s)


# Characters meaningful to pathspec's gitignore grammar: glob wildcards, bracket expressions,
# the escape character itself, and '!'/'#' which change a whole pattern's meaning when they
# are its first character. Backslash-escaping them makes a literal name safe to interpolate.
# Escape surrounding whitespace too, so pathspec does not strip it from literal names.
_GITIGNORE_PATTERN_SPECIAL_CHARS_RE = re.compile(r"([\\*?\[\]!#])")


def _escape_gitignore_path_component(component: str) -> str:
    """Escape gitignore/pathspec pattern metacharacters in a single path component (no
    separators) so it is matched as a literal name rather than as glob syntax.
    """
    component = _GITIGNORE_PATTERN_SPECIAL_CHARS_RE.sub(r"\\\1", component)
    return re.sub(r"(^\s|\s$)", r"\\\1", component)


class ScanResult(NamedTuple):
    """Result of scanning a directory."""

    directories: list[str]
    files: list[str]


def scan_directory(
    path: str,
    recursive: bool = False,
    relative_to: str | None = None,
    is_ignored_dir: Callable[[str], bool] | None = None,
    is_ignored_file: Callable[[str], bool] | None = None,
    skipped_links: list[str] | None = None,
) -> ScanResult:
    """
    :param path: the path to scan
    :param recursive: whether to recursively scan subdirectories
    :param relative_to: the path to which the results should be relative to; if None, provide absolute paths
    :param is_ignored_dir: a function with which to determine whether the given directory (abs. path) shall be ignored
    :param is_ignored_file: a function with which to determine whether the given file (abs. path) shall be ignored
    :param skipped_links: a list to which the path (in the form of the results) of every non-ignored symbolic link to a
        directory is appended; such a link is never entered, so a link to an ancestor cannot make the scan repeat the tree
    :return: the list of directories and files
    """
    if is_ignored_file is None:
        is_ignored_file = lambda x: False
    if is_ignored_dir is None:
        is_ignored_dir = lambda x: False

    files = []
    directories = []

    abs_path = os.path.abspath(path)
    rel_base = os.path.abspath(relative_to) if relative_to else None

    try:
        with os.scandir(abs_path) as entries:
            for entry in entries:
                try:
                    entry_path = entry.path

                    if rel_base:
                        try:
                            result_path = os.path.relpath(entry_path, rel_base)
                        except:
                            log.debug(f"Skipping entry due to relative path conversion error: {entry.path}")
                            continue
                    else:
                        result_path = entry_path

                    if entry.is_file():
                        if not is_ignored_file(entry_path):
                            files.append(result_path)
                    elif entry.is_dir(follow_symlinks=False):
                        if not is_ignored_dir(entry_path):
                            directories.append(result_path)
                            if recursive:
                                sub_result = scan_directory(
                                    entry_path,
                                    recursive=True,
                                    relative_to=relative_to,
                                    is_ignored_dir=is_ignored_dir,
                                    is_ignored_file=is_ignored_file,
                                    skipped_links=skipped_links,
                                )
                                files.extend(sub_result.files)
                                directories.extend(sub_result.directories)
                    elif entry.is_dir():
                        # a symbolic link to a directory: reported, never entered
                        if skipped_links is not None and not is_ignored_dir(entry_path):
                            skipped_links.append(result_path)
                except PermissionError as ex:
                    # Skip files/directories that cannot be accessed due to permission issues
                    log.debug(f"Skipping entry due to permission error: {entry.path}", exc_info=ex)
                    continue
    except PermissionError as ex:
        # Skip the entire directory if it cannot be accessed
        log.debug(f"Skipping directory due to permission error: {abs_path}", exc_info=ex)
        return ScanResult([], [])

    return ScanResult(directories, files)


def dir_entry_from_path(path: str) -> os.DirEntry:
    parent, name = os.path.split(os.path.normpath(path))
    for entry in os.scandir(parent or "."):
        if entry.name == name:
            return entry
    raise FileNotFoundError(f"No such file or directory: '{path}'")


class EnteredDirectories:
    """
    The real paths of the directories a walk that follows symbolic links to directories has entered.

    The walk enters a directory, real or reached through a link, only if its real path is not yet among them, and it
    records only the directories it enters (or, for a search that stops at a directory it finds, that it finds). A walk
    defers the links it meets until it has walked every directory it reaches without one, then admits each link in the
    order met. So a link to the walk's root, to an ancestor or to any directory the walk reaches by real directories adds
    nothing, and that directory is named by its own path; a link to a directory the walk skips or never reaches is
    followed, and of several links to one such directory only the first met is entered.
    """

    def __init__(self, *roots: str) -> None:
        """
        :param roots: the directories the walk starts in, which count as entered
        """
        self._real_paths: set[str] = {os.path.realpath(root) for root in roots}

    def admit(self, subdirectories: list[os.DirEntry]) -> list[os.DirEntry]:
        """
        Admits the subdirectories of one directory, or a deferred link, that the walk shall enter, recording them as
        entered.

        :param subdirectories: the real subdirectories of one directory the walk would descend into, or one link to a
            directory
        :return: the admitted subdirectories, in the order given
        """
        admitted: set[int] = set()
        for subdirectory in sorted(subdirectories, key=lambda entry: entry.name):
            real_path = os.path.realpath(subdirectory.path)
            if real_path in self._real_paths:
                continue
            self._real_paths.add(real_path)
            admitted.add(id(subdirectory))

        return [subdirectory for subdirectory in subdirectories if id(subdirectory) in admitted]


def walk_dir_entries(
    top, topdown=True, onerror=None, followlinks=False, recursive=True, is_ignored_dir: Callable[[os.DirEntry], bool] = lambda x: False
) -> Iterator[tuple[str, list[os.DirEntry], list[os.DirEntry]]]:
    """
    Variant of os.walk which yields os.DirEntry objects instead of strings.
    This allows for more efficient file system access, as it avoids additional stat calls.

    The directories contained in the result are exactly those that are descended into, i.e. ignored
    directories and (unless `followlinks` is set) symbolic links to directories are not contained.
    A recursive walk that follows links enters each real directory at most once (see :class:`EnteredDirectories`): it
    first walks every directory it reaches without a link, then the links it met, in the order met and those met inside a
    followed link included, entering each whose real directory it has not entered. So a directory reached both ways is
    walked under its own path whatever the names, and a directory reached only through links under the first link's path.
    A followed link is reported in a result of its own, ``(path of the directory holding it, [link], [])``, given before
    the link's own results when ``topdown`` is set and after them otherwise.

    :param top: the root directory to start walking from
    :param topdown: if True, yield the directory before its subdirectories; if False, yield the subdirectories before the directory
    :param onerror: a function to call with an OSError instance if an error occurs while accessing a directory
    :param followlinks: if True, follow symbolic links to directories; if False, do not follow them
    :param recursive: whether to recursively walk into subdirectories; if False, only the top directory is yielded
    :param is_ignored_dir: a function to determine whether a directory should be ignored; it takes an os.DirEntry and returns True if
        the directory should be ignored
    :return: an iterator yielding tuples of (directory path, list of subdirectory entries, list of file entries)
    """
    if not (followlinks and recursive):
        yield from _walk_dir_entries(top, topdown, onerror, followlinks, recursive, is_ignored_dir, None, None)
        return

    entered = EnteredDirectories(top)
    deferred_links: list[tuple[str, os.DirEntry]] = []
    yield from _walk_dir_entries(top, topdown, onerror, followlinks, recursive, is_ignored_dir, entered, deferred_links)

    # the links met, those met inside a followed link included, each followed unless its real directory was entered
    index = 0
    while index < len(deferred_links):
        holder, link = deferred_links[index]
        index += 1
        followed = entered.admit([link])
        if not followed:
            continue
        if topdown:
            yield holder, followed, []
        yield from _walk_dir_entries(link.path, topdown, onerror, followlinks, recursive, is_ignored_dir, entered, deferred_links)
        if not topdown:
            yield holder, followed, []


def _walk_dir_entries(
    top,
    topdown: bool,
    onerror: Callable[[OSError], None] | None,
    followlinks: bool,
    recursive: bool,
    is_ignored_dir: Callable[[os.DirEntry], bool],
    entered: EnteredDirectories | None,
    deferred_links: list[tuple[str, os.DirEntry]] | None,
) -> Iterator[tuple[str, list[os.DirEntry], list[os.DirEntry]]]:
    """
    The walk of :func:`walk_dir_entries` from one directory.

    :param entered: the directories the walk has entered, when it follows links recursively; None when it does not
    :param deferred_links: the links to directories met so far, each with the path of the directory holding it, to which
        this walk adds those it meets instead of descending into them, when it follows links recursively; None when it
        does not
    """
    try:
        with os.scandir(top) as it:
            entries = list(it)
    except OSError as error:
        DescriptorExhaustion.report(error, f"scanning the directory {top}")
        if onerror is not None:
            onerror(error)
        return

    dirs = []
    files = []

    # a walk that follows links meets them in name order, so that which link names a directory does not depend on the
    # order the file system lists entries in
    if entered is not None:
        entries.sort(key=lambda entry: entry.name)

    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False

        if not is_dir:
            files.append(entry)
        elif (followlinks or not entry.is_symlink()) and not is_ignored_dir(entry):
            dirs.append(entry)

    # the real subdirectories whose real directory this walk has not entered yet; the links are deferred
    if entered is not None and deferred_links is not None:
        deferred_links.extend((top, entry) for entry in dirs if entry.is_symlink())
        dirs = entered.admit([entry for entry in dirs if not entry.is_symlink()])

    if topdown:
        yield top, dirs, files

    if recursive:
        for entry in dirs:
            yield from _walk_dir_entries(entry.path, topdown, onerror, followlinks, recursive, is_ignored_dir, entered, deferred_links)

    if not topdown:
        yield top, dirs, files


@dataclass
class GitignoreSpec:
    file_path: str
    """Path to the gitignore file."""
    patterns: list[str] = field(default_factory=list)
    """List of patterns from the gitignore file.
    The patterns are adjusted based on the gitignore file location.
    """
    pathspec: PathSpec = field(init=False)
    """Compiled PathSpec object for pattern matching."""

    def __post_init__(self) -> None:
        """Initialize the PathSpec from patterns."""
        self.pathspec = PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, self.patterns)

    def matches(self, relative_path: str) -> bool:
        """
        Check if the given path matches any pattern in this gitignore spec.

        :param relative_path: Path to check (should be relative to repo root)
        :return: True if path matches any pattern
        """
        return match_path(relative_path, self.pathspec, root_path=os.path.dirname(self.file_path))


class GitignoreParser:
    """
    Parser for the gitignore files of a repository.

    A path's ignore decision reads only the .gitignore files in the directories on its ancestor chain, from the
    repository root down to the path itself, since a .gitignore governs only its own subtree. Each directory's
    .gitignore is read on its first use and memoised; the repository tree is never listed.
    """

    def __init__(self, repo_root: str, *, prune_spec: PathSpec | None = None) -> None:
        """
        Initialize the parser for a repository.

        :param repo_root: Root directory of the repository
        :param prune_spec: Configured ignore patterns; the .gitignore of a directory they match, and of every directory
            below it, is never read
        """
        self.repo_root = os.path.abspath(repo_root)
        self._prune_spec = prune_spec
        self._spec_by_directory: dict[str, GitignoreSpec | None] = {}

    def _directory_spec(self, directory: str) -> GitignoreSpec | None:
        """
        :param directory: a directory relative to the repository root, with forward slashes ("" for the root)
        :return: the non-empty spec of the directory's .gitignore, or None if it has none, it is empty or it
            cannot be read; memoised per directory
        """
        if directory in self._spec_by_directory:
            return self._spec_by_directory[directory]
        gitignore_dir = os.path.join(self.repo_root, directory) if directory else self.repo_root
        gitignore_path = os.path.join(gitignore_dir, ".gitignore")
        spec: GitignoreSpec | None
        try:
            with open(gitignore_path, encoding="utf-8") as f:
                content = f.read()
        except PermissionError as ex:
            if not directory:
                # The repository root itself cannot be read — e.g. a macOS privacy-protected
                # folder such as ~/Desktop, ~/Documents or ~/Downloads. Fail with an actionable
                # message rather than an opaque crash or a silently-empty parser.
                raise PermissionError(
                    f"serena cannot read the folder '{self.repo_root}': macOS is blocking access to it. "
                    f"Desktop, Documents, and Downloads are protected by the system's privacy "
                    f"settings, and serena does not have permission to open them."
                ) from ex
            log.debug(f"Skipping unreadable .gitignore {gitignore_path}: {ex}")
            spec = None
        except (OSError, UnicodeDecodeError):
            spec = None
        else:
            log.info("Processing .gitignore file: %s", gitignore_path)
            spec = GitignoreSpec(gitignore_path, self._parse_gitignore_content(content, gitignore_dir))
            if not spec.patterns:
                spec = None
        self._spec_by_directory[directory] = spec
        return spec

    def _relative_path(self, path: str, is_dir: bool | None) -> str | None:
        """
        :param path: a path, absolute or relative to the repository root
        :param is_dir: whether the path is a directory, where the caller knows; None determines it from the filesystem
        :return: the path relative to the repository root with forward slashes, ending in "/" for a directory, or None
            if it cannot be made relative to the repository root
        """
        if os.path.isabs(path):
            try:
                rel_path = os.path.relpath(path, self.repo_root)
            except ValueError as e:
                log.info("Ignoring path '%s' which is outside of the repository root (%s)", path, e)
                return None
        else:
            rel_path = path
        rel_path = rel_path.replace(os.sep, "/")
        if not rel_path.endswith("/"):
            if is_dir is None:
                is_dir = os.path.isdir(os.path.join(self.repo_root, rel_path))
            if is_dir:
                rel_path = rel_path + "/"
        return rel_path

    def specs_of(self, path: str, is_dir: bool | None = None) -> list[GitignoreSpec]:
        """
        The specs of the .gitignore files governing a path: those in the directories on its ancestor chain, from the
        repository root down to the path itself if it is a directory, root first. Reading stops at a directory the
        prune spec matches, at a directory the specs above it ignore, and at the root's .git directory; a path
        outside the repository root is governed by the root's .gitignore alone.

        :param path: a path, absolute or relative to the repository root
        :param is_dir: whether the path is a directory, where the caller knows; None determines it from the filesystem
        :return: the governing specs, root first
        """
        rel_path = self._relative_path(path, is_dir)
        if rel_path is None:
            return []
        normalized = os.path.normpath(rel_path).replace(os.sep, "/")
        directories = [] if normalized in (".", "..") or normalized.startswith("../") else normalized.split("/")
        if directories and not rel_path.endswith("/"):
            directories = directories[:-1]
        specs: list[GitignoreSpec] = []
        root_spec = self._directory_spec("")
        if root_spec is not None:
            specs.append(root_spec)
        directory = ""
        for component in directories:
            directory = f"{directory}/{component}" if directory else component
            if directory == ".git":
                break
            directory_path = directory + "/"
            if self._prune_spec is not None and match_path(directory_path, self._prune_spec, root_path=self.repo_root):
                break
            if any(spec.matches(directory_path) for spec in specs):
                break
            spec = self._directory_spec(directory)
            if spec is not None:
                specs.append(spec)
        return specs

    # Backport of upstream #1806: keep filesystem names literal in patterns.
    def _parse_gitignore_content(self, content: str, gitignore_dir: str) -> list[str]:
        """
        Parse gitignore content and adjust patterns based on the gitignore file location.

        :param content: Content of the .gitignore file
        :param gitignore_dir: Directory containing the .gitignore file (absolute path)
        :return: List of adjusted patterns
        """
        patterns = []

        # Get the relative path from repo root to the gitignore directory. Normalize to
        # forward slashes immediately: os.path.relpath returns native separators, but
        # gitignore/pathspec patterns are always POSIX-style, and on Windows os.sep is
        # backslash -- the same character pathspec uses as its escape character. Building
        # the pattern with any raw os.sep would make a later blanket backslash->slash
        # normalization indistinguishable from the escape backslashes below.
        rel_dir = os.path.relpath(gitignore_dir, self.repo_root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""

        # rel_dir is a filesystem path, but the code below interpolates it into pattern
        # position; escape each of its components so a directory name containing pattern
        # metacharacters (e.g. "***") is matched literally instead of as glob syntax.
        rel_dir_pattern = "/".join(_escape_gitignore_path_component(part) for part in rel_dir.split("/")) if rel_dir else rel_dir

        for line in content.splitlines():
            # Strip trailing whitespace (but preserve leading whitespace for now)
            line = line.rstrip()

            # Skip empty lines and comments
            if not line or line.lstrip().startswith("#"):
                continue

            # Store whether this is a negation pattern
            is_negation = line.startswith("!")
            if is_negation:
                line = line[1:]

            # Strip leading/trailing whitespace after removing negation
            line = line.strip()

            if not line:
                continue

            # Handle escaped characters at the beginning
            if line.startswith(("\\#", "\\!")):
                line = line[1:]

            # Determine if pattern is anchored to the gitignore directory and remove leading slash for processing
            is_anchored = line.startswith("/")
            if is_anchored:
                line = line[1:]

            # Adjust pattern based on gitignore file location. Joined with a literal "/",
            # never os.path.join/os.sep: gitignore patterns are always POSIX-style, and on
            # Windows os.sep is backslash, indistinguishable from the escape backslashes
            # rel_dir_pattern may already contain.
            if rel_dir:
                if is_anchored:
                    # Anchored patterns are relative to the gitignore directory
                    adjusted_pattern = f"{rel_dir_pattern}/{line}"
                else:
                    # Non-anchored patterns can match anywhere below the gitignore directory
                    # We need to preserve this behavior
                    if line.startswith("**/"):
                        # Even if pattern starts with **, it should still be scoped to the subdirectory
                        adjusted_pattern = f"{rel_dir_pattern}/{line}"
                    else:
                        # Add the directory prefix but also allow matching in subdirectories
                        adjusted_pattern = f"{rel_dir_pattern}/**/{line}"
            else:
                if is_anchored:
                    # Anchored patterns in root should only match at root level
                    # Add leading slash back to indicate root-only matching
                    adjusted_pattern = "/" + line
                else:
                    # Non-anchored patterns can match anywhere
                    adjusted_pattern = line

            # Re-add negation if needed
            if is_negation:
                adjusted_pattern = "!" + adjusted_pattern

            patterns.append(adjusted_pattern)

        return patterns

    def should_ignore(self, path: str) -> bool:
        """
        Check if a path should be ignored based on the gitignore rules.

        :param path: Path to check (absolute or relative to repo_root)
        :return: True if the path should be ignored, False otherwise
        """
        rel_path = self._relative_path(path, None)
        if rel_path is None:
            # a path that cannot be made relative to the repository root is outside it, so it is ignored
            return True

        # Ignore paths inside .git
        if Path(rel_path).parts[:1] == (".git",):
            return True

        return any(spec.matches(rel_path) for spec in self.specs_of(rel_path, is_dir=rel_path.endswith("/")))

    def reload(self) -> None:
        """Forget every .gitignore read so far, so that each is read again on its next use."""
        self._spec_by_directory = {}


class IgnoreSpecs:
    """
    The ignore spec that governs each path of a project: the configured patterns, then the patterns of the .gitignore
    files on the path's ancestor chain (if those are honoured), then the configured patterns again, so that neither a
    .gitignore negation can re-include a configured exclusion nor a .gitignore rule narrow a configured re-inclusion.
    The configured patterns also prune the chain: the .gitignore of a directory they exclude is never read.
    Each directory's spec is composed on first use and memoised.
    """

    def __init__(self, root: str, configured_patterns: list[str], read_gitignore_files: bool) -> None:
        """
        :param root: the project root
        :param configured_patterns: the configured ignore patterns, in order (gitignore syntax, forward slashes)
        :param read_gitignore_files: whether the .gitignore files of the project are honoured
        """
        self._configured_patterns = list(configured_patterns)
        self._configured_spec = PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, self._configured_patterns)
        self._gitignore_parser = GitignoreParser(root, prune_spec=self._configured_spec) if read_gitignore_files else None
        self._spec_by_gitignore_files: dict[tuple[str, ...], PathSpec] = {}

    def spec_of(self, relative_path: str, is_dir: bool | None = None) -> PathSpec:
        """
        :param relative_path: a path relative to the project root
        :param is_dir: whether the path is a directory, where the caller knows; None determines it from the filesystem
        :return: the ignore spec governing the path
        """
        if self._gitignore_parser is None:
            return self._configured_spec
        gitignore_specs = self._gitignore_parser.specs_of(relative_path, is_dir=is_dir)
        key = tuple(spec.file_path for spec in gitignore_specs)
        spec = self._spec_by_gitignore_files.get(key)
        if spec is None:
            patterns = [*self._configured_patterns]
            for gitignore_spec in gitignore_specs:
                patterns.extend(gitignore_spec.patterns)
            patterns.extend(self._configured_patterns)
            spec = PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, patterns)
            self._spec_by_gitignore_files[key] = spec
        return spec


def match_path(relative_path: str, path_spec: PathSpec, root_path: str = "", is_dir: bool | None = None) -> bool:
    """
    Match a relative path against a given pathspec. Just pathspec.match_file() is not enough,
    we need to do some massaging to fix issues with pathspec matching.

    :param relative_path: relative path to match against the pathspec
    :param path_spec: the pathspec to match against
    :param root_path: the root path from which the relative path is derived
    :param is_dir: whether the path is a directory, where the caller already knows; passing it avoids
        an `os.path.isdir` call. `None` determines it from the filesystem.
    :return:
    """
    if str(relative_path) in {"", "."}:
        return False

    normalized_path = str(relative_path).replace(os.path.sep, "/")

    # We can have patterns like /src/..., which would only match corresponding paths from the repo root
    # Unfortunately, pathspec can't know whether a relative path is relative to the repo root or not,
    # so it will never match src/...
    # The fix is to just always assume that the input path is relative to the repo root and to
    # prefix it with /.
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path

    # pathspec can't handle the matching of directories if they don't end with a slash!
    # see https://github.com/cpburnz/python-pathspec/issues/89
    # A path that already ends with '/' needs no stat: the slash is only ever appended, so the
    # directory check is irrelevant for it (this is the hot path of gitignore discovery).
    if not normalized_path.endswith("/"):
        if is_dir is None:
            abs_path = os.path.abspath(os.path.join(root_path, relative_path))
            is_dir = os.path.isdir(abs_path)
        if is_dir:
            normalized_path = normalized_path + "/"
    return path_spec.match_file(normalized_path)
