"""macOS backend: screencapture, the Accessibility tree, Vision OCR, Quartz events.

The app that launches the process (Terminal, an IDE, ...) needs the Accessibility and Screen
Recording permissions; macOS grants them per host app.

Coordinates: every x/y the backend takes or returns is in "view" space = main-display points times
the scale factor, the same convention as the Windows backend.
"""
import ctypes
import io
import os
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import ApplicationServices as AS
import AppKit
import Quartz
import Vision
import objc
from Foundation import NSURL
from PIL import Image

# The first AppKit or Quartz call registers this process as a regular app, and it never runs an event
# loop, so the Dock shows a Python icon marked not responding. Force quitting that icon kills the MCP
# server and the client does not start it again. Background-only processes have no Dock icon.
AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)

VIEW_W = int(os.environ.get('CU_VIEW_W', '1600'))
TREE = 'ax'
TMP = Path(tempfile.gettempdir()) / 'reflexcu'
TMP.mkdir(exist_ok=True)


# ---- screen geometry (points <-> view) ----

def screen():
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return b.size.width, b.size.height


def scale():
    return min(1.0, VIEW_W / screen()[0])


def screen_size():
    w, h = screen()
    return int(w), int(h)


def scale_factor():
    return scale()


def view_size():
    w, h = screen()
    return round(w * scale()), round(h * scale())


def to_pts(x, y):
    k = scale()
    return x / k, y / k


def view_rect(x, y, w, h):
    k = scale()
    return [int(round(x * k)), int(round(y * k)), int(round((x + w) * k)), int(round((y + h) * k))]


def capture(region=None, fmt='png'):
    """Native-resolution capture of the main display; region is [x0, y0, x1, y1] in view space."""
    path = TMP / f'cap.{fmt}'
    cmd = ['screencapture', '-x', '-D', '1', '-t', fmt]
    if region:
        x0, y0 = to_pts(region[0], region[1])
        x1, y1 = to_pts(region[2], region[3])
        cmd += ['-R', f'{x0:.0f},{y0:.0f},{max(1, x1 - x0):.0f},{max(1, y1 - y0):.0f}']
    subprocess.run(cmd + [str(path)], check=True, timeout=15)
    return path


def window_image(region, info):
    """`region` (view space) of a window that may be covered by others: the window's own pixels, not the screen's."""
    path = TMP / 'win.png'
    subprocess.run(['screencapture', '-x', '-o', '-l', str(info['hwnd']), '-t', 'png', str(path)], check=True, timeout=15)
    img = Image.open(path).convert('RGB')
    x0, y0, x1, y1 = info['rect']
    kx, ky = img.width / max(1, x1 - x0), img.height / max(1, y1 - y0)
    return img.crop((round((region[0] - x0) * kx), round((region[1] - y0) * ky),
                     round((region[2] - x0) * kx), round((region[3] - y0) * ky)))


def grab(region=None, zoom=False, quality=70, window=None):
    # The foreground window goes through the screen, so that its open menus are in the picture.
    img = window_image(region, window) if window and region and not window['foreground'] \
        else Image.open(capture(region)).convert('RGB')
    if not zoom:
        w, h = (region[2] - region[0], region[3] - region[1]) if region else [round(v * scale()) for v in screen()]
        if (w, h) != img.size:
            img = img.resize((max(1, int(w)), max(1, int(h))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=quality)
    return img, buf.getvalue()


# ---- windows ----

def front_pid():
    err, app = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateSystemWide(), 'AXFocusedApplication', None)
    if err == 0 and app is not None:
        err, pid = AS.AXUIElementGetPid(app, None)
        if err == 0:
            return pid
    for w in _cg_windows():
        return w['kCGWindowOwnerPID']
    return None


def front_app():
    """The frontmost app as the system tracks it. It can differ from the app with keyboard focus: pressing a
    control through accessibility can move focus to an app that stays behind in this list."""
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app is not None else None


def _cg_windows():
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    return [w for w in Quartz.CGWindowListCopyWindowInfo(opts, 0) or []
            if w.get('kCGWindowLayer') == 0 and w['kCGWindowBounds']['Width'] > 40 and w['kCGWindowBounds']['Height'] > 40]


