"""Persistent file selections and safe, local file comparisons.

The Nautilus extension only records paths here. Reading metadata and file
contents happens after the user explicitly opens the comparison window.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterable

from .config import (
    _atomic_write_text,
    _file_lock,
    _load_json_file_unlocked,
    list_profiles,
)
from .paths import COMPARISON_SELECTION_FILE, ensure_runtime_dirs
from .rc import rc_call
from .util import expand_path


SELECTION_SCHEMA = 1
HASH_ALGORITHM = "sha256"
HASH_CHUNK_SIZE = 1024 * 1024
REMOTE_HASH_WORKERS = 4
LOCAL_MODTIME_PRECISION_NS = 1_000
HASH_PRIORITY = (
    "sha256",
    "sha1",
    "md5",
    "quickxor",
    "dropbox",
    "blake3",
    "crc32",
)


def normalize_hash_algorithm(value: object) -> str:
    compact = "".join(char for char in str(value).casefold() if char.isalnum())
    aliases = {
        "sha256": "sha256",
        "sha1": "sha1",
        "md5": "md5",
        "quickxor": "quickxor",
        "quickxorhash": "quickxor",
        "dropbox": "dropbox",
        "dropboxhash": "dropbox",
    }
    return aliases.get(compact, compact)


def hash_algorithm_label(algorithm: str) -> str:
    normalized = normalize_hash_algorithm(algorithm)
    labels = {
        "sha256": "SHA-256",
        "sha1": "SHA-1",
        "md5": "MD5",
        "quickxor": "QUICKXOR",
        "dropbox": "DROPBOX",
        "blake3": "BLAKE3",
        "crc32": "CRC32",
    }
    return labels.get(normalized, normalized.upper())


def _hash_priority(algorithm: str) -> tuple[int, str]:
    normalized = normalize_hash_algorithm(algorithm)
    try:
        return HASH_PRIORITY.index(normalized), normalized
    except ValueError:
        return len(HASH_PRIORITY), normalized


def _normalized_paths(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Return unique absolute paths while preserving their input order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in paths:
        path = os.path.abspath(os.path.expanduser(os.fspath(value)))
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def load_marked_paths() -> list[str]:
    ensure_runtime_dirs()
    with _file_lock(COMPARISON_SELECTION_FILE, exclusive=False):
        data = _load_json_file_unlocked(
            COMPARISON_SELECTION_FILE,
            {"schema": SELECTION_SCHEMA, "paths": []},
        )
    paths = data.get("paths", []) if isinstance(data, dict) else []
    return _normalized_paths(paths) if isinstance(paths, list) else []


def _save_marked_paths(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    normalized = _normalized_paths(paths)
    ensure_runtime_dirs()
    with _file_lock(COMPARISON_SELECTION_FILE, exclusive=True):
        _atomic_write_text(
            COMPARISON_SELECTION_FILE,
            json.dumps(
                {"schema": SELECTION_SCHEMA, "paths": normalized},
                indent=2,
                ensure_ascii=False,
            ),
        )
    return normalized


def mark_paths(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Add paths to the comparison pool without touching the files."""
    existing = load_marked_paths()
    return _save_marked_paths([*existing, *paths])


