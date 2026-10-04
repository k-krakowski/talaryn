"""Optional X11 placement and native parent handles for external GTK dialogs.

Wayland placement stays with the compositor; xdg-foreign establishes the parent.
This module has no package imports so Nautilus can load it without loading the GUI.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager


@contextmanager
def _x11():
    library = ctypes.CDLL("libX11.so.6")
    library.XOpenDisplay.argtypes = [ctypes.c_char_p]
    library.XOpenDisplay.restype = ctypes.c_void_p
    library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    library.XDefaultRootWindow.restype = ctypes.c_ulong
    library.XCloseDisplay.argtypes = [ctypes.c_void_p]
    library.XFlush.argtypes = [ctypes.c_void_p]
    display = library.XOpenDisplay(None)
    try:
        yield library, display
    finally:
        if display:
            library.XCloseDisplay(display)


def capture_x11_pointer() -> list[int] | None:
    try:
        with _x11() as (lib, display):
            if not display:
                return None
            window = ctypes.c_ulong
            integer = ctypes.c_int
            lib.XQueryPointer.argtypes = [ctypes.c_void_p, window,
                ctypes.POINTER(window), ctypes.POINTER(window),
                ctypes.POINTER(integer), ctypes.POINTER(integer),
                ctypes.POINTER(integer), ctypes.POINTER(integer),
                ctypes.POINTER(ctypes.c_uint)]
            root, child = window(), window()
            x, y, local_x, local_y = integer(), integer(), integer(), integer()
            mask = ctypes.c_uint()
            if lib.XQueryPointer(display, lib.XDefaultRootWindow(display),
                                 ctypes.byref(root), ctypes.byref(child), ctypes.byref(x),
                                 ctypes.byref(y), ctypes.byref(local_x), ctypes.byref(local_y),
                                 ctypes.byref(mask)):
                return [x.value, y.value]
    except (OSError, AttributeError):
        pass
    return None


def clamp_position(pointer, size, bounds) -> tuple[int, int]:
    x, y = pointer
    width, height = size
    left, top, available_width, available_height = bounds
    return (max(left, min(x + 12, left + available_width - width)),
            max(top, min(y + 12, top + available_height - height)))


class _ClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class _XEvent(ctypes.Union):
    _fields_ = [("client", _ClientMessage), ("padding", ctypes.c_long * 24)]


def set_native_parent(window, handle: str) -> None:
    if not handle:
        return
    try:
        window.realize()
        surface = window.get_surface()
        if handle.startswith("wayland:"):
            import gi
            gi.require_version("GdkWayland", "4.0")
            from gi.repository import GdkWayland
            if isinstance(surface, GdkWayland.WaylandToplevel):
                surface.set_transient_for_exported(handle[len("wayland:"):])
        elif handle.startswith("x11:"):
            import gi
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11
            if isinstance(surface, GdkX11.X11Surface):
                with _x11() as (lib, display):
                    if display:
                        lib.XSetTransientForHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
                        lib.XSetTransientForHint(display, surface.get_xid(), int(handle[4:], 16))
                        lib.XFlush(display)
    except (OSError, ValueError, ImportError, AttributeError):
        pass


def place_at_pointer(window, pointer) -> None:
    """Send one EWMH move request after mapping, leaving later moves to the user."""
    if not isinstance(pointer, (list, tuple)) or len(pointer) != 2:
        return
    try:
        import gi
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11
        surface = window.get_surface()
        if not isinstance(surface, GdkX11.X11Surface):
            return
        monitors = surface.get_display().get_monitors()
        bounds = None
        for index in range(monitors.get_n_items()):
            monitor = monitors.get_item(index)
            geometry = monitor.get_geometry()
            scale = monitor.get_scale_factor()
            candidate = tuple(value * scale for value in
                              (geometry.x, geometry.y, geometry.width, geometry.height))
            if (candidate[0] <= pointer[0] < candidate[0] + candidate[2]
                    and candidate[1] <= pointer[1] < candidate[1] + candidate[3]):
                bounds = candidate
                break
        if bounds is None:
            return
        scale = surface.get_scale_factor()
        x, y = clamp_position(pointer, (surface.get_width() * scale,
                                       surface.get_height() * scale), bounds)
        with _x11() as (lib, display):
            if not display:
                return
            lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            lib.XInternAtom.restype = ctypes.c_ulong
            lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                      ctypes.c_long, ctypes.POINTER(_XEvent)]
            event = _XEvent()
            event.client.type = 33  # ClientMessage
            event.client.display = display
            event.client.window = surface.get_xid()
            event.client.message_type = lib.XInternAtom(display, b"_NET_MOVERESIZE_WINDOW", 0)
            event.client.format = 32
            event.client.data[0] = 1 | (1 << 8) | (1 << 9) | (1 << 12)
            event.client.data[1], event.client.data[2] = x, y
            lib.XSendEvent(display, lib.XDefaultRootWindow(display), 0,
                           (1 << 20) | (1 << 19), ctypes.byref(event))
            lib.XFlush(display)
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        pass