def overlays(pid):
    """Windows of an app that has none on the normal layer: Spotlight and other system panels take the
    keyboard from a floating panel, so without this there is no foreground window at all while they are up."""
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    return [w for w in Quartz.CGWindowListCopyWindowInfo(opts, 0) or []
            if w['kCGWindowOwnerPID'] == pid and w.get('kCGWindowLayer', 0) not in (0, 24, 25) and w.get('kCGWindowAlpha', 1) > 0
            and w['kCGWindowBounds']['Width'] > 40 and w['kCGWindowBounds']['Height'] > 40]


def popups(pid):
    """Open menus and popovers of an app: separate windows above the normal layer, invisible to the window's AX tree."""
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, 0) or []:
        b, layer = w['kCGWindowBounds'], w.get('kCGWindowLayer', 0)
        # layer 24/25 are the menu bar and its status items
        if w['kCGWindowOwnerPID'] == pid and layer > 0 and layer not in (24, 25) and b['Width'] > 30 and b['Height'] > 15 \
                and w.get('kCGWindowAlpha', 1) > 0:
            out.append(view_rect(b['X'], b['Y'], b['Width'], b['Height']))
    return out


def list_windows():
    fp, out, seen_front = front_pid(), [], False
    wins = _cg_windows()
    panel = overlays(fp) if fp is not None and not any(w['kCGWindowOwnerPID'] == fp for w in wins) else []
    for w in panel + wins:  # front to back
        b = w['kCGWindowBounds']
        fg = w['kCGWindowOwnerPID'] == fp and not seen_front
        seen_front = seen_front or fg
        out.append({'hwnd': int(w['kCGWindowNumber']), 'title': w.get('kCGWindowName') or '',
                    'exe': w.get('kCGWindowOwnerName') or '', 'pid': int(w['kCGWindowOwnerPID']),
                    'rect': view_rect(b['X'], b['Y'], b['Width'], b['Height']), 'minimized': False, 'foreground': fg})
        if w in panel:
            out[-1]['overlay'] = True
    return out


def find_window(spec):
    """spec: window number, or a case-insensitive substring of the window title or app name."""
    wins = list_windows()
    if spec in (None, ''):
        for w in wins:
            if w['foreground']:
                return w
        raise ValueError('no foreground window')
    if isinstance(spec, int) or str(spec).isdigit():
        for w in wins:
            if w['hwnd'] == int(spec):
                return w
        raise ValueError(f'no window {spec}')
    s = str(spec).lower()
    for test in (lambda w: s == w['exe'].lower() or s == w['title'].lower(),
                 lambda w: s in w['title'].lower(), lambda w: s in w['exe'].lower()):
        for w in wins:
            if test(w):
                return w
    raise ValueError(f'no window matches {spec!r}')


def ax_app(pid):
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 1.5)
    return app


def ax_get(el, attr):
    err, v = AS.AXUIElementCopyAttributeValue(el, attr, None)
    return v if err == 0 else None


def ax_window(info):
    """The AX element of the CG window `info` (matched by title, then by position)."""
    app = ax_app(info['pid'])
    wins = ax_get(app, 'AXWindows') or []
    for w in wins:
        if (ax_get(w, 'AXTitle') or '') == info['title'] and info['title']:
            return app, w
    for w in wins:
        r = ax_rect(ax_get(w, 'AXPosition'), ax_get(w, 'AXSize'))
        r = r and view_rect(*r)
        if r and abs(r[0] - info['rect'][0]) < 3 and abs(r[1] - info['rect'][1]) < 3:
            return app, w
    return app, ax_get(app, 'AXFocusedWindow') or (wins[0] if wins else None)


def focus(spec):
    info = find_window(spec)
    app, win = ax_window(info)
    if win is not None:
        AS.AXUIElementPerformAction(win, 'AXRaise')
    AS.AXUIElementSetAttributeValue(app, 'AXFrontmost', True)
    # A window coming from another Space or from behind a full-screen app takes a moment to be on screen.
    for _ in range(8):
        time.sleep(0.25)
        try:
            return find_window(info['hwnd'])
        except ValueError:
            pass
    return info


def idle_seconds():
    return round(Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, 0xFFFFFFFF), 1)


# ---- input ----

_posted = 0.0  # when this process last sent real input, to tell the user's activity from its own