def unmark_paths(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    remove = set(_normalized_paths(paths))
    return _save_marked_paths(
        path for path in load_marked_paths() if path not in remove
    )


def clear_marked_paths() -> None:
    _save_marked_paths(())


@dataclass
class FileDetails:
    path: str
    source_path: str = ""
    relative_path: str = ""
    size: int | None = None
    modified: datetime | None = None
    digest: str | None = None
    hash_algorithm: str = ""
    hashes: dict[str, str] = field(default_factory=dict)
    hash_sources: dict[str, str] = field(default_factory=dict)
    modified_precision_ns: int = LOCAL_MODTIME_PRECISION_NS
    error: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name

    def add_hash(self, algorithm: str, digest: str, source: str) -> None:
        normalized = normalize_hash_algorithm(algorithm)
        value = str(digest).strip().casefold()
        if not normalized or not value:
            return
        self.hashes[normalized] = value
        self.hash_sources[normalized] = source
        preferred = self.preferred_hash_algorithm()
        if preferred:
            self.hash_algorithm = preferred
            self.digest = self.hashes[preferred]

    def preferred_hash_algorithm(self) -> str:
        if not self.hashes:
            return ""
        return min(self.hashes, key=_hash_priority)


@dataclass
class ComparisonRow:
    key: str
    reference: FileDetails | None
    selected: FileDetails | None
    matched_hash_algorithm: str = ""
    byte_match: bool | None = None
    byte_error: str = ""

    def status(self) -> str:
        if self.reference is None:
            return "missing_reference"
        if self.selected is None:
            return "missing_selected"
        if self.reference.error or self.selected.error:
            return "error"
        if self.byte_error:
            return "error"
        if self.byte_match is True:
            return "byte_identical"
        if self.byte_match is False:
            return "byte_different"
        if self.matched_hash_algorithm:
            attributes = self.matching_attributes()
            if not attributes["hash"]:
                return "different"
            if all(attributes.values()):
                return "identical"
            return "same_content"
        if (
            self.reference.size == self.selected.size
            and modification_times_match(self.reference, self.selected)
        ):
            return "metadata_match"
        return "different"

    def matching_attributes(self) -> dict[str, bool]:
        reference = self.reference
        selected = self.selected
        if reference is None or selected is None:
            return {
                "hash": False,
                "size": False,
                "modified": False,
                "name": False,
            }
        algorithm = normalize_hash_algorithm(self.matched_hash_algorithm)
        return {
            "hash": bool(
                algorithm
                and reference.hashes.get(algorithm)
                and reference.hashes.get(algorithm)
                == selected.hashes.get(algorithm)
            ),
            "size": (
                reference.size is not None
                and reference.size == selected.size
            ),
            "modified": modification_times_match(reference, selected),
            "name": reference.name == selected.name,
        }


@dataclass
class FolderComparison:
    """A lazy-to-render comparison of two directory sources."""

    reference_path: str
    selected_path: str
    rows: list[ComparisonRow]
    reference_size: int
    selected_size: int

    @property
    def matching_count(self) -> int:
        return sum(
            1
            for row in self.rows
            if row.reference is not None
            and row.selected is not None
            and row.matching_attributes()["hash"]
        )

    @property
    def identical(self) -> bool:
        return all(
            row.reference is not None
            and row.selected is not None
            and row.matching_attributes()["hash"]
            and row.matching_attributes()["size"]
            for row in self.rows
        )

    def matching_attributes(self) -> dict[str, bool]:
        paired = [
            row
            for row in self.rows
            if row.reference is not None and row.selected is not None
        ]
        complete = len(paired) == len(self.rows)
        return {
            "hash": bool(paired) and complete and all(
                row.matching_attributes()["hash"] for row in paired
            ),
            "size": complete and all(
                row.matching_attributes()["size"] for row in paired
            ),
            "modified": complete and all(
                row.matching_attributes()["modified"] for row in paired
            ),
            "byte": bool(paired) and complete and all(
                row.byte_match is True for row in paired
            ),
        }

    def matched_hash_algorithms(self) -> tuple[str, ...]:
        return tuple(sorted({
            normalize_hash_algorithm(row.matched_hash_algorithm)
            for row in self.rows
            if row.matched_hash_algorithm
            and row.matching_attributes()["hash"]
        }, key=_hash_priority))


@dataclass
class PoolComparison:
    """Results for one shared pool of files and recursive directories."""

    rows: list[ComparisonRow]
    folders: list[FolderComparison]
    file_count: int


def modification_times_match(
    reference: FileDetails,
    selected: FileDetails,
) -> bool:
    if reference.modified is None or selected.modified is None:
        return False
    tolerance_ns = max(
        reference.modified_precision_ns,
        selected.modified_precision_ns,
        LOCAL_MODTIME_PRECISION_NS,
    )
    difference_ns = abs(
        (reference.modified - selected.modified).total_seconds()
    ) * 1_000_000_000
    return difference_ns <= tolerance_ns


def _file_details(path: str) -> FileDetails:
    result = FileDetails(path=path, source_path=path)
    try:
        metadata = Path(path).stat()
        if not stat.S_ISREG(metadata.st_mode):
            result.error = "not_a_regular_file"
            return result
        result.size = int(metadata.st_size)
        result.modified = datetime.fromtimestamp(metadata.st_mtime)
    except OSError as error:
        result.error = str(error)
    return result


def _expanded_file_details(
    paths: Iterable[str],
) -> list[FileDetails]:
    result: list[FileDetails] = []
    for value in paths:
        path = Path(value)
        try:
            is_directory = path.is_dir()
        except OSError:
            is_directory = False
        if not is_directory:
            details = _file_details(str(path))
            details.source_path = str(path)
            result.append(details)
            continue
        try:
            walker = os.walk(path, followlinks=False)
            for directory, directory_names, file_names in walker:
                directory_names.sort(key=str.casefold)
                file_names.sort(key=str.casefold)
                for name in file_names:
                    child = Path(directory) / name
                    details = _file_details(str(child))
                    details.source_path = str(path)
                    details.relative_path = child.relative_to(path).as_posix()
                    result.append(details)
        except OSError as error:
            details = FileDetails(
                path=str(path),
                source_path=str(path),
                relative_path=".",
                error=str(error),
            )
            result.append(details)
    return result


def create_pool_rows(
    source_paths: Iterable[str | os.PathLike[str]],
) -> list[ComparisonRow]:
    """Expand every pool source and include each physical path only once.

    Sources remain independent in the persisted pool.  Deduplication here only
    prevents a file selected both directly and through a parent directory from
    being read and hashed twice.
    """
    sources = _normalized_paths(source_paths)
    details_by_path: dict[str, FileDetails] = {}
    for source in sources:
        for details in _expanded_file_details((source,)):
            key = os.path.abspath(details.path)
            details_by_path.setdefault(key, details)
    return [
        ComparisonRow(details.relative_path or details.name, details, None)
        for details in details_by_path.values()
    ]


def create_comparison_rows(
    marked_paths: Iterable[str | os.PathLike[str]],
    selected_paths: Iterable[str | os.PathLike[str]],
) -> list[ComparisonRow]:
    """Read file metadata, expanding selected directories recursively."""
    marked = _normalized_paths(marked_paths)
    selected = _normalized_paths(selected_paths)
    marked_details = _expanded_file_details(marked)
    selected_details = _expanded_file_details(selected)
    return [
        *[
            ComparisonRow(details.relative_path or details.name, details, None)
            for details in marked_details
        ],
        *[
            ComparisonRow(details.relative_path or details.name, None, details)
            for details in selected_details
        ],
    ]


def is_directory_comparison(
    marked_paths: Iterable[str | os.PathLike[str]],
    selected_paths: Iterable[str | os.PathLike[str]],
) -> bool:
    marked = _normalized_paths(marked_paths)
    selected = _normalized_paths(selected_paths)
    return (
        len(marked) == 1
        and len(selected) == 1
        and Path(marked[0]).is_dir()
        and Path(selected[0]).is_dir()
    )


@dataclass(frozen=True)
class _RemoteHashTarget:
    details: FileDetails
    profile_id: str
    profile_label: str
    remote_spec: str
    remote_path: str


def _remote_hash_targets(
    details: Iterable[FileDetails],
    profiles: dict[str, dict[str, Any]],
) -> list[_RemoteHashTarget]:
    mounts: list[tuple[Path, str, dict[str, Any]]] = []
    for profile_id, profile in profiles.items():
        mount_value = expand_path(str(profile.get("mount_dir", ""))).strip()
        remote_spec = str(profile.get("remote_spec", "")).strip()
        if not mount_value or not remote_spec:
            continue
        mounts.append(
            (Path(os.path.abspath(mount_value)), profile_id, profile)
        )
    mounts.sort(key=lambda item: len(item[0].parts), reverse=True)

    targets: list[_RemoteHashTarget] = []
    for item in details:
        if item.error or item.name.casefold().endswith(".link.html"):
            continue
        local_path = Path(os.path.abspath(item.path))
        for mount_dir, profile_id, profile in mounts:
            try:
                relative = local_path.relative_to(mount_dir)
            except ValueError:
                continue
            targets.append(
                _RemoteHashTarget(
                    details=item,
                    profile_id=profile_id,
                    profile_label=str(profile.get("label", profile_id)),
                    remote_spec=str(profile.get("remote_spec", "")).strip(),
                    remote_path=relative.as_posix(),
                )
            )
            break
    return targets


def _profile_has_pending_uploads(stats: dict[str, Any]) -> bool:
    disk_cache = stats.get("diskCache")
    if not isinstance(disk_cache, dict):
        return False
    return any(
        int(disk_cache.get(key, 0) or 0) > 0
        for key in ("uploadsInProgress", "uploadsQueued")
    )


def load_provider_hashes(
    rows: Iterable[ComparisonRow],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    rc_caller: Callable[..., dict[str, Any] | None] = rc_call,
) -> None:
    """Read cheap provider hashes through the running mount's local RC API.

    Slow backend hashes and hashes for a profile with pending writes are
    deliberately skipped.  Those files are handled by the local SHA-256
    fallback instead.
    """
    all_details = [
        details
        for row in rows
        for details in (row.reference, row.selected)
        if details is not None
    ]
    targets = _remote_hash_targets(
        all_details,
        list_profiles() if profiles is None else profiles,
    )
    grouped: dict[str, list[_RemoteHashTarget]] = {}
    for target in targets:
        grouped.setdefault(target.profile_id, []).append(target)

    requests: list[_RemoteHashTarget] = []
    for profile_id, profile_targets in grouped.items():
        stats = rc_caller(profile_id, "vfs/stats", timeout=5.0)
        if stats is None or _profile_has_pending_uploads(stats):
            continue
        remote_spec = profile_targets[0].remote_spec
        info = rc_caller(
            profile_id,
            "operations/fsinfo",
            {"fs": remote_spec},
            timeout=10.0,
        )
        if not isinstance(info, dict):
            continue
        features = info.get("Features")
        if isinstance(features, dict) and bool(features.get("SlowHash")):
            continue
        supported = {
            normalize_hash_algorithm(value)
            for value in info.get("Hashes", [])
            if normalize_hash_algorithm(value)
        }
        if not supported:
            continue
        try:
            precision = max(1, int(info.get("Precision", 1) or 1))
        except (TypeError, ValueError):
            precision = LOCAL_MODTIME_PRECISION_NS
        for target in profile_targets:
            target.details.modified_precision_ns = precision
            requests.append(target)

    def load_target(target: _RemoteHashTarget) -> None:
        result = rc_caller(
            target.profile_id,
            "operations/stat",
            {
                "fs": target.remote_spec,
                "remote": target.remote_path,
                "opt": {
                    "filesOnly": True,
                    "showHash": True,
                    "noMimeType": True,
                },
            },
            timeout=15.0,
        )
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict):
            return
        try:
            remote_size = int(item.get("Size"))
        except (TypeError, ValueError):
            return
        if target.details.size is None or remote_size != target.details.size:
            return
        hashes = item.get("Hashes")
        if not isinstance(hashes, dict):
            return
        source = f"remote:{target.profile_label}"
        for algorithm, digest in hashes.items():
            target.details.add_hash(str(algorithm), str(digest or ""), source)

    if requests:
        with ThreadPoolExecutor(max_workers=REMOTE_HASH_WORKERS) as executor:
            list(executor.map(load_target, requests))


