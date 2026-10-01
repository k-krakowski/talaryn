"""GTK window used for comparisons started from the Nautilus extension."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import threading
from typing import Iterable

from .comparison import (
    HASH_PRIORITY,
    ComparisonRow,
    FileDetails,
    FolderComparison,
    PoolComparison,
    calculate_hashes,
    clear_marked_paths,
    compare_matching_pairs_byte_by_byte,
    compare_pool_rows,
    create_pool_rows,
    hash_algorithm_label,
    load_marked_paths,
    mark_paths,
    modification_times_match,
    normalize_hash_algorithm,
    unmark_paths,
)
from .config import list_profiles
from .i18n import tr
from .icons import DEFAULT_REMOTE_ICON_NAME, remote_icon_path
from .mount import show_file_in_nautilus
from .paths import APP_ICON_NAME, CACHE_DIR, COMPARISON_APP_ID
from .util import expand_path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GnomeDesktop", "4.0")
    from gi.repository import GnomeDesktop  # type: ignore  # noqa: E402
except (ValueError, ImportError):
    GnomeDesktop = None  # type: ignore


THUMBNAIL_QUERY_LIMIT = 2
TAG_TEXT_LIMIT = 8
TAG_COLUMN_WIDTH = 88
FILE_PREVIEW_SIZE = 80
METADATA_ROW_HEIGHT = 26
FOLDER_TREE_FILE_WIDTH = 260
FOLDER_TREE_TAG_WIDTH = 82
COMPARISON_CSS = b"""
.comparison-tag {
  border-radius: 6px;
  padding: 2px 7px;
  font-size: 0.78em;
  font-weight: 600;
  color: white;
}
.comparison-tag-identical { background-color: #198754; }
.comparison-tag-hash { background-color: #7c3aed; }
.comparison-tag-metadata { background-color: #2563a8; }
.comparison-tag-byte { background-color: #0f766e; }
.comparison-tag-error { background-color: #b3261e; }
.comparison-copy-button {
  min-width: 24px;
  min-height: 24px;
  padding: 0;
}
"""
_COMPARISON_STYLE_PROVIDER: Gtk.CssProvider | None = None


@dataclass
class ComparisonTreeNode:
    name: str
    relative_path: str
    children: dict[str, "ComparisonTreeNode"] = field(default_factory=dict)
    row: ComparisonRow | None = None
    reference_count: int = 0
    selected_count: int = 0
    reference_size: int = 0
    selected_size: int = 0

    @property
    def is_directory(self) -> bool:
        return bool(self.children)

    def ordered_children(self) -> list["ComparisonTreeNode"]:
        return sorted(
            self.children.values(),
            key=lambda child: (
                not child.is_directory,
                child.name.casefold(),
            ),
        )


def _comparison_tree(rows: Iterable[ComparisonRow]) -> ComparisonTreeNode:
    """Build a directory hierarchy without reading the filesystem again."""
    root = ComparisonTreeNode("", "")
    for row in rows:
        relative = str(row.key).replace("\\", "/").strip("/")
        parts = tuple(
            part
            for part in PurePosixPath(relative).parts
            if part not in ("", ".", "..")
        )
        if not parts:
            details = row.reference or row.selected
            if details is None:
                continue
            parts = (details.name,)
        node = root
        ancestry = [root]
        current_parts: list[str] = []
        for part in parts:
            current_parts.append(part)
            relative_path = "/".join(current_parts)
            node = node.children.setdefault(
                part,
                ComparisonTreeNode(part, relative_path),
            )
            ancestry.append(node)
        node.row = row
        for ancestor in ancestry:
            if row.reference is not None:
                ancestor.reference_count += 1
                ancestor.reference_size += row.reference.size or 0
            if row.selected is not None:
                ancestor.selected_count += 1
                ancestor.selected_size += row.selected.size or 0
    return root


@dataclass
class FolderComparisonGroup:
    """A connected set of pair comparisons rendered as one folder matrix."""

    paths: tuple[str, ...]
    comparisons: tuple[FolderComparison, ...]
    files: dict[str, dict[str, FileDetails]]

    @property
    def identical(self) -> bool:
        return bool(self.comparisons) and all(
            comparison.identical for comparison in self.comparisons
        )

    @property
    def matching_count(self) -> int:
        count = 0
        for files in self.files.values():
            if len(files) != len(self.paths):
                continue
            details = [files[path] for path in self.paths]
            common = set(details[0].hashes)
            for item in details[1:]:
                common.intersection_update(item.hashes)
            if any(
                len({item.hashes.get(algorithm, "") for item in details}) == 1
                and bool(details[0].hashes.get(algorithm))
                for algorithm in common
            ):
                count += 1
        return count

    def source_size(self, path: str) -> int:
        return sum(
            details[path].size or 0
            for details in self.files.values()
            if path in details
        )

    def matching_attributes(self) -> dict[str, bool]:
        attributes = [
            comparison.matching_attributes()
            for comparison in self.comparisons
        ]
        return {
            key: bool(attributes) and all(item[key] for item in attributes)
            for key in ("hash", "size", "modified", "byte")
        }

    def matched_hash_algorithms(self) -> tuple[str, ...]:
        return tuple(sorted({
            algorithm
            for comparison in self.comparisons
            for algorithm in comparison.matched_hash_algorithms()
        }))


@dataclass
class FolderGroupTreeNode:
    name: str
    relative_path: str
    children: dict[str, "FolderGroupTreeNode"] = field(default_factory=dict)
    files: dict[str, FileDetails] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    @property
    def is_directory(self) -> bool:
        return bool(self.children)

    def ordered_children(self) -> list["FolderGroupTreeNode"]:
        return sorted(
            self.children.values(),
            key=lambda child: (
                not child.is_directory,
                child.name.casefold(),
            ),
        )


@dataclass
class FolderContentGroup:
    files: dict[str, tuple[FileDetails, ...]]
    algorithms: tuple[str, ...]


@dataclass
class FolderContentAnalysis:
    matches: list[FolderContentGroup]
    conflicts: dict[str, dict[str, FileDetails]]
    unique: dict[str, dict[str, FileDetails]]
    same_content: bool

    @property
    def matching_file_count(self) -> int:
        return sum(
            len(details)
            for match in self.matches
            for details in match.files.values()
        )


def _hash_sort_key(algorithm: str) -> tuple[int, str]:
    normalized = normalize_hash_algorithm(algorithm)
    try:
        return HASH_PRIORITY.index(normalized), normalized
    except ValueError:
        return len(HASH_PRIORITY), normalized


def _folder_content_analysis(
    group: FolderComparisonGroup,
) -> FolderContentAnalysis:
    """Group files by equal content, independently of names and paths."""
    records: list[tuple[str, FileDetails]] = []
    seen: set[tuple[str, int]] = set()
    for by_source in group.files.values():
        for source, details in by_source.items():
            identity = (source, id(details))
            if identity in seen:
                continue
            seen.add(identity)
            records.append((source, details))

    parents = list(range(len(records)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    hash_buckets: dict[tuple[int, str, str], list[int]] = {}
    for index, (_source, details) in enumerate(records):
        if details.size is None or details.error:
            continue
        for algorithm, digest in details.hashes.items():
            if digest:
                hash_buckets.setdefault(
                    (details.size, normalize_hash_algorithm(algorithm), digest),
                    [],
                ).append(index)
    for indexes in hash_buckets.values():
        anchor = indexes[0]
        for index in indexes[1:]:
            union(anchor, index)

    components: dict[int, list[int]] = {}
    for index in range(len(records)):
        components.setdefault(find(index), []).append(index)

    matches: list[FolderContentGroup] = []
    matched_ids: set[int] = set()
    for indexes in components.values():
        sources = {records[index][0] for index in indexes}
        if len(sources) < 2:
            continue
        details_list = [records[index][1] for index in indexes]
        common = set(details_list[0].hashes)
        for details in details_list[1:]:
            common.intersection_update(details.hashes)
        algorithms = tuple(sorted(
            (
                algorithm
                for algorithm in common
                if len({
                    details.hashes.get(algorithm, "")
                    for details in details_list
                }) == 1
                and bool(details_list[0].hashes.get(algorithm))
            ),
            key=_hash_sort_key,
        ))
        by_source: dict[str, list[FileDetails]] = {}
        for index in indexes:
            source, details = records[index]
            by_source.setdefault(source, []).append(details)
            matched_ids.add(id(details))
        matches.append(
            FolderContentGroup(
                {
                    source: tuple(sorted(
                        items,
                        key=lambda item: item.path.casefold(),
                    ))
                    for source, items in by_source.items()
                },
                algorithms,
            )
        )

    remaining: dict[str, dict[str, FileDetails]] = {}
    for relative, by_source in group.files.items():
        filtered = {
            source: details
            for source, details in by_source.items()
            if id(details) not in matched_ids
        }
        if filtered:
            remaining[relative] = filtered
    conflicts = {
        relative: files
        for relative, files in remaining.items()
        if len(files) > 1
    }
    conflict_ids = {
        id(details)
        for files in conflicts.values()
        for details in files.values()
    }
    unique = {
        relative: {
            source: details
            for source, details in files.items()
            if id(details) not in conflict_ids
        }
        for relative, files in remaining.items()
        if any(id(details) not in conflict_ids for details in files.values())
    }
    all_matched = len(matched_ids) == len(records)
    same_multiplicity = all(
        len({len(match.files.get(path, ())) for path in group.paths}) == 1
        for match in matches
    )
    matches.sort(
        key=lambda match: tuple(
            details.path.casefold()
            for path in group.paths
            for details in match.files.get(path, ())
        )
    )
    return FolderContentAnalysis(
        matches,
        conflicts,
        unique,
        bool(matches) and all_matched and same_multiplicity,
    )


def _folder_comparison_groups(
    comparisons: Iterable[FolderComparison],
    source_order: Iterable[str] = (),
) -> list[FolderComparisonGroup]:
    """Merge connected pair comparisons into multi-folder display groups."""
    pending = list(comparisons)
    order = list(source_order)
    groups: list[FolderComparisonGroup] = []
    while pending:
        component = [pending.pop(0)]
        paths = {
            component[0].reference_path,
            component[0].selected_path,
        }
        changed = True
        while changed:
            changed = False
            for comparison in pending[:]:
                pair = {
                    comparison.reference_path,
                    comparison.selected_path,
                }
                if paths.isdisjoint(pair):
                    continue
                pending.remove(comparison)
                component.append(comparison)
                paths.update(pair)
                changed = True
        ordered_paths = tuple(
            [path for path in order if path in paths]
            + sorted(paths.difference(order), key=str.casefold)
        )
        files: dict[str, dict[str, FileDetails]] = {}
        for comparison in component:
            for row in comparison.rows:
                by_source = files.setdefault(row.key, {})
                if row.reference is not None:
                    by_source[comparison.reference_path] = row.reference
                if row.selected is not None:
                    by_source[comparison.selected_path] = row.selected
        groups.append(
            FolderComparisonGroup(
                ordered_paths,
                tuple(component),
                files,
            )
        )
    return groups


def _folder_group_tree(
    group: FolderComparisonGroup,
    files_by_relative: dict[str, dict[str, FileDetails]] | None = None,
) -> FolderGroupTreeNode:
    root = FolderGroupTreeNode("", "")
    source_files = (
        group.files if files_by_relative is None else files_by_relative
    )
    for relative, files in source_files.items():
        parts = tuple(
            part
            for part in PurePosixPath(relative.replace("\\", "/")).parts
            if part not in ("", ".", "..")
        )
        if not parts:
            continue
        node = root
        ancestry = [root]
        current_parts: list[str] = []
        for part in parts:
            current_parts.append(part)
            relative_path = "/".join(current_parts)
            node = node.children.setdefault(
                part,
                FolderGroupTreeNode(part, relative_path),
            )
            ancestry.append(node)
        node.files.update(files)
        for ancestor in ancestry:
            for source, details in files.items():
                ancestor.counts[source] = ancestor.counts.get(source, 0) + 1
                ancestor.sizes[source] = (
                    ancestor.sizes.get(source, 0) + (details.size or 0)
                )
    return root


def _install_comparison_styles() -> None:
    global _COMPARISON_STYLE_PROVIDER
    if _COMPARISON_STYLE_PROVIDER is not None:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(COMPARISON_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _COMPARISON_STYLE_PROVIDER = provider


def _human_size(value: int | None) -> str:
    if value is None:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return f"{value} B"


def _modified(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _comparison_targets(
    selected_paths: Iterable[str],
    marked_paths: Iterable[str],
) -> list[str]:
    """Merge paths into one deduplicated comparison pool."""
    result: list[str] = []
    seen: set[str] = set()
    for value in (*marked_paths, *selected_paths):
        path = os.path.abspath(os.path.expanduser(os.fsdecode(value)))
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _profile_for_path(
    path_value: str,
    profiles: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    """Return the profile with the longest mount path containing a file."""
    path = Path(os.path.abspath(expand_path(path_value)))
    matches: list[tuple[int, dict[str, object]]] = []
    for profile in profiles.values():
        mount_value = expand_path(str(profile.get("mount_dir", ""))).strip()
        if not mount_value:
            continue
        mount_dir = Path(os.path.abspath(mount_value))
        try:
            path.relative_to(mount_dir)
        except ValueError:
            continue
        matches.append((len(mount_dir.parts), profile))
    if not matches:
        return None
    return max(matches, key=lambda match: match[0])[1]


class ComparisonWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, selected_paths: Iterable[str]) -> None:
        super().__init__(application=app, title=tr("comparison_title"))
        _install_comparison_styles()
        self.set_icon_name(APP_ICON_NAME)
        initial_paths = list(selected_paths)
        if initial_paths:
            mark_paths(initial_paths)
        self.rows: list[ComparisonRow] = []
        self.folder_comparisons: list[FolderComparison] = []
        self._running = False
        self._marked_paths: tuple[str, ...] = ()
        self._refresh_pending = False
        self._pool_only_mode = False
        self._expanded_folder_comparisons: set[tuple[str, ...]] = set()
        self._expanded_source_folders: set[str] = set()
        self._expanded_tree_nodes: set[tuple[str, str]] = set()
        self._selection_watch_source = 0
        self._thumbnail_queue: deque[
            tuple[Gio.File, Gtk.Image, str, str]
        ] = deque()
        self._thumbnail_queries_active = 0
        self._thumbnail_pump_scheduled = False
        self._hash_help_window: Adw.Window | None = None
        self._profiles = list_profiles()
        self.thumbnail_factory = None
        if GnomeDesktop is not None:
            try:
                self.thumbnail_factory = (
                    GnomeDesktop.DesktopThumbnailFactory.new(
                        GnomeDesktop.DesktopThumbnailSize.NORMAL
                    )
                )
            except Exception:
                self.thumbnail_factory = None

        self.set_default_size(1180, 720)
        self.set_size_request(760, 440)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.clear_button = Gtk.Button(label=tr("comparison_clear_marked"))
        self.clear_button.add_css_class("destructive-action")
        self.clear_button.connect("clicked", self._on_clear_marked)
        header.pack_start(self.clear_button)
        self.byte_compare_button = Gtk.Button(
            label=tr("comparison_compare_bytes")
        )
        self.byte_compare_button.set_tooltip_text(
            tr("comparison_compare_bytes_help")
        )
        self.byte_compare_button.set_sensitive(False)
        self.byte_compare_button.connect(
            "clicked",
            self._start_byte_comparison,
        )
        header.pack_start(self.byte_compare_button)
        hash_help = Gtk.Button(icon_name="help-about-symbolic")
        hash_help.add_css_class("flat")
        hash_help.set_tooltip_text(tr("comparison_hash_help_title"))
        hash_help.connect("clicked", self._show_hash_help)
        header.pack_end(hash_help)
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)
        toolbar.add_top_bar(header)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)

        self.progress = Gtk.Label(xalign=0)
        self.progress.set_wrap(True)
        content.append(self.progress)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.list_box)
        content.append(scroll)

        toolbar.set_content(content)
        self.set_content(toolbar)
        self._start_comparison()
        self._selection_watch_source = GLib.timeout_add(
            750,
            self._watch_marked_paths,
        )
        self.connect("close-request", self._on_close_request)

    def _show_hash_help(self, *_args) -> None:
        if self._hash_help_window is not None:
            self._hash_help_window.present()
            return
        window = Adw.Window(
            title=tr("comparison_hash_help_title"),
            transient_for=self,
            modal=True,
        )
        self._hash_help_window = window
        window.connect("close-request", self._on_hash_help_closed)
        window.set_default_size(520, 230)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)
        icon = Gtk.Image(icon_name="dialog-information-symbolic")
        icon.set_pixel_size(40)
        content.append(icon)
        notice = Gtk.Label(label=tr("comparison_hash_notice"), xalign=0)
        notice.set_wrap(True)
        notice.set_max_width_chars(62)
        content.append(notice)
        toolbar.set_content(content)
        window.set_content(toolbar)
        window.present()

    def _on_hash_help_closed(self, *_args) -> bool:
        self._hash_help_window = None
        return False

    def _clear_rows(self) -> None:
        child = self.list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child

    @staticmethod
    def _cell(
        text: str,
        *,
        dim: bool = False,
        ellipsize: bool = False,
        ellipsize_mode=Pango.EllipsizeMode.MIDDLE,
        hexpand: bool = True,
        max_width: int = 45,
    ) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0, yalign=0)
        label.set_wrap(not ellipsize)
        if ellipsize:
            label.set_ellipsize(ellipsize_mode)
            label.set_tooltip_text(text)
        label.set_selectable(True)
        label.set_hexpand(hexpand)
        if max_width > 0:
            label.set_max_width_chars(max_width)
        if dim:
            label.add_css_class("dim-label")
        return label

    @staticmethod
    def _metadata_marker(
        tooltip: str,
        *,
        icon_name: str | None = None,
        gicon: Gio.Icon | None = None,
        symbol: str | None = None,
    ) -> Gtk.Widget:
        if symbol is not None:
            marker = Gtk.Label(label=symbol)
            marker.add_css_class("heading")
            marker.set_size_request(18, -1)
        elif gicon is not None:
            marker = Gtk.Image.new_from_gicon(gicon)
            marker.set_pixel_size(18)
            marker.set_size_request(18, 18)
        else:
            marker = Gtk.Image(icon_name=icon_name)
            marker.set_pixel_size(18)
        marker.set_valign(Gtk.Align.CENTER)
        marker.set_tooltip_text(tooltip)
        return marker

    def _metadata_row(
        self,
        value: str,
        tooltip: str,
        *,
        icon_name: str | None = None,
        gicon: Gio.Icon | None = None,
        symbol: str | None = None,
        copyable: bool = False,
        expand: bool = True,
        max_width: int = 26,
        ellipsize_mode=Pango.EllipsizeMode.MIDDLE,
    ) -> Gtk.Box:
        row = Gtk.Box(spacing=8)
        row.set_hexpand(expand)
        row.set_valign(Gtk.Align.CENTER)
        row.set_size_request(-1, METADATA_ROW_HEIGHT)
        row.append(
            self._metadata_marker(
                tooltip,
                icon_name=icon_name,
                gicon=gicon,
                symbol=symbol,
            )
        )
        label = self._cell(
            value,
            ellipsize=True,
            ellipsize_mode=ellipsize_mode,
            hexpand=expand,
            max_width=max_width,
        )
        label.set_valign(Gtk.Align.CENTER)
        row.append(label)
        if copyable and value and value != "…":
            copy = Gtk.Button(icon_name="edit-copy-symbolic")
            copy.add_css_class("flat")
            copy.add_css_class("comparison-copy-button")
            copy.set_valign(Gtk.Align.CENTER)
            copy.set_tooltip_text(tr("comparison_copy_value"))
            copy.connect("clicked", self._copy_text, value)
            row.append(copy)
        return row

    def _path_icon(
        self,
        details: FileDetails,
    ) -> tuple[str | None, Gio.Icon | None]:
        profile = _profile_for_path(details.path, self._profiles)
        if profile is None:
            return "folder-open-symbolic", None
        icon_file = remote_icon_path(
            str(profile.get("icon", DEFAULT_REMOTE_ICON_NAME))
        )
        if not icon_file:
            return "folder-remote-symbolic", None
        return (
            None,
            Gio.FileIcon.new(Gio.File.new_for_path(icon_file)),
        )

    def _path_metadata_row(
        self,
        details: FileDetails,
        *,
        expand: bool,
        max_width: int,
    ) -> Gtk.Box:
        icon_name, gicon = self._path_icon(details)
        return self._metadata_row(
            str(Path(details.path).parent),
            tr("comparison_path"),
            icon_name=icon_name,
            gicon=gicon,
            copyable=True,
            expand=expand,
            max_width=max_width,
        )

    @staticmethod
    def _copy_text(_button, value: str) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set_text(value)

    def _attach_file_context_menu(
        self,
        widget: Gtk.Widget,
        details: FileDetails,
    ) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect(
            "released",
            self._on_file_right_click,
            widget,
            details.path,
        )
        widget.add_controller(gesture)

    def _on_file_right_click(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        widget: Gtk.Widget,
        path: str,
    ) -> None:
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1

        popover = Gtk.Popover()
        popover.set_parent(widget)
        popover.add_css_class("menu")
        popover.set_has_arrow(True)
        popover.set_autohide(True)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_pointing_to(rect)
        popover.connect("closed", self._on_file_popover_closed)

        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        menu.set_margin_top(4)
        menu.set_margin_bottom(4)
        menu.set_margin_start(4)
        menu.set_margin_end(4)
        reveal = Gtk.Button()
        reveal.add_css_class("flat")
        reveal.set_has_frame(False)
        reveal.set_halign(Gtk.Align.FILL)
        reveal.set_hexpand(True)
        button_content = Gtk.Box(spacing=10)
        button_content.set_margin_start(8)
        button_content.set_margin_end(8)
        button_content.append(Gtk.Image(icon_name="folder-open-symbolic"))
        button_content.append(
            Gtk.Label(label=tr("comparison_show_in_folder"), xalign=0)
        )
        reveal.set_child(button_content)
        reveal.connect(
            "clicked",
            self._on_show_in_folder,
            popover,
            path,
        )
        menu.append(reveal)
        popover.set_child(menu)

        def popup() -> bool:
            if popover.get_parent() is not None:
                popover.popup()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(popup, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _on_show_in_folder(
        self,
        _button: Gtk.Button,
        popover: Gtk.Popover,
        path: str,
    ) -> None:
        popover.popdown()
        GLib.idle_add(show_file_in_nautilus, path)

    @staticmethod
    def _on_file_popover_closed(popover: Gtk.Popover) -> None:
        def unparent() -> bool:
            if popover.get_parent() is not None:
                popover.unparent()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(unparent, priority=GLib.PRIORITY_DEFAULT_IDLE)

    @staticmethod
    def _display_hash(
        details: FileDetails,
        requested_algorithm: str = "",
    ) -> tuple[str, str]:
        algorithm = normalize_hash_algorithm(requested_algorithm)
        if algorithm not in details.hashes:
            algorithm = details.preferred_hash_algorithm()
        return algorithm, details.hashes.get(algorithm, "")

    def _hash_metadata_row(
        self,
        details: FileDetails,
        requested_algorithm: str = "",
        *,
        expand: bool = True,
        max_width: int = 26,
    ) -> Gtk.Box:
        algorithm, digest = self._display_hash(
            details,
            requested_algorithm,
        )
        tooltip = (
            hash_algorithm_label(algorithm)
            if algorithm
            else tr("comparison_hash")
        )
        row = Gtk.Box(spacing=8)
        row.set_hexpand(expand)
        row.set_valign(Gtk.Align.CENTER)
        row.set_size_request(-1, METADATA_ROW_HEIGHT)
        marker = self._metadata_marker(tooltip, symbol="#")
        row.append(marker)
        label = self._cell(
            digest or "…",
            ellipsize=True,
            hexpand=expand,
            max_width=max_width,
        )
        label.set_valign(Gtk.Align.CENTER)
        row.append(label)
        actions = Gtk.Box(spacing=0)
        actions.set_valign(Gtk.Align.CENTER)
        row.append(actions)
        if digest:
            self._append_hash_copy_button(actions, digest)
        elif not details.error:
            calculate = Gtk.Button(icon_name="view-refresh-symbolic")
            calculate.add_css_class("flat")
            calculate.add_css_class("comparison-copy-button")
            calculate.set_tooltip_text(tr("comparison_calculate_sha256"))
            calculate.connect(
                "clicked",
                self._on_calculate_sha256,
                details,
                marker,
                label,
                actions,
            )
            actions.append(calculate)
        return row

    def _append_hash_copy_button(
        self,
        actions: Gtk.Box,
        digest: str,
    ) -> None:
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.add_css_class("flat")
        copy.add_css_class("comparison-copy-button")
        copy.set_tooltip_text(tr("comparison_copy_value"))
        copy.connect("clicked", self._copy_text, digest)
        actions.append(copy)

    def _on_calculate_sha256(
        self,
        button: Gtk.Button,
        details: FileDetails,
        marker: Gtk.Widget,
        label: Gtk.Label,
        actions: Gtk.Box,
    ) -> None:
        button.set_sensitive(False)
        button.set_tooltip_text(tr("comparison_hash_calculating_one"))
        button.set_child(Gtk.Spinner(spinning=True))
        thread = threading.Thread(
            target=self._calculate_sha256_in_background,
            args=(details, marker, label, actions, button),
            daemon=True,
        )
        thread.start()

    def _calculate_sha256_in_background(
        self,
        details: FileDetails,
        marker: Gtk.Widget,
        label: Gtk.Label,
        actions: Gtk.Box,
        button: Gtk.Button,
    ) -> None:
        calculate_hashes((ComparisonRow(details.name, details, None),))
        GLib.idle_add(
            self._finish_sha256_calculation,
            details,
            marker,
            label,
            actions,
            button,
        )

    def _finish_sha256_calculation(
        self,
        details: FileDetails,
        marker: Gtk.Widget,
        label: Gtk.Label,
        actions: Gtk.Box,
        button: Gtk.Button,
    ) -> bool:
        actions.remove(button)
        digest = details.hashes.get("sha256", "")
        if digest:
            label.set_label(digest)
            label.set_tooltip_text(digest)
            marker.set_tooltip_text("SHA-256")
            self._append_hash_copy_button(actions, digest)
        else:
            label.set_label("—")
            label.set_tooltip_text(
                tr("comparison_hash_failed", error=details.error)
            )
            warning = Gtk.Image(icon_name="dialog-warning-symbolic")
            warning.set_tooltip_text(
                tr("comparison_hash_failed", error=details.error)
            )
            actions.append(warning)
        return GLib.SOURCE_REMOVE

    def _metadata_cell(
        self,
        details: FileDetails | None,
        hash_algorithm: str = "",
        *,
        compact_copy_actions: bool = False,
    ) -> Gtk.Widget:
        if details is None:
            return self._cell(tr("comparison_missing"), dim=True)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        content.set_hexpand(True)
        filename = self._cell(
            details.name,
            ellipsize=True,
            max_width=28,
        )
        filename.add_css_class("heading")
        content.append(filename)

        body = Gtk.Box(spacing=12)
        body.set_hexpand(True)
        thumbnail = Gtk.Image()
        thumbnail.set_pixel_size(FILE_PREVIEW_SIZE)
        thumbnail.set_size_request(FILE_PREVIEW_SIZE, FILE_PREVIEW_SIZE)
        thumbnail.set_valign(Gtk.Align.START)
        thumbnail.add_css_class("frame")
        self._load_thumbnail(thumbnail, details.path)
        body.append(thumbnail)
        metadata = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=1,
        )
        metadata.set_hexpand(True)
        metadata.set_valign(Gtk.Align.START)
        size_and_modified = Gtk.Box(
            spacing=10,
            homogeneous=False,
        )
        size_and_modified.set_halign(Gtk.Align.START)
        size_and_modified.set_size_request(-1, METADATA_ROW_HEIGHT)
        size_and_modified.append(
            self._metadata_row(
                _human_size(details.size),
                tr("comparison_size"),
                icon_name="drive-harddisk-symbolic",
                expand=False,
                max_width=10,
                ellipsize_mode=Pango.EllipsizeMode.END,
            )
        )
        size_and_modified.append(
            self._metadata_row(
                _modified(details.modified),
                tr("comparison_modified"),
                icon_name="document-open-recent-symbolic",
                expand=False,
                max_width=19,
                ellipsize_mode=Pango.EllipsizeMode.END,
            )
        )
        metadata.append(size_and_modified)
        metadata.append(
            self._hash_metadata_row(
                details,
                hash_algorithm,
                expand=not compact_copy_actions,
                max_width=-1 if compact_copy_actions else 26,
            )
        )
        metadata.append(
            self._path_metadata_row(
                details,
                expand=not compact_copy_actions,
                max_width=-1 if compact_copy_actions else 26,
            )
        )
        if details.error:
            metadata.append(
                self._metadata_row(
                    tr("comparison_error", error=details.error),
                    tr("comparison_error_label"),
                    icon_name="dialog-warning-symbolic",
                )
            )
        body.append(metadata)
        content.append(body)
        self._attach_file_context_menu(content, details)
        return content

    @staticmethod
    def _short_tag_text(text: str) -> str:
        if len(text) <= TAG_TEXT_LIMIT:
            return text
        return f"{text[:TAG_TEXT_LIMIT - 1]}…"

    @staticmethod
    def _comparison_tag(
        text: str,
        css_class: str,
        tooltip: str = "",
    ) -> Gtk.Label:
        visible_text = ComparisonWindow._short_tag_text(text)
        label = Gtk.Label(label=visible_text)
        label.add_css_class("comparison-tag")
        label.add_css_class(css_class)
        if visible_text != text:
            tooltip = f"{text}\n{tooltip}" if tooltip else text
        if tooltip:
            label.set_tooltip_text(tooltip)
        return label

    @staticmethod
    def _hash_source_text(details: FileDetails, algorithm: str) -> str:
        source = details.hash_sources.get(algorithm, "")
        if source == "local":
            return tr("comparison_hash_source_local")
        if source.startswith("remote:"):
            return tr(
                "comparison_hash_source_remote",
                profile=source.removeprefix("remote:"),
            )
        return tr("comparison_hash_source_unknown")

    def _hash_tag_tooltip(self, item: ComparisonRow) -> str:
        algorithm = normalize_hash_algorithm(item.matched_hash_algorithm)
        if not algorithm or item.reference is None or item.selected is None:
            return ""
        return tr(
            "comparison_hash_tag_tooltip",
            algorithm=hash_algorithm_label(algorithm),
            reference=self._hash_source_text(item.reference, algorithm),
            selected=self._hash_source_text(item.selected, algorithm),
        )

    def _status_cell(self, item: ComparisonRow) -> Gtk.Widget:
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=7,
        )
        content.set_size_request(TAG_COLUMN_WIDTH, -1)
        content.set_hexpand(False)
        item_status = item.status()
        attributes = item.matching_attributes()
        tags = Gtk.FlowBox()
        tags.set_selection_mode(Gtk.SelectionMode.NONE)
        tags.set_homogeneous(False)
        tags.set_max_children_per_line(2)
        tags.set_row_spacing(5)
        tags.set_column_spacing(5)
        tags.set_halign(Gtk.Align.START)
        tags.set_hexpand(False)
        if item.byte_match is True:
            tags.append(
                self._comparison_tag(
                    tr("comparison_tag_byte"),
                    "comparison-tag-byte",
                    tr("comparison_tag_byte_help"),
                )
            )
        elif item.byte_match is False:
            tags.append(
                self._comparison_tag(
                    tr("comparison_byte_different"),
                    "comparison-tag-error",
                    tr("comparison_byte_different"),
                )
            )
        if all(attributes.values()):
            tags.append(
                self._comparison_tag(
                    tr("comparison_tag_identical"),
                    "comparison-tag-identical",
                    tr("comparison_tag_identical_help"),
                )
            )
        else:
            if attributes["hash"]:
                tags.append(
                    self._comparison_tag(
                        hash_algorithm_label(item.matched_hash_algorithm),
                        "comparison-tag-hash",
                        self._hash_tag_tooltip(item),
                    )
                )
            for attribute, label_key, help_key in (
                ("size", "comparison_tag_size", "comparison_tag_size_help"),
                (
                    "modified",
                    "comparison_tag_modified",
                    "comparison_tag_modified_help",
                ),
                ("name", "comparison_tag_name", "comparison_tag_name_help"),
            ):
                if attributes[attribute]:
                    tags.append(
                        self._comparison_tag(
                            tr(label_key),
                            "comparison-tag-metadata",
                            tr(help_key),
                        )
                    )
        if item_status == "error":
            error = item.byte_error
            if not error and item.reference is not None:
                error = item.reference.error
            if not error and item.selected is not None:
                error = item.selected.error
            tags.append(
                self._comparison_tag(
                    tr("comparison_error_label"),
                    "comparison-tag-error",
                    tr("comparison_error", error=error),
                )
            )
        if tags.get_first_child() is not None:
            content.append(tags)
        return content

    def _add_row(self, item: ComparisonRow) -> None:
        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        if self._pool_only_mode:
            grid.attach(self._metadata_cell(item.reference), 0, 0, 9, 1)
        else:
            grid.attach(self._status_cell(item), 0, 0, 2, 1)
            grid.attach(
                self._metadata_cell(
                    item.reference,
                    item.matched_hash_algorithm,
                ),
                2,
                0,
                4,
                1,
            )
            grid.attach(
                self._metadata_cell(
                    item.selected,
                    item.matched_hash_algorithm,
                ),
                7,
                0,
                4,
                1,
            )
        if item.reference is not None:
            reference_source = (
                item.reference.source_path or item.reference.path
            )
            remove_reference = Gtk.Button(icon_name="window-close-symbolic")
            remove_reference.add_css_class("flat")
            remove_reference.set_tooltip_text(
                f"{tr('comparison_remove_marked')}: {reference_source}"
            )
            remove_reference.connect(
                "clicked",
                self._on_remove_marked,
                reference_source,
            )
            grid.attach(
                remove_reference,
                9 if self._pool_only_mode else 6,
                0,
                1,
                1,
            )
        if item.selected is not None and not self._pool_only_mode:
            selected_source = item.selected.source_path or item.selected.path
            remove_selected = Gtk.Button(icon_name="window-close-symbolic")
            remove_selected.add_css_class("flat")
            remove_selected.set_tooltip_text(
                f"{tr('comparison_remove_marked')}: {selected_source}"
            )
            remove_selected.connect(
                "clicked",
                self._on_remove_marked,
                selected_source,
            )
            grid.attach(remove_selected, 11, 0, 1, 1)
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.set_child(grid)
        self.list_box.append(row)

    @staticmethod
    def _matching_tag_signature(row: ComparisonRow) -> tuple[object, ...]:
        algorithm = normalize_hash_algorithm(row.matched_hash_algorithm)
        reference = row.reference
        digest = (
            reference.hashes.get(algorithm, "")
            if reference is not None and algorithm
            else ""
        )
        attributes = row.matching_attributes()
        return (
            algorithm,
            digest,
            attributes["hash"],
            attributes["size"],
            attributes["modified"],
            attributes["name"],
            row.byte_match,
            bool(row.byte_error),
        )

    @classmethod
    def _group_matching_rows(
        cls,
        rows: Iterable[ComparisonRow],
    ) -> list[tuple[ComparisonRow, list[FileDetails]]]:
        grouped: dict[
            tuple[object, ...],
            tuple[ComparisonRow, dict[str, FileDetails]],
        ] = {}
        for row in rows:
            if row.reference is None or row.selected is None:
                continue
            signature = cls._matching_tag_signature(row)
            if signature not in grouped:
                grouped[signature] = (row, {})
            _representative, details_by_path = grouped[signature]
            for details in (row.reference, row.selected):
                details_by_path.setdefault(details.path, details)
        result = [
            (
                representative,
                sorted(
                    details_by_path.values(),
                    key=lambda details: details.path.casefold(),
                ),
            )
            for representative, details_by_path in grouped.values()
        ]
        result.sort(
            key=lambda group: tuple(
                details.path.casefold() for details in group[1]
            )
        )
        return result

    def _duplicate_file_cell(
        self,
        details: FileDetails,
        hash_algorithm: str,
    ) -> Gtk.Widget:
        content = Gtk.Box(spacing=4)
        content.set_size_request(280, -1)
        content.set_hexpand(True)
        content.append(self._metadata_cell(details, hash_algorithm))
        source = details.source_path or details.path
        remove = Gtk.Button(icon_name="window-close-symbolic")
        remove.add_css_class("flat")
        remove.set_valign(Gtk.Align.START)
        remove.set_tooltip_text(
            f"{tr('comparison_remove_marked')}: {source}"
        )
        remove.connect("clicked", self._on_remove_marked, source)
        content.append(remove)
        return content

    def _add_duplicate_group_row(
        self,
        representative: ComparisonRow,
        files: Iterable[FileDetails],
    ) -> None:
        content = Gtk.Box(spacing=12)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(12)
        content.set_margin_end(12)
        status = self._status_cell(representative)
        status.set_size_request(TAG_COLUMN_WIDTH, -1)
        content.append(status)
        file_cells = Gtk.Box(spacing=12, homogeneous=True, hexpand=True)
        for details in files:
            file_cells.append(
                self._duplicate_file_cell(
                    details,
                    representative.matched_hash_algorithm,
                )
            )
        content.append(file_cells)
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.set_child(content)
        self.list_box.append(row)

    def _add_unique_row(self, item: ComparisonRow) -> None:
        details = item.reference or item.selected
        if details is None:
            return
        overlay = Gtk.Overlay()
        overlay.set_margin_top(10)
        overlay.set_margin_bottom(10)
        overlay.set_margin_start(12)
        overlay.set_margin_end(12)
        metadata = self._metadata_cell(
            details,
            compact_copy_actions=True,
        )
        metadata.set_margin_end(36)
        overlay.set_child(metadata)
        source = details.source_path or details.path
        remove = Gtk.Button(icon_name="window-close-symbolic")
        remove.add_css_class("flat")
        remove.set_halign(Gtk.Align.END)
        remove.set_valign(Gtk.Align.START)
        remove.set_tooltip_text(
            f"{tr('comparison_remove_marked')}: {source}"
        )
        remove.connect("clicked", self._on_remove_marked, source)
        overlay.add_overlay(remove)
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.set_child(overlay)
        self.list_box.append(row)

    def _load_thumbnail(self, image: Gtk.Image, path_value: str) -> None:
        path = Path(path_value)
        content_type, _uncertain = Gio.content_type_guess(path.name, None)
        try:
            image.set_from_gicon(Gio.content_type_get_icon(content_type))
        except Exception:
            image.set_from_icon_name("text-x-generic")

        file = Gio.File.new_for_path(str(path))
        uri = file.get_uri()
        cached = self._cached_thumbnail_for_uri(uri)
        if cached:
            image.set_from_file(cached)
            image.set_pixel_size(FILE_PREVIEW_SIZE)
            return
        if self.thumbnail_factory is None:
            return
        self._thumbnail_queue.append((file, image, uri, content_type))
        self._schedule_thumbnail_pump()

    @staticmethod
    def _cached_thumbnail_for_uri(uri: str) -> str | None:
        name = f"{hashlib.md5(uri.encode('utf-8')).hexdigest()}.png"
        for size in ("normal", "large", "x-large", "xx-large"):
            candidate = CACHE_DIR.parent / "thumbnails" / size / name
            if candidate.is_file():
                return str(candidate)
        return None

    def _schedule_thumbnail_pump(self) -> None:
        if self._thumbnail_pump_scheduled:
            return
        self._thumbnail_pump_scheduled = True
        GLib.idle_add(
            self._pump_thumbnail_queue,
            priority=GLib.PRIORITY_LOW,
        )

    def _pump_thumbnail_queue(self) -> bool:
        self._thumbnail_pump_scheduled = False
        while (
            self._thumbnail_queue
            and self._thumbnail_queries_active < THUMBNAIL_QUERY_LIMIT
        ):
            file, image, uri, content_type = self._thumbnail_queue.popleft()
            if image.get_root() is None:
                continue
            self._thumbnail_queries_active += 1
            try:
                file.query_info_async(
                    "time::modified",
                    Gio.FileQueryInfoFlags.NONE,
                    GLib.PRIORITY_LOW,
                    None,
                    self._on_thumbnail_info,
                    (image, uri, content_type),
                )
            except Exception:
                self._thumbnail_queries_active -= 1
        return GLib.SOURCE_REMOVE

    def _on_thumbnail_info(
        self,
        file: Gio.File,
        result: Gio.AsyncResult,
        user_data: tuple[Gtk.Image, str, str],
    ) -> None:
        image, uri, content_type = user_data
        try:
            info = file.query_info_finish(result)
            if image.get_root() is None:
                return
            mtime = int(info.get_attribute_uint64("time::modified"))
            cached = self.thumbnail_factory.lookup(uri, mtime)
            if cached:
                image.set_from_file(cached)
                image.set_pixel_size(FILE_PREVIEW_SIZE)
                return
            if not self.thumbnail_factory.can_thumbnail(
                uri,
                content_type,
                mtime,
            ):
                return
            if self.thumbnail_factory.has_valid_failed_thumbnail(uri, mtime):
                return
            self.thumbnail_factory.generate_thumbnail_async(
                uri,
                content_type,
                None,
                self._on_thumbnail_generated,
                (image, uri, mtime),
            )
        except Exception:
            pass
        finally:
            self._thumbnail_queries_active = max(
                0,
                self._thumbnail_queries_active - 1,
            )
            if self._thumbnail_queue:
                self._schedule_thumbnail_pump()

    def _on_thumbnail_generated(
        self,
        factory,
        result: Gio.AsyncResult,
        user_data: tuple[Gtk.Image, str, int],
    ) -> None:
        image, uri, mtime = user_data
        try:
            pixbuf = factory.generate_thumbnail_finish(result)
            if pixbuf is None or image.get_root() is None:
                return
            factory.save_thumbnail(pixbuf, uri, mtime, None)
            image.set_from_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
            image.set_pixel_size(FILE_PREVIEW_SIZE)
        except Exception:
            try:
                factory.create_failed_thumbnail(uri, mtime, None)
            except Exception:
                pass

    def _add_section(self, title: str) -> None:
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("heading")
        label.set_margin_top(18)
        label.set_margin_bottom(4)
        label.set_margin_start(12)
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.add_css_class("boxed-list")
        row.set_child(label)
        self.list_box.append(row)

    @staticmethod
    def _all_row_details(rows: Iterable[ComparisonRow]) -> list[FileDetails]:
        details_by_path: dict[str, FileDetails] = {}
        for row in rows:
            for details in (row.reference, row.selected):
                if details is not None:
                    details_by_path.setdefault(details.path, details)
        return list(details_by_path.values())

    @staticmethod
    def _details_belonging_to_source(
        source: str,
        rows: Iterable[ComparisonRow],
    ) -> list[FileDetails]:
        source_path = Path(source)
        is_directory = source_path.is_dir()
        result: list[FileDetails] = []
        for details in ComparisonWindow._all_row_details(rows):
            path = Path(details.path)
            if is_directory:
                try:
                    path.relative_to(source_path)
                except ValueError:
                    continue
            elif path != source_path:
                continue
            result.append(details)
        return sorted(result, key=lambda item: item.path.casefold())

    @staticmethod
    def _duplicate_paths(rows: Iterable[ComparisonRow]) -> set[str]:
        return {
            details.path
            for row in rows
            if row.reference is not None and row.selected is not None
            for details in (row.reference, row.selected)
        }

    def _add_source_row(
        self,
        path: str,
        rows: Iterable[ComparisonRow],
        *,
        unique_only: bool = False,
    ) -> None:
        rows = list(rows)
        if Path(path).is_dir():
            self._add_folder_source_row(
                path,
                rows,
                unique_only=unique_only,
            )
            return
        content = Gtk.Box(spacing=10)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        icon_name = (
            "folder-symbolic" if Path(path).is_dir()
            else "text-x-generic-symbolic"
        )
        icon = Gtk.Image(icon_name=icon_name)
        icon.set_pixel_size(22)
        content.append(icon)
        label = self._cell(path)
        content.append(label)
        duplicate = path in self._duplicate_paths(rows)
        state = Gtk.Label(
            label=tr(
                "comparison_duplicate" if duplicate else "comparison_unique"
            )
        )
        state.add_css_class(
            "comparison-tag" if duplicate else "dim-label"
        )
        if duplicate:
            state.add_css_class("comparison-tag-hash")
        content.append(state)
        remove = Gtk.Button(icon_name="window-close-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text(tr("comparison_remove_marked"))
        remove.connect("clicked", self._on_remove_marked, path)
        content.append(remove)
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.set_child(content)
        self.list_box.append(row)

    def _add_folder_source_row(
        self,
        path: str,
        rows: list[ComparisonRow],
        *,
        unique_only: bool = False,
    ) -> None:
        details = self._details_belonging_to_source(path, rows)
        duplicate_paths = self._duplicate_paths(rows)
        duplicates = sum(
            1 for item in details if item.path in duplicate_paths
        )
        total_size = sum(item.size or 0 for item in details)
        source_path = Path(path)
        tree_rows: list[ComparisonRow] = []
        for item in details:
            try:
                relative = Path(item.path).relative_to(source_path).as_posix()
            except ValueError:
                relative = item.name
            tree_rows.append(ComparisonRow(relative, item, None))
        tree = _comparison_tree(tree_rows)

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header_row = Gtk.Box(spacing=4)
        toggle = Gtk.ToggleButton()
        toggle.add_css_class("flat")
        toggle.set_hexpand(True)
        summary = Gtk.Box(spacing=10)
        summary.set_margin_top(8)
        summary.set_margin_bottom(8)
        summary.set_margin_end(10)
        indicator = Gtk.Image(icon_name="pan-end-symbolic")
        indicator.set_pixel_size(16)
        summary.append(indicator)
        icon = Gtk.Image(icon_name="folder-symbolic")
        icon.set_pixel_size(22)
        summary.append(icon)
        name = Gtk.Label(label=source_path.name, xalign=0, hexpand=True)
        name.add_css_class("heading")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_tooltip_text(path)
        summary.append(name)
        stats = Gtk.Label(
            label=tr(
                (
                    "comparison_unique_location_summary"
                    if unique_only
                    else "comparison_location_summary"
                ),
                files=len(details),
                duplicates=duplicates,
                size=_human_size(total_size),
            ),
            xalign=1,
        )
        stats.add_css_class("dim-label")
        summary.append(stats)
        toggle.set_child(summary)
        header_row.append(toggle)
        remove = Gtk.Button(icon_name="window-close-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text(tr("comparison_remove_marked"))
        remove.connect("clicked", self._on_remove_marked, path)
        header_row.append(remove)
        container.append(header_row)

        children = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        children.set_margin_bottom(10)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_child(children)
        container.append(revealer)
        toggle.connect(
            "toggled",
            self._on_source_toggled,
            path,
            tree,
            duplicate_paths,
            children,
            revealer,
            indicator,
        )
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.set_child(container)
        self.list_box.append(row)
        if path in self._expanded_source_folders:
            toggle.set_active(True)

    def _on_source_toggled(
        self,
        toggle: Gtk.ToggleButton,
        source: str,
        tree: ComparisonTreeNode,
        duplicate_paths: set[str],
        children: Gtk.Box,
        revealer: Gtk.Revealer,
        indicator: Gtk.Image,
    ) -> None:
        expanded = toggle.get_active()
        indicator.set_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"
        )
        revealer.set_reveal_child(expanded)
        if expanded:
            self._expanded_source_folders.add(source)
        else:
            self._expanded_source_folders.discard(source)
        if not expanded or children.get_first_child() is not None:
            return
        context = f"source:{source}"
        for node in tree.ordered_children():
            children.append(
                self._source_tree_node(
                    node,
                    source,
                    duplicate_paths,
                    depth=0,
                    context=context,
                )
            )

    def _source_tree_node(
        self,
        node: ComparisonTreeNode,
        source: str,
        duplicate_paths: set[str],
        depth: int,
        context: str,
    ) -> Gtk.Widget:
        if node.is_directory:
            return self._source_directory_node(
                node,
                source,
                duplicate_paths,
                depth,
                context,
            )
        details = node.row.reference if node.row is not None else None
        if details is None:
            return Gtk.Box()
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=1,
        )
        content.set_hexpand(True)
        content.set_margin_start(30 + depth * 18)
        content.set_margin_end(12)
        header = Gtk.Box(spacing=8)
        icon = Gtk.Image()
        content_type, _uncertain = Gio.content_type_guess(details.name, None)
        try:
            icon.set_from_gicon(Gio.content_type_get_icon(content_type))
        except Exception:
            icon.set_from_icon_name("text-x-generic-symbolic")
        icon.set_pixel_size(18)
        header.append(icon)
        name = Gtk.Label(label=details.name, xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_tooltip_text(details.path)
        header.append(name)
        size = Gtk.Label(label=_human_size(details.size), xalign=1)
        size.add_css_class("dim-label")
        header.append(size)
        duplicate = details.path in duplicate_paths
        status = Gtk.Label(
            label=tr(
                "comparison_duplicate" if duplicate else "comparison_unique"
            )
        )
        status.add_css_class(
            "comparison-tag" if duplicate else "dim-label"
        )
        if duplicate:
            status.add_css_class("comparison-tag-hash")
        header.append(status)
        content.append(header)
        metadata = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=1,
        )
        metadata.set_margin_start(26)
        metadata.append(
            self._hash_metadata_row(
                details,
                expand=False,
                max_width=-1,
            )
        )
        content.append(metadata)
        content.set_tooltip_text(details.path)
        self._attach_file_context_menu(content, details)
        return content

    def _source_directory_node(
        self,
        node: ComparisonTreeNode,
        source: str,
        duplicate_paths: set[str],
        depth: int,
        context: str,
    ) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toggle = Gtk.ToggleButton()
        toggle.add_css_class("flat")
        toggle.set_hexpand(True)
        header = Gtk.Box(spacing=8)
        header.set_margin_start(12 + depth * 18)
        header.set_margin_end(12)
        indicator = Gtk.Image(icon_name="pan-end-symbolic")
        indicator.set_pixel_size(14)
        header.append(indicator)
        icon = Gtk.Image(icon_name="folder-symbolic")
        icon.set_pixel_size(18)
        header.append(icon)
        name = Gtk.Label(label=node.name, xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        full_path = str(Path(source) / node.relative_path)
        name.set_tooltip_text(full_path)
        header.append(name)
        stats = Gtk.Label(
            label=tr(
                "comparison_tree_folder_summary",
                files=node.reference_count,
                size=_human_size(node.reference_size),
            ),
            xalign=1,
        )
        stats.add_css_class("dim-label")
        header.append(stats)
        toggle.set_child(header)
        container.append(toggle)

        children = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_child(children)
        container.append(revealer)
        state_key = (context, node.relative_path)
        toggle.connect(
            "toggled",
            self._on_source_directory_toggled,
            node,
            source,
            duplicate_paths,
            depth,
            context,
            children,
            revealer,
            indicator,
            state_key,
        )
        if state_key in self._expanded_tree_nodes:
            toggle.set_active(True)
        return container

    def _on_source_directory_toggled(
        self,
        toggle: Gtk.ToggleButton,
        node: ComparisonTreeNode,
        source: str,
        duplicate_paths: set[str],
        depth: int,
        context: str,
        children: Gtk.Box,
        revealer: Gtk.Revealer,
        indicator: Gtk.Image,
        state_key: tuple[str, str],
    ) -> None:
        expanded = toggle.get_active()
        indicator.set_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"
        )
        revealer.set_reveal_child(expanded)
        if expanded:
            self._expanded_tree_nodes.add(state_key)
        else:
            self._expanded_tree_nodes.discard(state_key)
        if not expanded or children.get_first_child() is not None:
            return
        for child in node.ordered_children():
            children.append(
                self._source_tree_node(
                    child,
                    source,
                    duplicate_paths,
                    depth + 1,
                    context,
                )
            )

    @staticmethod
    def _folder_tree_width(folder_count: int) -> int:
        return (
            folder_count * FOLDER_TREE_FILE_WIDTH
            + max(0, folder_count - 1) * FOLDER_TREE_TAG_WIDTH
        )

    @staticmethod
    def _relative_folder_path(details: FileDetails, source: str) -> str:
        try:
            return Path(details.path).relative_to(Path(source)).as_posix()
        except ValueError:
            return details.relative_path or details.name

    @staticmethod
    def _matching_hash_algorithm(
        first: FileDetails,
        second: FileDetails,
    ) -> str:
        common = first.hashes.keys() & second.hashes.keys()
        matching = [
            algorithm
            for algorithm in common
            if first.hashes.get(algorithm)
            and first.hashes.get(algorithm) == second.hashes.get(algorithm)
        ]
        return min(matching, key=_hash_sort_key) if matching else ""

    def _folder_byte_state(
        self,
        first: FileDetails,
        second: FileDetails,
    ) -> bool | None:
        paths = {first.path, second.path}
        for row in self.rows:
            if row.reference is None or row.selected is None:
                continue
            if {row.reference.path, row.selected.path} == paths:
                return row.byte_match
        return None

    def _folder_pair_tags(
        self,
        first: FileDetails | None,
        first_source: str,
        second: FileDetails | None,
        second_source: str,
    ) -> Gtk.Widget:
        tags = Gtk.FlowBox()
        tags.set_selection_mode(Gtk.SelectionMode.NONE)
        tags.set_homogeneous(False)
        tags.set_max_children_per_line(2)
        tags.set_row_spacing(4)
        tags.set_column_spacing(4)
        tags.set_halign(Gtk.Align.CENTER)
        tags.set_valign(Gtk.Align.CENTER)
        tags.set_size_request(FOLDER_TREE_TAG_WIDTH, -1)
        if first is None or second is None:
            return tags
        algorithm = self._matching_hash_algorithm(first, second)
        same_size = first.size is not None and first.size == second.size
        same_modified = modification_times_match(first, second)
        same_name = first.name == second.name
        same_path = (
            self._relative_folder_path(first, first_source)
            == self._relative_folder_path(second, second_source)
        )
        byte_state = self._folder_byte_state(first, second)
        if byte_state is True:
            tags.append(
                self._comparison_tag(
                    "B",
                    "comparison-tag-byte",
                    tr("comparison_tree_tag_byte_help"),
                )
            )
        identical = bool(
            algorithm
            and same_size
            and same_modified
            and same_name
            and same_path
        )
        if identical:
            tags.append(
                self._comparison_tag(
                    "I",
                    "comparison-tag-identical",
                    tr("comparison_tree_tag_identical_help"),
                )
            )
            return tags
        attributes = (
            (
                bool(algorithm),
                "H",
                "comparison-tag-hash",
                tr(
                    "comparison_tree_tag_hash_help",
                    algorithm=(
                        hash_algorithm_label(algorithm)
                        if algorithm else tr("comparison_hash")
                    ),
                ),
            ),
            (
                same_size,
                "S",
                "comparison-tag-metadata",
                tr("comparison_tree_tag_size_help"),
            ),
            (
                same_modified,
                "M",
                "comparison-tag-metadata",
                tr("comparison_tree_tag_modified_help"),
            ),
            (
                same_name,
                "N",
                "comparison-tag-metadata",
                tr("comparison_tree_tag_name_help"),
            ),
            (
                same_path,
                "P",
                "comparison-tag-metadata",
                tr("comparison_tree_tag_path_help"),
            ),
        )
        for matches, text, css_class, tooltip in attributes:
            if matches:
                tags.append(self._comparison_tag(text, css_class, tooltip))
        return tags

    def _folder_tree_cell(
        self,
        details: FileDetails | None,
        depth: int,
    ) -> Gtk.Widget:
        if details is None:
            missing = Gtk.Label(label="—", xalign=0)
            missing.add_css_class("dim-label")
            missing.set_margin_start(18 + depth * 18)
            return missing
        content = Gtk.Box(spacing=8)
        content.set_margin_start(18 + depth * 18)
        content_type, _uncertain = Gio.content_type_guess(details.name, None)
        icon = Gtk.Image()
        try:
            icon.set_from_gicon(Gio.content_type_get_icon(content_type))
        except Exception:
            icon.set_from_icon_name("text-x-generic-symbolic")
        icon.set_pixel_size(16)
        content.append(icon)
        name = Gtk.Label(label=details.name, xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_tooltip_text(details.path)
        content.append(name)
        self._attach_file_context_menu(content, details)
        return content

    def _folder_tags(
        self,
        folder: FolderComparisonGroup,
        analysis: FolderContentAnalysis,
    ) -> Gtk.FlowBox:
        attributes = folder.matching_attributes()
        tags = Gtk.FlowBox()
        tags.set_selection_mode(Gtk.SelectionMode.NONE)
        tags.set_homogeneous(False)
        tags.set_max_children_per_line(3)
        tags.set_row_spacing(5)
        tags.set_column_spacing(5)
        tags.set_halign(Gtk.Align.START)
        if attributes["byte"]:
            tags.append(
                self._comparison_tag(
                    tr("comparison_tag_byte"),
                    "comparison-tag-byte",
                    tr("comparison_tag_byte_help"),
                )
            )
        if folder.identical:
            tags.append(
                self._comparison_tag(
                    tr("comparison_tag_identical"),
                    "comparison-tag-identical",
                    tr("comparison_folder_identical_help"),
                )
            )
        elif analysis.same_content:
            tags.append(
                self._comparison_tag(
                    tr("comparison_folder_same_content"),
                    "comparison-tag-hash",
                    tr("comparison_folder_same_content_help"),
                )
            )
        elif attributes["hash"]:
            for algorithm in folder.matched_hash_algorithms():
                tags.append(
                    self._comparison_tag(
                        hash_algorithm_label(algorithm),
                        "comparison-tag-hash",
                        tr(
                            "comparison_folder_hash_help",
                            algorithm=hash_algorithm_label(algorithm),
                        ),
                    )
                )
        if attributes["size"] and not folder.identical:
            tags.append(
                self._comparison_tag(
                    tr("comparison_tag_size"),
                    "comparison-tag-metadata",
                    tr("comparison_tag_size_help"),
                )
            )
        if attributes["modified"]:
            tags.append(
                self._comparison_tag(
                    tr("comparison_tag_modified"),
                    "comparison-tag-metadata",
                    tr("comparison_tag_modified_help"),
                )
            )
        return tags

    def _folder_tree_header(
        self,
        folder: FolderComparisonGroup,
    ) -> Gtk.Widget:
        header = Gtk.Grid(column_spacing=4)
        header.set_column_homogeneous(False)
        header.set_size_request(
            self._folder_tree_width(len(folder.paths)),
            -1,
        )
        header.set_margin_top(8)
        header.set_margin_bottom(6)
        header.set_margin_start(12)
        header.set_margin_end(12)
        for column, source in enumerate(folder.paths):
            cell = Gtk.Box(spacing=6, hexpand=True)
            cell.set_size_request(FOLDER_TREE_FILE_WIDTH, -1)
            label = Gtk.Label(
                label=tr("comparison_folder_number", number=column + 1),
                xalign=0,
                hexpand=True,
            )
            label.add_css_class("heading")
            label.set_tooltip_text(source)
            cell.append(label)
            remove = Gtk.Button(icon_name="window-close-symbolic")
            remove.add_css_class("flat")
            remove.set_tooltip_text(
                f"{tr('comparison_remove_marked')}: {source}"
            )
            remove.connect("clicked", self._on_remove_marked, source)
            cell.append(remove)
            header.attach(cell, column * 2, 0, 1, 1)
            if column < len(folder.paths) - 1:
                spacer = Gtk.Box()
                spacer.set_size_request(FOLDER_TREE_TAG_WIDTH, -1)
                header.attach(spacer, column * 2 + 1, 0, 1, 1)
        return header

    def _folder_tree_row(
        self,
        node: FolderGroupTreeNode,
        folder: FolderComparisonGroup,
        depth: int,
    ) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=4)
        grid.set_size_request(
            self._folder_tree_width(len(folder.paths)),
            -1,
        )
        grid.set_margin_top(4)
        grid.set_margin_bottom(4)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.set_column_homogeneous(False)
        anchor_source = next(
            (
                source
                for source in folder.paths
                if node.files.get(source) is not None
            ),
            "",
        )
        anchor = node.files.get(anchor_source)
        for column, source in enumerate(folder.paths):
            cell = self._folder_tree_cell(node.files.get(source), depth)
            cell.set_size_request(FOLDER_TREE_FILE_WIDTH, -1)
            grid.attach(
                cell,
                column * 2,
                0,
                1,
                1,
            )
            if column:
                grid.attach(
                    self._folder_pair_tags(
                        anchor,
                        anchor_source,
                        node.files.get(source),
                        source,
                    ),
                    column * 2 - 1,
                    0,
                    1,
                    1,
                )
        return grid

    def _folder_content_file_entry(
        self,
        details: FileDetails,
        source: str,
    ) -> Gtk.Widget:
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=1,
        )
        header = Gtk.Box(spacing=7)
        content_type, _uncertain = Gio.content_type_guess(details.name, None)
        icon = Gtk.Image()
        try:
            icon.set_from_gicon(Gio.content_type_get_icon(content_type))
        except Exception:
            icon.set_from_icon_name("text-x-generic-symbolic")
        icon.set_pixel_size(16)
        header.append(icon)
        name = Gtk.Label(label=details.name, xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(name)
        content.append(header)
        relative = self._relative_folder_path(details, source)
        parent = str(PurePosixPath(relative).parent)
        location = Gtk.Label(
            label=parent if parent not in ("", ".") else "/",
            xalign=0,
        )
        location.set_margin_start(23)
        location.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        location.add_css_class("dim-label")
        location.add_css_class("caption")
        content.append(location)
        content.set_tooltip_text(details.path)
        self._attach_file_context_menu(content, details)
        return content

    def _folder_content_cell(
        self,
        details_list: tuple[FileDetails, ...],
        source: str,
    ) -> Gtk.Widget:
        if not details_list:
            missing = Gtk.Label(label="—", xalign=0)
            missing.add_css_class("dim-label")
            missing.set_size_request(FOLDER_TREE_FILE_WIDTH, -1)
            return missing
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        content.set_size_request(FOLDER_TREE_FILE_WIDTH, -1)
        for details in details_list:
            content.append(self._folder_content_file_entry(details, source))
        return content

    def _folder_content_row(
        self,
        match: FolderContentGroup,
        folder: FolderComparisonGroup,
    ) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=4)
        grid.set_size_request(
            self._folder_tree_width(len(folder.paths)),
            -1,
        )
        grid.set_margin_top(5)
        grid.set_margin_bottom(5)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        anchor_source = next(
            (path for path in folder.paths if match.files.get(path)),
            "",
        )
        anchor_files = match.files.get(anchor_source, ())
        anchor = anchor_files[0] if anchor_files else None
        for column, source in enumerate(folder.paths):
            details_list = match.files.get(source, ())
            grid.attach(
                self._folder_content_cell(details_list, source),
                column * 2,
                0,
                1,
                1,
            )
            if column:
                candidate = details_list[0] if details_list else None
                grid.attach(
                    self._folder_pair_tags(
                        anchor,
                        anchor_source,
                        candidate,
                        source,
                    ),
                    column * 2 - 1,
                    0,
                    1,
                    1,
                )
        return grid

    def _folder_result_section(
        self,
        folder: FolderComparisonGroup,
        *,
        title: str,
        count: int,
        icon_name: str,
        context: str,
        section_key: str,
        matches: list[FolderContentGroup] | None = None,
        tree: FolderGroupTreeNode | None = None,
    ) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toggle = Gtk.ToggleButton()
        toggle.add_css_class("flat")
        toggle.set_hexpand(True)
        header = Gtk.Box(spacing=8)
        header.set_margin_start(12)
        header.set_margin_end(12)
        indicator = Gtk.Image(icon_name="pan-end-symbolic")
        indicator.set_pixel_size(14)
        header.append(indicator)
        icon = Gtk.Image(icon_name=icon_name)
        icon.set_pixel_size(18)
        header.append(icon)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("heading")
        header.append(label)
        amount = Gtk.Label(
            label=tr("comparison_tree_section_count", count=count),
            xalign=1,
        )
        amount.add_css_class("dim-label")
        header.append(amount)
        toggle.set_child(header)
        container.append(toggle)
        children = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_child(children)
        container.append(revealer)
        state_key = (context, f"section:{section_key}")
        toggle.connect(
            "toggled",
            self._on_folder_result_section_toggled,
            folder,
            matches,
            tree,
            context,
            children,
            revealer,
            indicator,
            state_key,
        )
        if state_key in self._expanded_tree_nodes:
            toggle.set_active(True)
        return container

    def _on_folder_result_section_toggled(
        self,
        toggle: Gtk.ToggleButton,
        folder: FolderComparisonGroup,
        matches: list[FolderContentGroup] | None,
        tree: FolderGroupTreeNode | None,
        context: str,
        children: Gtk.Box,
        revealer: Gtk.Revealer,
        indicator: Gtk.Image,
        state_key: tuple[str, str],
    ) -> None:
        expanded = toggle.get_active()
        indicator.set_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"
        )
        revealer.set_reveal_child(expanded)
        if expanded:
            self._expanded_tree_nodes.add(state_key)
        else:
            self._expanded_tree_nodes.discard(state_key)
        if not expanded or children.get_first_child() is not None:
            return
        if matches is not None:
            for match in matches:
                children.append(self._folder_content_row(match, folder))
            return
        if tree is not None:
            for node in tree.ordered_children():
                children.append(
                    self._folder_comparison_tree_node(
                        node,
                        folder,
                        depth=0,
                        context=context,
                    )
                )

    @staticmethod
    def _folder_stat_row(
        icon_name: str,
        label_text: str,
        value: str,
    ) -> Gtk.Widget:
        row = Gtk.Box(spacing=6)
        row.set_halign(Gtk.Align.END)
        icon = Gtk.Image(icon_name=icon_name)
        icon.set_pixel_size(14)
        icon.add_css_class("dim-label")
        row.append(icon)
        label = Gtk.Label(label=label_text, xalign=0)
        label.add_css_class("dim-label")
        label.add_css_class("caption")
        row.append(label)
        value_label = Gtk.Label(label=value, xalign=1)
        value_label.add_css_class("heading")
        value_label.set_ellipsize(Pango.EllipsizeMode.END)
        value_label.set_max_width_chars(34)
        value_label.set_tooltip_text(value)
        row.append(value_label)
        return row

    def _add_folder_comparison(self, folder: FolderComparisonGroup) -> None:
        analysis = _folder_content_analysis(folder)
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toggle = Gtk.ToggleButton()
        toggle.add_css_class("flat")
        toggle.set_hexpand(True)
        summary = Gtk.Box(spacing=10)
        summary.set_margin_top(8)
        summary.set_margin_bottom(8)
        summary.set_margin_end(10)
        summary.append(self._folder_tags(folder, analysis))
        icon = Gtk.Image(icon_name="folder-symbolic")
        icon.set_pixel_size(22)
        summary.append(icon)
        indicator = Gtk.Image(icon_name="pan-end-symbolic")
        indicator.set_pixel_size(16)
        summary.append(indicator)
        names = Gtk.Label(
            label="  ↔  ".join(Path(path).name for path in folder.paths),
            xalign=0,
            hexpand=True,
        )
        names.add_css_class("heading")
        names.set_ellipsize(Pango.EllipsizeMode.END)
        names.set_tooltip_text("\n".join(folder.paths))
        summary.append(names)
        count = sum(len(files) for files in folder.files.values())
        stats = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )
        stats.set_halign(Gtk.Align.END)
        stats.append(
            self._folder_stat_row(
                "object-select-symbolic",
                tr("comparison_folder_matched_label"),
                f"{analysis.matching_file_count}/{count}",
            )
        )
        stats.append(
            self._folder_stat_row(
                "drive-harddisk-symbolic",
                tr("comparison_folder_size_label"),
                " / ".join(
                    _human_size(folder.source_size(path))
                    for path in folder.paths
                ),
            )
        )
        summary.append(stats)
        toggle.set_child(summary)
        container.append(toggle)
        children = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=5,
        )
        children.set_margin_bottom(10)
        horizontal_scroll = Gtk.ScrolledWindow()
        horizontal_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.NEVER,
        )
        horizontal_scroll.set_propagate_natural_height(True)
        horizontal_scroll.set_child(children)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_child(horizontal_scroll)
        container.append(revealer)
        toggle.connect(
            "toggled",
            self._on_folder_toggled,
            indicator,
            folder,
            analysis,
            children,
            revealer,
        )
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.set_child(container)
        self.list_box.append(row)
        folder_key = folder.paths
        if folder_key in self._expanded_folder_comparisons:
            toggle.set_active(True)

    def _on_folder_toggled(
        self,
        toggle: Gtk.ToggleButton,
        indicator: Gtk.Image,
        folder: FolderComparisonGroup,
        analysis: FolderContentAnalysis,
        children: Gtk.Box,
        revealer: Gtk.Revealer,
    ) -> None:
        expanded = toggle.get_active()
        indicator.set_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"
        )
        revealer.set_reveal_child(expanded)
        folder_key = folder.paths
        if expanded:
            self._expanded_folder_comparisons.add(folder_key)
        else:
            self._expanded_folder_comparisons.discard(folder_key)
        if not expanded or children.get_first_child() is not None:
            return
        children.append(self._folder_tree_header(folder))
        context = "pair:" + "\0".join(folder.paths)
        if analysis.matches:
            children.append(
                self._folder_result_section(
                    folder,
                    title=tr("comparison_tree_matching_content"),
                    count=analysis.matching_file_count,
                    icon_name="object-select-symbolic",
                    context=context,
                    section_key="content",
                    matches=analysis.matches,
                )
            )
        if analysis.conflicts:
            children.append(
                self._folder_result_section(
                    folder,
                    title=tr("comparison_tree_path_conflicts"),
                    count=sum(
                        len(files) for files in analysis.conflicts.values()
                    ),
                    icon_name="dialog-warning-symbolic",
                    context=context,
                    section_key="conflicts",
                    tree=_folder_group_tree(folder, analysis.conflicts),
                )
            )
        if analysis.unique:
            children.append(
                self._folder_result_section(
                    folder,
                    title=tr("comparison_tree_unique"),
                    count=sum(
                        len(files) for files in analysis.unique.values()
                    ),
                    icon_name="folder-symbolic",
                    context=context,
                    section_key="unique",
                    tree=_folder_group_tree(folder, analysis.unique),
                )
            )

    def _folder_comparison_tree_node(
        self,
        node: FolderGroupTreeNode,
        folder: FolderComparisonGroup,
        depth: int,
        context: str,
    ) -> Gtk.Widget:
        if node.is_directory:
            return self._folder_comparison_directory_node(
                node,
                folder,
                depth,
                context,
            )
        return self._folder_tree_row(node, folder, depth)

    def _folder_comparison_directory_node(
        self,
        node: FolderGroupTreeNode,
        folder: FolderComparisonGroup,
        depth: int,
        context: str,
    ) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toggle = Gtk.ToggleButton()
        toggle.add_css_class("flat")
        toggle.set_hexpand(True)
        header = Gtk.Box(spacing=8)
        header.set_margin_start(12)
        header.set_margin_end(12)
        grid = Gtk.Grid(column_spacing=4, hexpand=True)
        grid.set_column_homogeneous(False)
        grid.set_size_request(
            self._folder_tree_width(len(folder.paths)),
            -1,
        )
        indicators: list[Gtk.Image] = []
        for column, source in enumerate(folder.paths):
            cell, indicator = self._folder_directory_cell(
                node,
                source,
                depth,
            )
            if indicator is not None:
                indicators.append(indicator)
            cell.set_size_request(FOLDER_TREE_FILE_WIDTH, -1)
            grid.attach(
                cell,
                column * 2,
                0,
                1,
                1,
            )
            if column < len(folder.paths) - 1:
                spacer = Gtk.Box()
                spacer.set_size_request(FOLDER_TREE_TAG_WIDTH, -1)
                grid.attach(spacer, column * 2 + 1, 0, 1, 1)
        header.append(grid)
        toggle.set_child(header)
        container.append(toggle)

        children = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_child(children)
        container.append(revealer)
        state_key = (context, node.relative_path)
        toggle.connect(
            "toggled",
            self._on_folder_directory_toggled,
            node,
            folder,
            depth,
            context,
            children,
            revealer,
            indicators,
            state_key,
        )
        if state_key in self._expanded_tree_nodes:
            toggle.set_active(True)
        return container

    def _folder_directory_cell(
        self,
        node: FolderGroupTreeNode,
        root_path: str,
        depth: int,
    ) -> tuple[Gtk.Widget, Gtk.Image | None]:
        count = node.counts.get(root_path, 0)
        size = node.sizes.get(root_path, 0)
        if count == 0:
            missing = Gtk.Label(label="—", xalign=0)
            missing.add_css_class("dim-label")
            missing.set_margin_start(18 + depth * 18)
            return missing, None
        content = Gtk.Box(spacing=7)
        content.set_margin_start(depth * 18)
        indicator = Gtk.Image(icon_name="pan-end-symbolic")
        indicator.set_pixel_size(14)
        content.append(indicator)
        icon = Gtk.Image(icon_name="folder-symbolic")
        icon.set_pixel_size(18)
        content.append(icon)
        labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=1,
            hexpand=True,
        )
        name = Gtk.Label(label=node.name, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_tooltip_text(str(Path(root_path) / node.relative_path))
        labels.append(name)
        stats = Gtk.Label(
            label=tr(
                "comparison_tree_folder_summary",
                files=count,
                size=_human_size(size),
            ),
            xalign=0,
        )
        stats.add_css_class("dim-label")
        stats.add_css_class("caption")
        labels.append(stats)
        content.append(labels)
        return content, indicator

    def _on_folder_directory_toggled(
        self,
        toggle: Gtk.ToggleButton,
        node: FolderGroupTreeNode,
        folder: FolderComparisonGroup,
        depth: int,
        context: str,
        children: Gtk.Box,
        revealer: Gtk.Revealer,
        indicators: list[Gtk.Image],
        state_key: tuple[str, str],
    ) -> None:
        expanded = toggle.get_active()
        for indicator in indicators:
            indicator.set_from_icon_name(
                "pan-down-symbolic" if expanded else "pan-end-symbolic"
            )
        revealer.set_reveal_child(expanded)
        if expanded:
            self._expanded_tree_nodes.add(state_key)
        else:
            self._expanded_tree_nodes.discard(state_key)
        if not expanded or children.get_first_child() is not None:
            return
        for child in node.ordered_children():
            children.append(
                self._folder_comparison_tree_node(
                    child,
                    folder,
                    depth + 1,
                    context,
                )
            )

    def _watch_marked_paths(self) -> bool:
        marked = tuple(load_marked_paths())
        if marked == self._marked_paths:
            return GLib.SOURCE_CONTINUE
        self._marked_paths = marked
        if self._running:
            self._refresh_pending = True
        else:
            self._start_comparison()
        return GLib.SOURCE_CONTINUE

    def _on_close_request(self, *_args) -> bool:
        if self._selection_watch_source:
            GLib.source_remove(self._selection_watch_source)
            self._selection_watch_source = 0
        return False

    def _start_comparison(self, *_args) -> None:
        if self._running:
            return
        self._profiles = list_profiles()
        sources = load_marked_paths()
        self._marked_paths = tuple(sources)
        self._refresh_pending = False
        if not sources:
            self._pool_only_mode = True
            self.rows = []
            self.folder_comparisons = []
            self.progress.set_text(tr("comparison_no_marked"))
            self._clear_rows()
            self.spinner.stop()
            self.list_box.set_sensitive(True)
            self.byte_compare_button.set_sensitive(False)
            self.clear_button.set_sensitive(False)
            return
        self._pool_only_mode = False
        self._running = True
        self.spinner.start()
        self.list_box.set_sensitive(False)
        self.byte_compare_button.set_sensitive(False)
        self.clear_button.set_sensitive(False)
        self.progress.set_text(tr("comparison_calculating"))
        thread = threading.Thread(
            target=self._calculate_in_background,
            args=(sources,),
            daemon=True,
        )
        thread.start()

    def _calculate_in_background(
        self,
        sources: list[str],
    ) -> None:
        rows = create_pool_rows(sources)
        result = compare_pool_rows(rows, sources)
        GLib.idle_add(self._show_rows, result)

    @staticmethod
    def _matching_rows(rows: Iterable[ComparisonRow]) -> list[ComparisonRow]:
        return [
            row
            for row in rows
            if row.reference is not None and row.selected is not None
        ]

    @staticmethod
    def _details_is_inside_folders(
        details: FileDetails | None,
        folders: Iterable[Path],
    ) -> bool:
        if details is None:
            return False
        path = Path(details.path)
        for folder in folders:
            try:
                path.relative_to(folder)
                return True
            except ValueError:
                continue
        return False

    @classmethod
    def _row_is_inside_folders(
        cls,
        row: ComparisonRow,
        folders: Iterable[Path],
    ) -> bool:
        folders = tuple(folders)
        return cls._details_is_inside_folders(
            row.reference,
            folders,
        ) or cls._details_is_inside_folders(row.selected, folders)

    @staticmethod
    def _partition_folder_comparisons(
        folders: Iterable[FolderComparison],
    ) -> tuple[list[FolderComparison], list[FolderComparison]]:
        identical: list[FolderComparison] = []
        different: list[FolderComparison] = []
        for folder in folders:
            (identical if folder.identical else different).append(folder)
        return identical, different

    @staticmethod
    def _byte_comparable_rows(
        rows: Iterable[ComparisonRow],
    ) -> list[ComparisonRow]:
        return [
            row
            for row in rows
            if row.reference is not None
            and row.selected is not None
            and row.matching_attributes()["hash"]
        ]

    def _render_rows(
        self,
        rows: list[ComparisonRow],
        folders: Iterable[FolderComparison] = (),
    ) -> None:
        self._pool_only_mode = False
        self.rows = rows
        self.folder_comparisons = list(folders)
        self._clear_rows()
        sources = load_marked_paths()
        folder_sources = tuple(
            Path(source) for source in sources if Path(source).is_dir()
        )
        visible_rows = [
            row for row in rows
            if not self._row_is_inside_folders(row, folder_sources)
        ]
        matching = self._matching_rows(visible_rows)
        matching_groups = self._group_matching_rows(matching)
        unique = [
            row for row in visible_rows
            if row.reference is not None and row.selected is None
        ]
        all_unique_rows = [
            row for row in rows
            if row.reference is not None and row.selected is None
        ]
        unique_folder_sources = [
            source
            for source in sources
            if Path(source).is_dir()
            and self._details_belonging_to_source(
                source,
                all_unique_rows,
            )
        ]
        folder_groups = _folder_comparison_groups(
            self.folder_comparisons,
            sources,
        )
        identical_folders = [
            folder for folder in folder_groups if folder.identical
        ]
        different_folders = [
            folder for folder in folder_groups if not folder.identical
        ]

        if identical_folders or matching_groups:
            self._add_section(tr("comparison_matching_pairs"))
        for folder in identical_folders:
            self._add_folder_comparison(folder)
        for representative, files in matching_groups:
            self._add_duplicate_group_row(representative, files)

        if unique_folder_sources or unique:
            self._add_section(tr("comparison_unique_files"))
        for source in unique_folder_sources:
            self._add_source_row(
                source,
                all_unique_rows,
                unique_only=True,
            )
        for row in unique:
            self._add_unique_row(row)

        if different_folders:
            self._add_section(tr("comparison_folder_comparisons"))
        for folder in different_folders:
            self._add_folder_comparison(folder)

    def _show_rows(self, result: PoolComparison) -> bool:
        rows = result.rows
        self._render_rows(rows, result.folders)
        self.spinner.stop()
        self.list_box.set_sensitive(True)
        self.byte_compare_button.set_sensitive(
            bool(self._byte_comparable_rows(rows))
        )
        self.clear_button.set_sensitive(True)
        self._running = False
        self.progress.set_text(
            tr(
                "comparison_pool_completed",
                files=result.file_count,
                pairs=len(self._matching_rows(rows)),
            )
            if result.file_count
            else tr("comparison_empty")
        )
        if self._refresh_pending:
            GLib.idle_add(self._start_comparison)
        return GLib.SOURCE_REMOVE

    def _start_byte_comparison(self, *_args) -> None:
        if self._running:
            return
        matching_count = len(self._byte_comparable_rows(self.rows))
        if not matching_count:
            self.byte_compare_button.set_sensitive(False)
            return
        self._running = True
        self.spinner.start()
        self.list_box.set_sensitive(False)
        self.byte_compare_button.set_sensitive(False)
        self.clear_button.set_sensitive(False)
        self.progress.set_text(
            tr("comparison_byte_calculating", count=matching_count)
        )
        thread = threading.Thread(
            target=self._compare_bytes_in_background,
            args=(self.rows,),
            daemon=True,
        )
        thread.start()

    def _compare_bytes_in_background(
        self,
        rows: list[ComparisonRow],
    ) -> None:
        checked = compare_matching_pairs_byte_by_byte(rows)
        GLib.idle_add(self._show_byte_results, rows, checked)

    @staticmethod
    def _row_hash_signature(row: ComparisonRow) -> tuple[str, str] | None:
        algorithm = normalize_hash_algorithm(row.matched_hash_algorithm)
        reference = row.reference
        selected = row.selected
        if not algorithm or reference is None or selected is None:
            return None
        digest = reference.hashes.get(algorithm, "")
        if not digest or digest != selected.hashes.get(algorithm, ""):
            return None
        return algorithm, digest

    @classmethod
    def _sync_folder_byte_results(
        cls,
        rows: Iterable[ComparisonRow],
        folders: Iterable[FolderComparison],
    ) -> None:
        rows = list(rows)
        verified = {
            signature
            for row in rows
            if row.byte_match is True
            if (signature := cls._row_hash_signature(row)) is not None
        }
        failed = {
            signature
            for row in rows
            if row.byte_match is False or row.byte_error
            if (signature := cls._row_hash_signature(row)) is not None
        }
        for folder in folders:
            for row in folder.rows:
                signature = cls._row_hash_signature(row)
                if signature is not None and signature in verified - failed:
                    row.byte_match = True

    def _show_byte_results(
        self,
        rows: list[ComparisonRow],
        checked: int,
    ) -> bool:
        self._sync_folder_byte_results(rows, self.folder_comparisons)
        self._render_rows(rows, self.folder_comparisons)
        self.spinner.stop()
        self.list_box.set_sensitive(True)
        self.byte_compare_button.set_sensitive(
            bool(self._byte_comparable_rows(rows))
        )
        self.clear_button.set_sensitive(True)
        self._running = False
        self.progress.set_text(
            tr("comparison_byte_completed", count=checked)
        )
        if self._refresh_pending:
            GLib.idle_add(self._start_comparison)
        return GLib.SOURCE_REMOVE

    def _on_clear_marked(self, *_args) -> None:
        clear_marked_paths()
        self._marked_paths = ()
        if self._running:
            self._refresh_pending = True
        else:
            self._start_comparison()

    def _on_remove_marked(self, _button, path: str) -> None:
        self._marked_paths = tuple(unmark_paths((path,)))
        if self._running:
            self._refresh_pending = True
        else:
            self._start_comparison()

    def set_selected_paths(self, selected_paths: Iterable[str]) -> None:
        updated = list(selected_paths)
        if not updated:
            return
        self._marked_paths = tuple(mark_paths(updated))
        if self._running:
            self._refresh_pending = True
        else:
            self._start_comparison()


class ComparisonApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=COMPARISON_APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.window: ComparisonWindow | None = None

    def do_activate(self) -> None:
        self._present_comparison(())

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        arguments = command_line.get_arguments()
        self._present_comparison(map(os.fsdecode, arguments[1:]))
        return 0

    def _present_comparison(self, selected_paths: Iterable[str]) -> None:
        if self.window is None or self.window.get_application() is None:
            self.window = ComparisonWindow(self, selected_paths)
        else:
            self.window.set_selected_paths(selected_paths)
        self.window.present()


def run_file_comparison(selected_paths: Iterable[str]) -> int:
    arguments = [
        "talaryn-comparison",
        *[os.fsdecode(path) for path in selected_paths],
    ]
    return ComparisonApplication().run(arguments)