def post(ev, pid=None):
    global _posted
    if pid:
        Quartz.CGEventPostToPid(pid, ev)
    else:
        _posted = time.time()
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def user_busy(quiet=1.5):
    """Did the user, not this process, touch the mouse or keyboard within `quiet` seconds?"""
    idle = idle_seconds()
    return idle < quiet and time.time() - _posted > idle + 0.05


def activate(el, button='left', count=1):
    """Operate a control through accessibility: no cursor, no typing focus needed. Only for the foreground
    window, since an app may raise the window and take the keyboard when pressed this way (Chrome's new tab
    button does). False when this control has no such route and it takes a real click."""
    ax = el.get('ax')
    if ax is None or count != 1:
        return False
    if button == 'left' and el.get('field'):
        return AS.AXUIElementSetAttributeValue(ax, 'AXFocused', True) == 0
    err, names = AS.AXUIElementCopyActionNames(ax, None)
    action = {'left': 'AXPress', 'right': 'AXShowMenu'}.get(button)
    return err == 0 and action in (names or []) and AS.AXUIElementPerformAction(ax, action) == 0


# ---- input to a window behind others ----
# An app hit-tests a posted mouse event with the window-local position inside the event, which the window
# server fills in only for events it routes itself. With that set, an event posted to the process reaches
# the view under it, and the real cursor and the frontmost app stay as they are. Many views also ignore
# input while their app is inactive, so the app is first told it was activated, by an event sent to it alone.

class _Point(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


_cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
_cg.CGEventSetWindowLocation.argtypes = [ctypes.c_void_p, _Point]
F_TARGET_PID, F_WINDOW, F_UNDER_POINTER, F_CAN_HANDLE, F_SUBTYPE = 40, 51, 91, 92, 7
TITLE_BAR = (30, 16)  # points from the window's corner; a click here makes the window key
_click_number = 90000


def window_origin(info):
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, info['hwnd']) or []:
        return w['kCGWindowBounds']['X'], w['kCGWindowBounds']['Y']
    raise ValueError(f"window {info['hwnd']} is gone")


def stamp(ev, info, wx, wy, ox, oy):
    fields = ((F_TARGET_PID, info['pid']), (F_WINDOW, info['hwnd']), (F_UNDER_POINTER, info['hwnd']),
              (F_CAN_HANDLE, info['hwnd']), (F_SUBTYPE, 3), (101, 20), (102, 63))
    for field, value in fields:
        Quartz.CGEventSetIntegerValueField(ev, field, value)
    Quartz.CGEventSetLocation(ev, (ox + wx, oy + wy))
    _cg.CGEventSetWindowLocation(objc.pyobjc_id(ev), _Point(wx, wy))
    post(ev, info['pid'])


def window_mouse(info, kind, wx, wy, origin, button=0, clicks=1, number=0):
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (origin[0] + wx, origin[1] + wy), button)
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventNumber, number)
    stamp(ev, info, wx, wy, *origin)


def next_click():
    global _click_number
    _click_number += 1
    return _click_number


def appkit_event(info, subtype, flags=0):
    ev = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        AppKit.NSEventTypeAppKitDefined, (0, 0), flags, AppKit.NSProcessInfo.processInfo().systemUptime(),
        info['hwnd'] if subtype == AppKit.NSEventSubtypeApplicationActivated else 0, None, subtype, 0, 0)
    post(ev.CGEvent(), info['pid'])


_inside = {'window': None, 'timer': None, 'restored': None}


def guard_focus(info, before):
    """Some actions make the app raise itself and take the keyboard (Chrome does on a new tab). Give the
    keyboard back to the app that had it, The user going to that app on their own within the same
    second would be undone too, which is rare and cheap next to their typing landing in it."""
    for _ in range(40):
        if front_pid() == info['pid'] and before != info['pid']:
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(before)
            if app is None or not app.activateWithOptions_(0):
                return None
            _inside['window'] = None  # the app was really deactivated: the next call has to tell it again
            for _ in range(40):
                if front_pid() == before:
                    break
                time.sleep(0.01)
            return app.localizedName()
        time.sleep(0.015)
    return None


def pop_restored():
    name, _inside['restored'] = _inside['restored'], None
    return {'focus_restored_to': name} if name else {}


def leave():
    info, _inside['window'] = _inside['window'], None
    if info and info['pid'] != front_app():  # the user may have switched to it meanwhile
        appkit_event(info, AppKit.NSEventSubtypeApplicationDeactivated)