def _shared_matching_hash(
    reference: FileDetails,
    selected: FileDetails,
) -> str:
    common = reference.hashes.keys() & selected.hashes.keys()
    for algorithm in sorted(common, key=_hash_priority):
        if reference.hashes[algorithm] == selected.hashes[algorithm]:
            return algorithm
    return ""


def _preferred_common_hash_algorithm(
    reference: FileDetails,
    selected: FileDetails,
) -> str:
    common = reference.hashes.keys() & selected.hashes.keys()
    return min(common, key=_hash_priority) if common else ""


def _match_details(
    references: Iterable[FileDetails],
    selected: Iterable[FileDetails],
) -> tuple[list[ComparisonRow], list[FileDetails], list[FileDetails]]:
    available = sorted(selected, key=lambda item: item.path)
    pairs: list[ComparisonRow] = []
    unmatched_references: list[FileDetails] = []
    for reference in sorted(references, key=lambda item: item.path):
        options: list[tuple[FileDetails, str]] = []
        if not reference.error and reference.size is not None:
            for candidate in available:
                if candidate.error or candidate.size != reference.size:
                    continue
                algorithm = _shared_matching_hash(reference, candidate)
                if algorithm:
                    options.append((candidate, algorithm))
        if not options:
            unmatched_references.append(reference)
            continue
        options.sort(
            key=lambda option: (
                option[0].name != reference.name,
                not modification_times_match(reference, option[0]),
                _hash_priority(option[1]),
                option[0].path.casefold(),
            )
        )
        selected_file, algorithm = options[0]
        available.remove(selected_file)
        pairs.append(
            ComparisonRow(
                reference.name,
                reference,
                selected_file,
                matched_hash_algorithm=algorithm,
            )
        )
    return pairs, unmatched_references, available