@contextmanager
def inside(info):
    """Run with the app of `info` believing it is frontmost and that window key. The belief outlasts the call,
    since telling the app it lost focus clears what the last call left, like the selection in Chrome's
    address bar, which the next call may need."""
    if _inside['timer']:
        _inside['timer'].cancel()
    origin = window_origin(info)
    before = front_pid()
    if not _inside['window'] or _inside['window']['hwnd'] != info['hwnd']:
        leave()
        appkit_event(info, AppKit.NSEventSubtypeApplicationActivated, 0xc0000)
        time.sleep(0.03)
        down, up, _, b = MOUSE['left']
        n = next_click()
        window_mouse(info, down, *TITLE_BAR, origin, b, 1, n)
        time.sleep(0.05)
        window_mouse(info, up, *TITLE_BAR, origin, b, 1, n)
        time.sleep(0.12)
        _inside['window'] = info
    try:
        yield origin
    finally:
        _inside['restored'] = guard_focus(info, before) if before else None
        _inside['timer'] = threading.Timer(5, leave)
        _inside['timer'].daemon = True
        _inside['timer'].start()


def local(x, y, origin):
    px, py = to_pts(x, y)
    return px - origin[0], py - origin[1]


def bg_click(info, x, y, button='left', count=1, hold=0.04):
    down, up, _, b = MOUSE[button]
    with inside(info) as origin:
        wx, wy = local(x, y, origin)
        window_mouse(info, Quartz.kCGEventMouseMoved, wx, wy, origin)
        time.sleep(0.04)
        n = next_click()
        for i in range(count):
            window_mouse(info, down, wx, wy, origin, b, i + 1, n)
            time.sleep(hold)
            window_mouse(info, up, wx, wy, origin, b, i + 1, n)
            if i + 1 < count:
                time.sleep(0.08)
        time.sleep(0.1)


def bg_drag(info, x0, y0, x1, y1, button='left', seconds=0.4):
    down, up, dragged, b = MOUSE[button]
    with inside(info) as origin:
        ax0, ay0 = local(x0, y0, origin)
        ax1, ay1 = local(x1, y1, origin)
        n = next_click()
        window_mouse(info, Quartz.kCGEventMouseMoved, ax0, ay0, origin)
        time.sleep(0.04)
        window_mouse(info, down, ax0, ay0, origin, b, 1, n)
        time.sleep(0.08)
        steps = max(4, int(seconds / 0.016))
        for i in range(1, steps + 1):
            window_mouse(info, dragged, ax0 + (ax1 - ax0) * i / steps, ay0 + (ay1 - ay0) * i / steps, origin, b, 1, n)
            time.sleep(seconds / steps)
        window_mouse(info, up, ax1, ay1, origin, b, 1, n)
        time.sleep(0.1)


def bg_scroll(info, x, y, dy=0, dx=0):
    with inside(info) as origin:
        wx, wy = local(x, y, origin)
        window_mouse(info, Quartz.kCGEventMouseMoved, wx, wy, origin)
        time.sleep(0.04)
        wheel = [(-1 if dy > 0 else 1, 0)] * abs(int(dy)) + [(0, -1 if dx > 0 else 1)] * abs(int(dx))
        for v, h in wheel:
            stamp(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, v, h), info, wx, wy, *origin)
            time.sleep(0.02)
        time.sleep(0.1)


MOUSE = {'left': (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged, 0),
         'right': (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged, 1),
         'middle': (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGEventOtherMouseDragged, 2)}


def mouse_event(kind, x, y, button=0, clicks=1):
    ev = Quartz.CGEventCreateMouseEvent(None, kind, to_pts(x, y), button)
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
    post(ev)


def cursor():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    k = scale()
    return int(p.x * k), int(p.y * k)


def move_rel(dx, dy):
    cx, cy = cursor()
    move_to(cx + int(dx), cy + int(dy))


def move_to(x, y):
    mouse_event(Quartz.kCGEventMouseMoved, x, y)


def click(x, y, button='left', count=1, hold=0.04):
    down, up, _, b = MOUSE[button]
    move_to(x, y)
    time.sleep(0.05)
    for i in range(count):
        mouse_event(down, x, y, b, i + 1)
        time.sleep(hold)
        mouse_event(up, x, y, b, i + 1)
        if i + 1 < count:
            time.sleep(0.08)


def drag(x0, y0, x1, y1, button='left', seconds=0.4):
    down, up, dragged, b = MOUSE[button]
    move_to(x0, y0)
    time.sleep(0.05)
    mouse_event(down, x0, y0, b)
    time.sleep(0.15)  # the window server drops motion that arrives before it has recognised the drag
    steps = max(4, int(seconds / 0.016))
    for i in range(1, steps + 1):
        mouse_event(dragged, x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, b)
        time.sleep(seconds / steps)
    time.sleep(0.12)
    mouse_event(dragged, x1, y1, b)
    time.sleep(0.05)
    mouse_event(up, x1, y1, b)


def scroll(x, y, dy=0, dx=0):
    """dy > 0 scrolls down, in lines."""
    move_to(x, y)
    time.sleep(0.05)
    for _ in range(abs(int(dy))):
        post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, -1 if dy > 0 else 1, 0))
        time.sleep(0.02)
    for _ in range(abs(int(dx))):
        post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, 0, -1 if dx > 0 else 1))
        time.sleep(0.02)


KEYCODE = {**dict(zip('asdfhgzxcv', range(10))), 'b': 11, 'q': 12, 'w': 13, 'e': 14, 'r': 15, 'y': 16, 't': 17,
           '1': 18, '2': 19, '3': 20, '4': 21, '6': 22, '5': 23, '=': 24, '9': 25, '7': 26, '-': 27, '8': 28, '0': 29,
           ']': 30, 'o': 31, 'u': 32, '[': 33, 'i': 34, 'p': 35, 'l': 37, 'j': 38, "'": 39, 'k': 40, ';': 41,
           '\\': 42, ',': 43, '/': 44, 'n': 45, 'm': 46, '.': 47, '`': 50,
           'enter': 36, 'return': 36, 'tab': 48, 'space': 49, 'backspace': 51, 'esc': 53, 'escape': 53,
           'cmd': 55, 'command': 55, 'win': 55, 'shift': 56, 'capslock': 57, 'alt': 58, 'option': 58, 'opt': 58,
           'ctrl': 59, 'control': 59, 'fn': 63, 'home': 115, 'pageup': 116, 'delete': 117, 'del': 117, 'end': 119,
           'pagedown': 121, 'left': 123, 'right': 124, 'down': 125, 'up': 126,
           'f1': 122, 'f2': 120, 'f3': 99, 'f4': 118, 'f5': 96, 'f6': 97, 'f7': 98, 'f8': 100, 'f9': 101,
           'f10': 109, 'f11': 103, 'f12': 111}
FLAGS = {55: Quartz.kCGEventFlagMaskCommand, 56: Quartz.kCGEventFlagMaskShift,
         58: Quartz.kCGEventFlagMaskAlternate, 59: Quartz.kCGEventFlagMaskControl}


def keycode(name):
    n = name.strip().lower() or name  # a bare ' ' is not a key name; use "space"
    if n not in KEYCODE:
        raise ValueError(f'unknown key {name!r}')
    return KEYCODE[n]


def key_event(code, down, flags, pid=None):
    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    Quartz.CGEventSetFlags(ev, flags)
    post(ev, pid)


def press(combo, hold=0.05, times=1, pid=None):
    codes = [keycode(k) for k in combo.split('+')]
    for i in range(times):
        flags = 0
        for c in codes:
            flags |= FLAGS.get(c, 0)
            key_event(c, True, flags, pid)
            time.sleep(0.01)
        time.sleep(hold)
        for c in reversed(codes):
            flags &= ~FLAGS.get(c, 0)
            key_event(c, False, flags, pid)
        if i + 1 < times:
            time.sleep(0.06)


def type_text(text, paste=False, pid=None):
    if paste:
        pb = AppKit.NSPasteboard.generalPasteboard()
        saved = [{t: i.dataForType_(t) for t in i.types()} for i in pb.pasteboardItems() or []]
        pb.clearContents()
        pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
        time.sleep(0.12)
        press('cmd+v', pid=pid)
        time.sleep(0.3)  # the app reads the clipboard after it handles the key
        pb.clearContents()
        items = []
        for types in saved:
            item = AppKit.NSPasteboardItem.alloc().init()
            for t, data in types.items():
                if data is not None:
                    item.setData_forType_(data, t)
            items.append(item)
        if items:
            pb.writeObjects_(items)
        return
    for ch in text:
        if ch == '\n':
            press('enter', pid=pid)
            continue
        n = len(ch.encode('utf-16-le')) // 2
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventSetFlags(ev, 0)
            Quartz.CGEventKeyboardSetUnicodeString(ev, n, ch)
            post(ev, pid)
        time.sleep(0.008)