def _finalize_rows(
    pairs: Iterable[ComparisonRow],
    unmatched_references: Iterable[FileDetails],
    unmatched_selected: Iterable[FileDetails],
) -> list[ComparisonRow]:
    pairs = list(pairs)
    unmatched_references = list(unmatched_references)
    unmatched_selected = list(unmatched_selected)
    pairs.sort(key=lambda row: row.key.casefold())
    unmatched_references.sort(key=lambda item: item.path)
    unmatched_selected.sort(key=lambda item: item.path)
    return [
        *pairs,
        *[
            ComparisonRow(details.name, details, None)
            for details in unmatched_references
        ],
        *[
            ComparisonRow(details.name, None, details)
            for details in unmatched_selected
        ],
    ]


def match_comparison_rows(rows: Iterable[ComparisonRow]) -> list[ComparisonRow]:
    """Match every file against the opposite set using common hashes."""
    rows = list(rows)
    references = [row.reference for row in rows if row.reference is not None]
    selected = [row.selected for row in rows if row.selected is not None]
    pairs, unmatched_references, unmatched_selected = _match_details(
        references,
        selected,
    )
    return _finalize_rows(pairs, unmatched_references, unmatched_selected)


def _match_folder_rows(
    references: Iterable[FileDetails],
    selected: Iterable[FileDetails],
) -> tuple[list[ComparisonRow], list[FileDetails], list[FileDetails]]:
    selected_by_path = {
        details.relative_path: details
        for details in selected
        if details.relative_path
    }
    pairs: list[ComparisonRow] = []
    unmatched_references: list[FileDetails] = []
    used_selected_ids: set[int] = set()
    for reference in sorted(
        references,
        key=lambda details: details.relative_path.casefold(),
    ):
        candidate = selected_by_path.get(reference.relative_path)
        if candidate is None:
            unmatched_references.append(reference)
            continue
        used_selected_ids.add(id(candidate))
        pairs.append(
            ComparisonRow(
                reference.relative_path,
                reference,
                candidate,
            )
        )
    unmatched_selected = [
        details for details in selected
        if id(details) not in used_selected_ids
    ]
    return pairs, unmatched_references, unmatched_selected