# ---- observation: Accessibility tree + Vision OCR ----

ROLES = {'AXButton': '按钮', 'AXCheckBox': '复选框', 'AXPopUpButton': '下拉框', 'AXComboBox': '下拉框',
         'AXTextField': '输入框', 'AXTextArea': '输入框', 'AXSearchField': '输入框', 'AXLink': '链接',
         'AXImage': '图片', 'AXMenuItem': '菜单项', 'AXMenuBarItem': '菜单', 'AXMenuButton': '按钮',
         'AXRadioButton': '单选', 'AXSlider': '滑块', 'AXStaticText': '文本', 'AXRow': '行', 'AXCell': '单元格',
         'AXHeading': '标题', 'AXDisclosureTriangle': '展开箭头', 'AXTabGroup': '标签页组', 'AXToolbar': '工具栏',
         'AXIncrementor': '微调', 'AXColorWell': '颜色', 'AXOutline': '列表', 'AXTable': '表格', 'AXList': '列表'}
CLICKABLE = {'AXButton', 'AXCheckBox', 'AXPopUpButton', 'AXComboBox', 'AXTextField', 'AXTextArea', 'AXSearchField',
             'AXLink', 'AXMenuItem', 'AXMenuBarItem', 'AXMenuButton', 'AXRadioButton', 'AXSlider', 'AXRow', 'AXCell',
             'AXDisclosureTriangle', 'AXIncrementor'}
ATTRS = ['AXRole', 'AXSubrole', 'AXTitle', 'AXDescription', 'AXValue', 'AXPosition', 'AXSize', 'AXChildren', 'AXEnabled',
         'AXPlaceholderValue']
FIELDS = {'AXTextField', 'AXTextArea', 'AXSearchField', 'AXComboBox'}


def ax_rect(pos, size):
    try:
        ok1, p = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        ok2, s = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        return (p.x, p.y, s.width, s.height) if ok1 and ok2 else None
    except Exception:
        return None


_awake = set()


def wake_tree(info, app, win):
    """Electron and Chromium build their accessibility tree only once a client asks for it, which takes a
    moment; reading right away gets an empty page. A foreground window has usually been asked already."""
    manual = AS.AXUIElementSetAttributeValue(app, 'AXManualAccessibility', True)
    if info['pid'] in _awake:
        return
    _awake.add(info['pid'])
    bundle = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(info['pid'])
    bundle = bundle and bundle.bundleURL()
    if bundle is None or not list(Path(bundle.path()).glob('Contents/Frameworks/*.framework/Helpers')):
        return
    if manual != 0:  # Chrome itself answers only to the older switch, and reports an error even as it obeys
        AS.AXUIElementSetAttributeValue(app, 'AXEnhancedUserInterface', True)
    for _ in range(12):
        queue = [win]
        while queue:
            el = queue.pop()
            if ax_get(el, 'AXRole') == 'AXWebArea' and ax_get(el, 'AXChildren'):
                return
            queue += list(ax_get(el, 'AXChildren') or [])[:40]
        time.sleep(0.25)