def _calculate_sha256_details(details_list: Iterable[FileDetails]) -> None:
    seen: set[int] = set()
    for details in details_list:
        if id(details) in seen:
            continue
        seen.add(id(details))
        if details.error or HASH_ALGORITHM in details.hashes:
            continue
        try:
            digest = hashlib.sha256()
            with Path(details.path).open("rb") as stream:
                while chunk := stream.read(HASH_CHUNK_SIZE):
                    digest.update(chunk)
            details.add_hash(HASH_ALGORITHM, digest.hexdigest(), "local")
        except OSError as error:
            details.error = str(error)


def calculate_hashes(
    rows: Iterable[ComparisonRow],
) -> None:
    """Calculate SHA-256 hashes locally after an explicit comparison."""
    _calculate_sha256_details(
        details
        for row in rows
        for details in (row.reference, row.selected)
        if details is not None
    )


def compare_comparison_rows(
    rows: Iterable[ComparisonRow],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    rc_caller: Callable[..., dict[str, Any] | None] = rc_call,
    match_relative_paths: bool = False,
) -> list[ComparisonRow]:
    """Use provider hashes first and SHA-256 only where it is needed."""
    rows = list(rows)
    load_provider_hashes(rows, profiles=profiles, rc_caller=rc_caller)
    references = [row.reference for row in rows if row.reference is not None]
    selected = [row.selected for row in rows if row.selected is not None]
    if match_relative_paths:
        pairs, unmatched_references, unmatched_selected = _match_folder_rows(
            references,
            selected,
        )
        for row in pairs:
            reference = row.reference
            candidate = row.selected
            if reference is None or candidate is None:
                continue
            algorithm = _preferred_common_hash_algorithm(
                reference,
                candidate,
            )
            if not algorithm:
                _calculate_sha256_details((reference, candidate))
                algorithm = _preferred_common_hash_algorithm(
                    reference,
                    candidate,
                )
            row.matched_hash_algorithm = algorithm
        return _finalize_rows(
            pairs,
            unmatched_references,
            unmatched_selected,
        )

    pairs, unmatched_references, unmatched_selected = _match_details(
        references,
        selected,
    )

    fallback_references: set[int] = set()
    fallback_selected: set[int] = set()
    for reference in unmatched_references:
        if reference.error or reference.size is None:
            continue
        for candidate in unmatched_selected:
            if candidate.error or candidate.size != reference.size:
                continue
            if reference.hashes.keys() & candidate.hashes.keys():
                continue
            fallback_references.add(id(reference))
            fallback_selected.add(id(candidate))

    _calculate_sha256_details(
        [
            reference
            for reference in unmatched_references
            if id(reference) in fallback_references
        ]
    )
    _calculate_sha256_details(
        [
            candidate
            for candidate in unmatched_selected
            if id(candidate) in fallback_selected
        ]
    )
    fallback_pairs, remaining_references, remaining_selected = _match_details(
        unmatched_references,
        unmatched_selected,
    )
    return _finalize_rows(
        [*pairs, *fallback_pairs],
        remaining_references,
        remaining_selected,
    )