def observe_tree(info, limit=600, budget=3.0):
    app, win = ax_window(info)
    if win is not None:
        wake_tree(info, app, win)
    out, deadline = [], time.time() + budget
    wx0, wy0, wx1, wy1 = info['rect']
    queue = [win] if win is not None else []
    menubar = ax_get(app, 'AXMenuBar') if info['foreground'] else None
    queue += list(ax_get(menubar, 'AXChildren') or []) if menubar is not None else []
    seen = 0
    while queue and len(out) < limit and seen < 4000 and time.time() < deadline:
        el = queue.pop(0)
        seen += 1
        err, vals = AS.AXUIElementCopyMultipleAttributeValues(el, ATTRS, 0, None)
        if err != 0 or vals is None:
            continue
        # Attributes an element does not have come back as AXValue error markers, not None.
        role, sub, title, desc, value = [v if isinstance(v, str) else None for v in vals[:5]]
        pos, size = vals[5], vals[6]
        kids = vals[7] if hasattr(vals[7], '__len__') and not isinstance(vals[7], str) else None
        enabled = vals[8] if isinstance(vals[8], bool) else None
        r = ax_rect(pos, size)
        if role != 'AXMenuBarItem' and kids:
            queue += list(kids)
        if not isinstance(role, str) or not r or r[2] < 3 or r[3] < 3:
            continue
        rect = view_rect(*r)
        if role != 'AXMenuBarItem' and (rect[2] <= wx0 or rect[0] >= wx1 or rect[3] <= wy0 or rect[1] >= wy1):
            continue
        holds = role in FIELDS or role == 'AXPopUpButton'   # controls whose value is their content, not their name
        hint = vals[9] if role in FIELDS and isinstance(vals[9], str) else None
        named = next((t.strip() for t in (title, desc, hint) if isinstance(t, str) and t.strip()), '')
        shown = value.strip() if isinstance(value, str) and (holds or role in ('AXStaticText', 'AXLink', 'AXHeading')) else ''
        text = named or shown
        if not text and role not in CLICKABLE:
            continue
        if role in ('AXRow', 'AXCell') and not text:
            continue
        item = {'role': '标签页' if sub == 'AXTabButton' else ROLES.get(role, '元素'), 'text': text[:120].replace('\n', ' '),
                'rect': rect, 'src': 'ax', 'clickable': role in CLICKABLE, 'ax': el, 'field': role in FIELDS}
        if role in ('AXCheckBox', 'AXSwitch', 'AXRadioButton') and isinstance(vals[4], (int, float)) and not isinstance(vals[4], bool):
            item['state'] = '开' if vals[4] else '关'
        elif holds and named and sub != 'AXSecureTextField' and isinstance(value, str):
            # what a field contains, or what a popup button is set to; without it "is the box filled in?" cannot be answered
            v = ' '.join(value.split())
            if role == 'AXPopUpButton':
                item['state'] = '当前：' + v[:40] if v else ''
            elif v != named:
                item['state'] = '内容：' + v[:60] if v else '内容为空'
            if not item['state']:
                del item['state']
        if enabled is False:
            item['disabled'] = True
        out.append(item)
    return out


def observe_ocr(region=None, window=None):
    if window and region and not window['foreground']:
        img = window_image(region, window)
        path = TMP / 'ocr.png'
        img.save(path)
    else:
        path = capture(region)
        img = Image.open(path)
    vw0 = (region[2] - region[0]) if region else screen()[0] * scale()
    if img.width < vw0 * 1.5:
        # On a non-Retina display UI text is ~10 px tall and Vision skips whole lines; enlarge first.
        img = img.convert('RGB').resize((img.width * 2, img.height * 2), Image.LANCZOS)
        path = TMP / 'ocr.png'
        img.save(path)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(['zh-Hans', 'en-US'])
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f'OCR failed: {err}')
    # Vision boxes are normalised with the origin at the bottom-left of the captured image.
    vw, vh = (region[2] - region[0], region[3] - region[1]) if region else [v * scale() for v in screen()]
    ox, oy = (region[0], region[1]) if region else (0, 0)
    out = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if not cand or cand[0].confidence() < 0.2 or not cand[0].string().strip():
            continue
        b = obs.boundingBox()
        x0, x1 = b.origin.x * vw + ox, (b.origin.x + b.size.width) * vw + ox
        y0, y1 = (1 - b.origin.y - b.size.height) * vh + oy, (1 - b.origin.y) * vh + oy
        out.append({'role': '文字', 'text': cand[0].string().strip()[:120], 'src': 'ocr', 'clickable': True,
                    'rect': [int(x0), int(y0), int(x1), int(y1)]})
    return out


def observe_popups(info):
    if info.get('overlay'):
        return []   # the panel is the window itself here, and has already been read
    els = []
    vw, vh = view_size()
    for r in popups(info['pid']):
        area = [max(0, r[0]), max(0, r[1]), min(vw, r[2]), min(vh, r[3])]
        if area[2] > area[0] and area[3] > area[1]:
            els += [dict(e, role='菜单项') for e in observe_ocr(area)]
    return els


def status_extra():
    return {'os': 'macOS', 'accessibility': bool(AS.AXIsProcessTrusted()),
            'screen_recording': bool(Quartz.CGPreflightScreenCaptureAccess())}