def _pool_details(rows: Iterable[ComparisonRow]) -> list[FileDetails]:
    details_by_path: dict[str, FileDetails] = {}
    for row in rows:
        for details in (row.reference, row.selected):
            if details is None:
                continue
            details_by_path.setdefault(os.path.abspath(details.path), details)
    return list(details_by_path.values())


def _common_pool_hash(details: list[FileDetails]) -> str:
    if not details:
        return ""
    common = set(details[0].hashes)
    for item in details[1:]:
        common.intersection_update(item.hashes)
    return min(common, key=_hash_priority) if common else ""


def _folder_comparisons(
    source_paths: Iterable[str | os.PathLike[str]],
    details: Iterable[FileDetails],
) -> list[FolderComparison]:
    directories = [
        Path(path)
        for path in _normalized_paths(source_paths)
        if Path(path).is_dir()
    ]
    all_details = list(details)
    results: list[FolderComparison] = []
    for index, reference_root in enumerate(directories):
        reference_files: dict[str, FileDetails] = {}
        for item in all_details:
            try:
                relative = Path(item.path).relative_to(reference_root).as_posix()
            except ValueError:
                continue
            reference_files[relative] = item
        for selected_root in directories[index + 1:]:
            selected_files: dict[str, FileDetails] = {}
            for item in all_details:
                try:
                    relative = Path(item.path).relative_to(selected_root).as_posix()
                except ValueError:
                    continue
                selected_files[relative] = item
            folder_rows: list[ComparisonRow] = []
            for relative in sorted(
                reference_files.keys() | selected_files.keys(),
                key=str.casefold,
            ):
                reference = reference_files.get(relative)
                selected = selected_files.get(relative)
                algorithm = (
                    _shared_matching_hash(reference, selected)
                    if reference is not None and selected is not None
                    else ""
                )
                folder_rows.append(
                    ComparisonRow(
                        relative,
                        reference,
                        selected,
                        matched_hash_algorithm=algorithm,
                    )
                )
            results.append(
                FolderComparison(
                    reference_path=str(reference_root),
                    selected_path=str(selected_root),
                    rows=folder_rows,
                    reference_size=sum(
                        item.size or 0 for item in reference_files.values()
                    ),
                    selected_size=sum(
                        item.size or 0 for item in selected_files.values()
                    ),
                )
            )
    results.sort(
        key=lambda item: (
            not item.identical,
            -item.matching_count,
            item.reference_path.casefold(),
            item.selected_path.casefold(),
        )
    )
    return results


def compare_pool_rows(
    rows: Iterable[ComparisonRow],
    source_paths: Iterable[str | os.PathLike[str]],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    rc_caller: Callable[..., dict[str, Any] | None] = rc_call,
) -> PoolComparison:
    """Compare every expanded file with the whole shared comparison pool.

    Files are first bucketed by size.  A provider hash avoids reading file
    contents when every candidate in a bucket exposes a common algorithm;
    otherwise SHA-256 is calculated only for that candidate bucket.  Duplicate
    groups are rendered as an anchor plus each other member, avoiding quadratic
    rows while still accounting for every copy.
    """
    source_paths = _normalized_paths(source_paths)
    rows = list(rows)
    load_provider_hashes(rows, profiles=profiles, rc_caller=rc_caller)
    details = _pool_details(rows)
    by_size: dict[int, list[FileDetails]] = {}
    for item in details:
        if item.error or item.size is None:
            continue
        by_size.setdefault(item.size, []).append(item)

    duplicate_rows: list[ComparisonRow] = []
    duplicate_ids: set[int] = set()
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        algorithm = _common_pool_hash(candidates)
        if not algorithm:
            _calculate_sha256_details(candidates)
            algorithm = HASH_ALGORITHM
        groups: dict[str, list[FileDetails]] = {}
        for item in candidates:
            digest = item.hashes.get(algorithm, "")
            if digest:
                groups.setdefault(digest, []).append(item)
        for copies in groups.values():
            if len(copies) < 2:
                continue
            copies.sort(key=lambda item: item.path.casefold())
            anchor = copies[0]
            duplicate_ids.update(id(item) for item in copies)
            for copy in copies[1:]:
                duplicate_rows.append(
                    ComparisonRow(
                        anchor.name,
                        anchor,
                        copy,
                        matched_hash_algorithm=algorithm,
                    )
                )

    duplicate_rows.sort(
        key=lambda row: (
            not all(row.matching_attributes().values()),
            not row.matching_attributes()["modified"],
            not row.matching_attributes()["name"],
            row.reference.path.casefold() if row.reference else "",
            row.selected.path.casefold() if row.selected else "",
        )
    )
    unique_rows = [
        ComparisonRow(item.name, item, None)
        for item in sorted(details, key=lambda value: value.path.casefold())
        if id(item) not in duplicate_ids
    ]
    return PoolComparison(
        rows=[*duplicate_rows, *unique_rows],
        folders=_folder_comparisons(source_paths, details),
        file_count=len(details),
    )


def _files_equal_byte_by_byte(reference: str, selected: str) -> bool:
    with Path(reference).open("rb") as reference_stream:
        with Path(selected).open("rb") as selected_stream:
            while True:
                reference_chunk = reference_stream.read(HASH_CHUNK_SIZE)
                selected_chunk = selected_stream.read(HASH_CHUNK_SIZE)
                if reference_chunk != selected_chunk:
                    return False
                if not reference_chunk:
                    return True


def compare_matching_pairs_byte_by_byte(
    rows: Iterable[ComparisonRow],
) -> int:
    """Verify hash-matched pairs by reading both files again."""
    checked = 0
    for row in rows:
        reference = row.reference
        selected = row.selected
        if reference is None or selected is None:
            continue
        if reference.error or selected.error:
            continue
        algorithm = normalize_hash_algorithm(row.matched_hash_algorithm)
        if not algorithm or reference.size != selected.size:
            continue
        if (
            reference.hashes.get(algorithm) is None
            or reference.hashes.get(algorithm)
            != selected.hashes.get(algorithm)
        ):
            continue
        checked += 1
        row.byte_error = ""
        try:
            row.byte_match = _files_equal_byte_by_byte(
                reference.path,
                selected.path,
            )
        except OSError as error:
            row.byte_match = None
            row.byte_error = str(error)
    return checked
