#!/usr/bin/env python3
"""Fingerprint Manager: guided fingerprint enrollment and testing on top of fprintd."""

import datetime
import json
import math
import os
import re
import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango

APP_ID = "io.github.melhzy.FingerprintManager"
FPRINT = "net.reactivated.Fprint"
CALL_TIMEOUT_MS = 5 * 60 * 1000  # a call may sit behind a polkit password dialog
TEST_ROUNDS = 5

# Left to right, as the hands are drawn.
FINGERS = [
    "left-little-finger", "left-ring-finger", "left-middle-finger", "left-index-finger", "left-thumb",
    "right-thumb", "right-index-finger", "right-middle-finger", "right-ring-finger", "right-little-finger",
]
RECOMMENDED = ["right-index-finger", "right-thumb", "left-index-finger", "left-thumb"]

STATE_COLOR = {
    "good": (0.18, 0.76, 0.49),
    "untested": (0.21, 0.52, 0.89),
    "fair": (0.90, 0.65, 0.04),
    "poor": (0.88, 0.11, 0.14),
}

ERROR_TEXT = {
    "AlreadyInUse": "The sensor is busy. Another app or a password prompt is using it; close it and try again.",
    "PermissionDenied": "Permission wasn't granted. Changing fingerprints needs your password.",
    "NoSuchDevice": "No fingerprint sensor was found.",
    "ClaimDevice": "The sensor couldn't be opened.",
}
ENROLL_RETRY = {
    "enroll-retry-scan": "That touch wasn't read clearly. Lift your finger and touch again.",
    "enroll-swipe-too-short": "The swipe was too short. Try again a little slower.",
    "enroll-finger-not-centered": "Your finger was off-centre. Place it on the middle of the sensor.",
    "enroll-remove-and-retry": "Lift your finger fully off the sensor, then touch again.",
}
ENROLL_FAIL = {
    "enroll-failed": "The sensor couldn't build a usable fingerprint from these touches.",
    "enroll-data-full": "The sensor's storage is full. Delete a fingerprint you don't need, then try again.",
    "enroll-disconnected": "The sensor was disconnected.",
    "enroll-duplicate": "This finger is already enrolled under another name. "
                        "Delete that entry first, or use a different finger.",
    "enroll-unknown-error": "The sensor reported an error.",
}
VERIFY_RETRY = {
    "verify-retry-scan": "That touch wasn't read clearly. Lift your finger and touch again.",
    "verify-swipe-too-short": "The swipe was too short. Try again a little slower.",
    "verify-finger-not-centered": "Your finger was off-centre. Place it on the middle of the sensor.",
    "verify-remove-and-retry": "Lift your finger fully off the sensor, then touch again.",
}
VERIFY_FAIL = {
    "verify-disconnected": "The sensor was disconnected.",
    "verify-unknown-error": "The sensor reported an error.",
}


def label(finger):
    return finger.replace("-", " ")


class FprintError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name = name
        self.text = ERROR_TEXT.get(name, message)


def to_fprint_error(error):
    match = re.match(r"GDBus\.Error:([\w.]+): (.*)", error.message, re.S)
    if match:
        return FprintError(match.group(1).rsplit(".", 1)[-1], match.group(2))
    return FprintError("", error.message)


def run(gen):
    """Drive a generator that yields pending operations.

    A pending operation is a function taking a done(value, error) callback; the
    generator is resumed with the value, or has the error thrown into it.
    """
    def step(value=None, error=None):
        try:
            start = gen.throw(error) if error else gen.send(value)
        except StopIteration:
            return
        start(step)
    step()


def sleep(ms):
    def start(done):
        GLib.timeout_add(ms, lambda: done(None, None))
    return start


def quiet(device, *methods):
    """Call clean-up methods in order, ignoring failures (e.g. nothing to stop)."""
    for method in methods:
        try:
            yield device.call(method)
        except FprintError:
            pass


class Device:
    """The default fprintd reader. Events go to `listener(kind, *args)`."""

    def __init__(self):
        try:
            manager = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
                FPRINT, "/net/reactivated/Fprint/Manager", FPRINT + ".Manager", None)
            (path,) = manager.call_sync("GetDefaultDevice", None, Gio.DBusCallFlags.NONE, -1, None).unpack()
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None, FPRINT, path, FPRINT + ".Device", None)
        except GLib.Error as e:
            raise to_fprint_error(e) from None
        self.name = self.proxy.get_cached_property("name").unpack()
        self._stages = self.proxy.get_cached_property("num-enroll-stages").unpack()
        self.listener = None
        self.proxy.connect("g-signal", self._on_signal)
        self.proxy.connect("g-properties-changed", self._on_properties)

    @property
    def stages(self):
        # fprintd exits when idle, which empties the property cache until it is back.
        value = self.proxy.get_cached_property("num-enroll-stages")
        if value and value.unpack() > 0:
            self._stages = value.unpack()
        return self._stages

    def call(self, method, *args):
        params = GLib.Variant("(" + "s" * len(args) + ")", args) if args else None

        def start(done):
            def ready(proxy, result):
                try:
                    value = proxy.call_finish(result).unpack()
                except GLib.Error as e:
                    done(None, to_fprint_error(e))
                else:
                    done(value, None)
            self.proxy.call(method, params, Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION,
                            CALL_TIMEOUT_MS, None, ready)
        return start

    def _emit(self, kind, *args):
        if self.listener:
            self.listener(kind, *args)

    def _on_signal(self, _proxy, _sender, signal, params):
        self._emit(signal, *params.unpack())

    def _on_properties(self, _proxy, changed, _invalidated):
        changed = changed.unpack()
        if "finger-present" in changed:
            self._emit("finger-present", changed["finger-present"])


class Results:
    """Last test outcome per finger. Only counts and a date are stored."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        if path:
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                pass

    def _save(self):
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=1)

    def get(self, finger):
        return self.data.get(finger)

    def set(self, finger, matched, total):
        self.data[finger] = {"matched": matched, "total": total, "date": datetime.date.today().isoformat()}
        self._save()

    def drop(self, finger):
        if self.data.pop(finger, None):
            self._save()

    def keep_only(self, fingers):
        for finger in set(self.data) - set(fingers):
            self.drop(finger)


def faded(color, alpha):
    return Gdk.RGBA(color.red, color.green, color.blue, alpha)


def round_stroke(width):
    stroke = Gsk.Stroke.new(width)
    stroke.set_line_cap(Gsk.LineCap.ROUND)
    stroke.set_line_join(Gsk.LineJoin.ROUND)
    return stroke


def polyline(*points):
    builder = Gsk.PathBuilder.new()
    builder.move_to(*points[0])
    for point in points[1:]:
        builder.line_to(*point)
    return builder.to_path()


def circle(x, y, radius):
    builder = Gsk.PathBuilder.new()
    builder.add_circle(Graphene.Point().init(x, y), radius)
    return builder.to_path()


def draw_text(widget, snapshot, text, size, bold, cx, cy, color):
    layout = widget.create_pango_layout(text)
    font = layout.get_context().get_font_description().copy()
    font.set_absolute_size(size * Pango.SCALE)
    font.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    layout.set_font_description(font)
    width, height = layout.get_pixel_size()
    snapshot.save()
    snapshot.translate(Graphene.Point().init(cx - width / 2, cy - height / 2))
    snapshot.append_layout(layout, color)
    snapshot.restore()


def finger_shapes():
    """Each finger as (base, tip, width) on a 560x262 canvas, hands seen from above."""
    right = {
        "thumb": ((92, 185), (38, 128), 30),
        "index-finger": ((97, 135), (92, 52), 26),
        "middle-finger": ((126, 130), (126, 32), 26),
        "ring-finger": ((155, 133), (160, 48), 26),
        "little-finger": ((183, 142), (194, 84), 23),
    }
    shapes = {}
    for name, ((bx, by), (tx, ty), width) in right.items():
        shapes["right-" + name] = ((300 + bx, by), (300 + tx, ty), width)
        shapes["left-" + name] = ((260 - bx, by), (260 - tx, ty), width)
    return shapes


class HandsView(Gtk.Widget):
    """Both hands, with a status dot on each fingertip. Click a finger to select it."""

    WIDTH, HEIGHT = 560, 262
    PALMS = (60, 380)  # x of each 120-wide palm
    SHAPES = finger_shapes()

    def __init__(self, win):
        super().__init__(hexpand=True, focusable=True, has_tooltip=True)
        self.set_size_request(360, self.HEIGHT)
        self.win = win
        self.hover = None
        self.scale, self.offset = 1, (0, 0)
        click = Gtk.GestureClick()
        click.connect("pressed", self.on_click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self.set_hover(self.finger_at(x, y)))
        motion.connect("leave", lambda _c: self.set_hover(None))
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        self.connect("query-tooltip", self.on_tooltip)

    def finger_at(self, x, y):
        x = (x - self.offset[0]) / self.scale
        y = (y - self.offset[1]) / self.scale
        for finger, ((bx, by), (tx, ty), width) in self.SHAPES.items():
            dx, dy = tx - bx, ty - by
            t = max(0, min(1, ((x - bx) * dx + (y - by) * dy) / (dx * dx + dy * dy)))
            if math.hypot(x - bx - t * dx, y - by - t * dy) <= width / 2 + 4:
                return finger
        return None

    def set_hover(self, finger):
        if finger != self.hover:
            self.hover = finger
            self.set_cursor(Gdk.Cursor.new_from_name("pointer") if finger else None)
            self.queue_draw()

    def on_click(self, _gesture, _n, x, y):
        self.grab_focus()
        finger = self.finger_at(x, y)
        if finger:
            self.win.select(finger)

    def on_key(self, _controller, keyval, _keycode, _state):
        step = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1}.get(keyval)
        if step is None:
            return False
        index = (FINGERS.index(self.win.selected) + step) % len(FINGERS)
        self.win.select(FINGERS[index])
        return True

    def on_tooltip(self, _widget, x, y, _keyboard, tooltip):
        finger = self.finger_at(x, y)
        if not finger:
            return False
        tooltip.set_text(f"{label(finger).capitalize()}: {self.win.status_text(finger)}")
        return True

    def stroke_finger(self, snapshot, finger, color):
        base, tip, width = self.SHAPES[finger]
        snapshot.append_stroke(polyline(base, tip), round_stroke(width), color)

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        self.scale = min(width / self.WIDTH, height / self.HEIGHT)
        self.offset = ((width - self.WIDTH * self.scale) / 2, (height - self.HEIGHT * self.scale) / 2)
        snapshot.save()
        snapshot.translate(Graphene.Point().init(*self.offset))
        snapshot.scale(self.scale, self.scale)
        fg = self.get_color()
        solid = faded(fg, 1)

        # Silhouettes, drawn opaque inside one opacity group so overlapping parts don't darken.
        snapshot.push_opacity(0.13)
        for x in self.PALMS:
            palm = Gsk.RoundedRect()
            palm.init_from_rect(Graphene.Rect().init(x, 120, 120, 105), 30)
            builder = Gsk.PathBuilder.new()
            builder.add_rounded_rect(palm)
            snapshot.append_fill(builder.to_path(), Gsk.FillRule.WINDING, solid)
        for finger in FINGERS:
            self.stroke_finger(snapshot, finger, solid)
        snapshot.pop()

        if self.hover and self.hover != self.win.selected:
            self.stroke_finger(snapshot, self.hover, faded(fg, 0.10))
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        self.stroke_finger(snapshot, self.win.selected, faded(accent, 0.45))

        for finger in FINGERS:
            self.draw_dot(snapshot, fg, *self.SHAPES[finger][1], self.win.state_of(finger))
        draw_text(self, snapshot, "Left hand", 12, False, self.PALMS[0] + 60, 246, faded(fg, 0.6))
        draw_text(self, snapshot, "Right hand", 12, False, self.PALMS[1] + 60, 246, faded(fg, 0.6))
        snapshot.restore()

    def draw_dot(self, snapshot, fg, x, y, state):
        dot = circle(x, y, 9)
        if state == "none":
            snapshot.append_stroke(dot, Gsk.Stroke.new(1.5), faded(fg, 0.4))
            return
        white = Gdk.RGBA(1, 1, 1, 1)
        snapshot.append_fill(dot, Gsk.FillRule.WINDING, Gdk.RGBA(*STATE_COLOR[state], 1))
        if state == "good":
            tick = polyline((x - 4, y), (x - 1, y + 3.5), (x + 4.5, y - 3.5))
            snapshot.append_stroke(tick, round_stroke(2), white)
        else:
            draw_text(self, snapshot, "?" if state == "untested" else "!", 12, True, x, y, white)


class Ring(Gtk.Widget):
    """A segmented progress ring with a count in the middle."""

    def __init__(self):
        super().__init__(halign=Gtk.Align.CENTER)
        self.set_size_request(220, 220)
        self.segments, self.big, self.small = [], "", ""
        self.flashing = False

    def set(self, segments, big, small):
        self.segments, self.big, self.small = segments, big, small
        self.queue_draw()

    def flash(self):
        """Briefly turn the count amber to mark a rejected touch."""
        self.flashing = True
        self.queue_draw()
        GLib.timeout_add(600, self.unflash)

    def unflash(self):
        self.flashing = False
        self.queue_draw()
        return False

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        fg = self.get_color()
        cx, cy, radius = width / 2, height / 2, min(width, height) / 2 - 10
        count = len(self.segments)
        for i, state in enumerate(self.segments):
            color = faded(fg, 0.15) if state == "pending" else Gdk.RGBA(*STATE_COLOR[state], 1)
            if count == 1:
                arc = circle(cx, cy, radius)
            else:
                start = -math.pi / 2 + i * 2 * math.pi / count + 0.08
                end = -math.pi / 2 + (i + 1) * 2 * math.pi / count - 0.08
                arc = Gsk.Path.parse(
                    f"M {cx + radius * math.cos(start)} {cy + radius * math.sin(start)} "
                    f"A {radius} {radius} 0 {int(end - start > math.pi)} 1 "
                    f"{cx + radius * math.cos(end)} {cy + radius * math.sin(end)}")
            snapshot.append_stroke(arc, round_stroke(9), color)
        count_color = Gdk.RGBA(*STATE_COLOR["fair"], 1) if self.flashing else fg
        draw_text(self, snapshot, self.big, 56, True, cx, cy - 10, count_color)
        draw_text(self, snapshot, self.small, 15, False, cx, cy + 34, faded(fg, 0.6))


class SessionPage(Adw.NavigationPage):
    """A guided sensor session: a live view with a progress ring, then a result view.

    state: idle -> starting -> running -> stopping -> done. Closing the page
    while starting sets "cancelled", which begin() notices once its call returns.
    """

    stop_method = None

    def __init__(self, win, finger, title):
        super().__init__(title=title)
        self.win = win
        self.device = win.device
        self.finger = finger
        self.state = "idle"

        self.ring = Ring()
        self.instruction = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["title-1"])
        self.hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, height_request=44,
                              valign=Gtk.Align.START, css_classes=["dim-label"])
        self.counts = Gtk.Label(css_classes=["caption", "dim-label"])
        cancel = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER, css_classes=["pill"])
        cancel.connect("clicked", lambda *_: win.nav.pop())
        live = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER,
                       margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        for child in (self.ring, self.instruction, self.hint, self.counts, cancel):
            live.append(child)

        self.result = Adw.StatusPage()
        self.result_buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.result.set_child(self.result_buttons)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(live, "live")
        self.stack.add_named(self.result, "result")
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.connect("hiding", self.on_hiding)

    def start(self):
        self.state = "starting"
        self.instruction.set_label("Getting the sensor ready…")
        run(self.begin())

    def claim(self):
        try:
            yield self.device.call("Claim", "")
        except FprintError as e:
            self.state = "done"
            self.show_error("Couldn't use the sensor", e.text)
            return False
        return True

    def on_hiding(self, *_):
        if self.state == "starting":
            self.state = "cancelled"
        elif self.state == "running":
            run(self.shutdown())

    def shutdown(self):
        """Stop the sensor action, release the reader and reload the finger list."""
        self.state = "stopping"
        self.device.listener = None
        yield from quiet(self.device, self.stop_method, "Release")
        yield from self.win.reload()
        self.state = "done"

    def show_result(self, icon, title, description, buttons):
        """buttons: (label, callback) pairs; the first one is the suggested action."""
        self.result.set_icon_name(icon)
        self.result.set_title(title)
        self.result.set_description(description)
        while child := self.result_buttons.get_first_child():
            self.result_buttons.remove(child)
        for i, (text, callback) in enumerate(buttons):
            button = Gtk.Button(label=text, css_classes=["pill", "suggested-action"] if i == 0 else ["pill"])
            button.connect("clicked", lambda _b, cb=callback: cb())
            self.result_buttons.append(button)
        self.stack.set_visible_child_name("result")

    def show_error(self, title, description):
        self.show_result("dialog-warning-symbolic", title, description, [("Back", self.win.nav.pop)])


class EnrollPage(SessionPage):
    stop_method = "EnrollStop"

    def __init__(self, win, finger, replace):
        super().__init__(win, finger, f"Enroll {label(finger)}")
        self.replace = replace
        self.good = self.rejected = 0
        self.total = max(self.device.stages, 1)
        self.update_progress()

    def begin(self):
        self.hint.set_label("You may be asked for your password.")
        if not (yield from self.claim()):
            return
        try:
            if self.replace:
                yield self.device.call("DeleteEnrolledFinger", self.finger)
                self.win.results.drop(self.finger)
            if self.state == "starting":
                self.device.listener = self.on_event
                yield self.device.call("EnrollStart", self.finger)
        except FprintError as e:
            yield from self.shutdown()
            self.show_error("Couldn't start enrolling", e.text)
            return
        if self.state == "cancelled":
            yield from self.shutdown()
            return
        self.state = "running"
        self.total = max(self.device.stages, 1)
        self.update_progress()
        self.instruction.set_label("Touch the sensor")
        self.hint.set_label(self.tip())

    def tip(self):
        progress = self.good / self.total
        if progress < 1 / 3:
            return f"Rest the pad of your {label(self.finger)} flat on the centre of the sensor."
        if progress < 2 / 3:
            return "Now shift a little with each touch: slightly left, right, up and down."
        return "Finish with the tip and the edges, rolling your finger slightly."

    def update_progress(self):
        good = min(self.good, self.total)
        self.ring.set(["good"] * good + ["pending"] * (self.total - good), str(good), f"of {self.total}")
        self.counts.set_label(self.rejected_text())

    def rejected_text(self):
        if not self.rejected:
            return ""
        return f"{self.rejected} touch{'es' if self.rejected > 1 else ''} rejected"

    def on_event(self, kind, *args):
        if self.state != "running":
            return
        if kind == "finger-present":
            if args[0]:
                self.instruction.set_label("Hold still…")
            return
        if kind != "EnrollStatus":
            return
        status, done = args
        if status == "enroll-stage-passed":
            self.good += 1
            self.instruction.set_label("Good. Lift and touch again")
            self.hint.set_label(self.tip())
        elif status == "enroll-completed":
            self.good = self.total
            self.instruction.set_label("All touches recorded")
            self.hint.set_label("")
        elif status in ENROLL_RETRY:
            self.rejected += 1
            self.ring.flash()
            self.instruction.set_label("Not recorded. Try again")
            self.hint.set_label(ENROLL_RETRY[status])
        self.update_progress()
        if done:
            run(self.finish(status))

    def finish(self, status):
        yield from self.shutdown()
        finger = self.finger
        if status == "enroll-completed":
            rejected = f" ({self.rejected_text()} along the way.)" if self.rejected else ""
            self.show_result(
                "object-select-symbolic", "Fingerprint recorded",
                f"Your {label(finger)} is enrolled.{rejected} "
                "Test it now to check that it is recognised reliably.",
                [("Test It Now", lambda: self.win.start_test(finger)), ("Done", self.win.nav.pop)])
        else:
            removed = " The previous fingerprint for this finger was removed." if self.replace else ""
            self.show_result(
                "dialog-warning-symbolic", "Enrollment didn't finish",
                ENROLL_FAIL.get(status, status) + removed,
                [("Try Again", lambda: self.win.start_enroll(finger)), ("Back", self.win.nav.pop)])


class TestPage(SessionPage):
    stop_method = "VerifyStop"

    def __init__(self, win, finger):
        super().__init__(win, finger, f"Test {label(finger)}")
        self.outcomes = []
        self.waiting = False  # a verify is running and a touch is expected
        self.update_progress()

    def begin(self):
        if not (yield from self.claim()):
            return
        if self.state == "cancelled":
            yield from self.shutdown()
            return
        self.state = "running"
        self.device.listener = self.on_event
        yield from self.next_round()

    def next_round(self):
        try:
            yield self.device.call("VerifyStart", self.finger)
        except FprintError as e:
            if self.state == "running":
                yield from self.shutdown()
                self.show_error("Couldn't start the test", e.text)
            return
        self.waiting = True
        self.instruction.set_label("Touch the sensor")
        self.hint.set_label(f"Touch {len(self.outcomes) + 1} of {TEST_ROUNDS}. "
                            "Touch it the way you normally would when unlocking.")

    def update_progress(self):
        done = ["good" if matched else "poor" for matched in self.outcomes]
        self.ring.set(done + ["pending"] * (TEST_ROUNDS - len(done)), str(len(done)), f"of {TEST_ROUNDS}")
        if self.outcomes:
            self.counts.set_label(f"{sum(self.outcomes)} recognised, {self.outcomes.count(False)} missed")

    def on_event(self, kind, *args):
        if self.state != "running" or not self.waiting:
            return
        if kind == "finger-present":
            if args[0]:
                self.instruction.set_label("Hold still…")
            return
        if kind != "VerifyStatus":
            return
        status, done = args
        if status in VERIFY_RETRY:
            # An unreadable touch says nothing about the stored print, so it isn't scored.
            self.ring.flash()
            self.instruction.set_label("Not read. Try again")
            self.hint.set_label(VERIFY_RETRY[status])
            if not done:
                return
        elif status == "verify-match":
            self.outcomes.append(True)
            self.instruction.set_label("Recognised")
        elif status == "verify-no-match":
            self.outcomes.append(False)
            self.instruction.set_label("Not recognised")
        else:
            run(self.finish(status))
            return
        self.waiting = False
        self.update_progress()
        run(self.after_round())

    def after_round(self):
        yield from quiet(self.device, "VerifyStop")
        if self.state != "running":
            return
        if len(self.outcomes) >= TEST_ROUNDS:
            yield from self.finish(None)
            return
        self.hint.set_label("Lift your finger.")
        yield sleep(1000)
        if self.state == "running":
            yield from self.next_round()

    def finish(self, error_status):
        yield from self.shutdown()
        if error_status:
            self.show_error("The test didn't finish", VERIFY_FAIL.get(error_status, error_status))
            return
        finger, matched, total = self.finger, sum(self.outcomes), len(self.outcomes)
        self.win.results.set(finger, matched, total)
        self.win.update_home()
        summary = f"{matched} of {total} touches were recognised."
        done = ("Done", self.win.nav.pop)
        again = ("Test Again", lambda: self.win.start_test(finger))
        reenroll = ("Re-enroll", lambda: self.win.start_enroll(finger))
        state = self.win.state_of(finger)
        if state == "good":
            note = "" if matched == total else " An occasional miss is normal."
            self.show_result("object-select-symbolic", "Recorded well", summary + note, [done, again])
        elif state == "fair":
            self.show_result(
                "dialog-warning-symbolic", "Borderline",
                summary + " Re-enrolling with more varied touches should make it more reliable.",
                [reenroll, again, done])
        else:
            self.show_result(
                "dialog-warning-symbolic", "Poorly recorded",
                summary + " Re-enroll this finger, covering the centre, tip and edges.",
                [reenroll, again, done])


def card(child):
    for side in ("top", "bottom", "start", "end"):
        child.set_property(f"margin-{side}", 14)
    box = Gtk.Box(css_classes=["card"])
    box.append(child)
    return box


class Window(Adw.ApplicationWindow):
    def __init__(self, app, make_device, results):
        super().__init__(application=app, title="Fingerprints", default_width=660, default_height=720)
        self.make_device = make_device
        self.results = results
        self.device = None
        self.enrolled = []
        self.selected = RECOMMENDED[0]
        self.next_action = None
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.connect_device()

    def connect_device(self):
        try:
            self.device = self.make_device()
        except FprintError as e:
            page = Adw.StatusPage(icon_name="auth-fingerprint-symbolic",
                                  title="No fingerprint sensor available", description=e.text)
            retry = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
            retry.connect("clicked", lambda *_: self.connect_device())
            page.set_child(retry)
            view = Adw.ToolbarView(content=page)
            view.add_top_bar(Adw.HeaderBar())
            self.toasts.set_child(view)
            return
        self.nav = Adw.NavigationView()
        self.home = self.build_home()
        self.nav.add(self.home)
        self.toasts.set_child(self.nav)
        run(self.reload())

    def build_home(self):
        self.next_text = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.next_button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        self.next_button.connect("clicked", lambda *_: self.next_action())
        next_labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        next_labels.append(Gtk.Label(label="Next step", xalign=0, css_classes=["caption-heading", "dim-label"]))
        next_labels.append(self.next_text)
        next_row = Gtk.Box(spacing=12, hexpand=True)
        next_row.append(next_labels)
        next_row.append(self.next_button)

        self.hands = HandsView(self)
        dots = [f'<span foreground="#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}">●</span> {text}'
                for (r, g, b), text in zip(STATE_COLOR.values(),
                                           ("Recognised well", "Not tested yet", "Borderline", "Poor"))]
        legend = Gtk.Label(label="    ".join(dots + ["○ Not enrolled"]), use_markup=True, wrap=True,
                           justify=Gtk.Justification.CENTER, css_classes=["caption"])

        self.finger_title = Gtk.Label(xalign=0, css_classes=["title-3"])
        self.finger_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self.enroll_button = Gtk.Button()
        self.enroll_button.connect("clicked", lambda *_: self.start_enroll(self.selected))
        self.test_button = Gtk.Button(label="Test")
        self.test_button.connect("clicked", lambda *_: self.start_test(self.selected))
        self.delete_button = Gtk.Button(label="Delete", css_classes=["destructive-action"])
        self.delete_button.connect("clicked", lambda *_: self.confirm_delete(self.selected))
        self.finger_buttons = Gtk.Box(spacing=8, margin_top=8)
        for button in (self.test_button, self.enroll_button, self.delete_button):
            self.finger_buttons.append(button)
        finger_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        for child in (self.finger_title, self.finger_status, self.finger_buttons):
            finger_box.append(child)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                          margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        for child in (card(next_row), self.hands, legend, card(finger_box)):
            content.append(child)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=Adw.Clamp(maximum_size=640, child=content))
        view = Adw.ToolbarView(content=scroller)
        view.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Fingerprints", subtitle=self.device.name)))
        return Adw.NavigationPage(title="Fingerprints", child=view)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))

    def reload(self):
        try:
            (fingers,) = yield self.device.call("ListEnrolledFingers", "")
        except FprintError as e:
            fingers = []
            if e.name != "NoEnrolledPrints":
                self.toast(e.text)
        self.enrolled = fingers
        self.results.keep_only(fingers)
        self.update_home()

    def state_of(self, finger):
        if finger not in self.enrolled:
            return "none"
        result = self.results.get(finger)
        if not result:
            return "untested"
        ratio = result["matched"] / result["total"]
        return "good" if ratio >= 0.8 else "fair" if ratio >= 0.6 else "poor"

    def status_text(self, finger):
        state = self.state_of(finger)
        if state == "none":
            return "Not enrolled"
        if state == "untested":
            return "Enrolled, not tested yet"
        result = self.results.get(finger)
        date = datetime.date.fromisoformat(result["date"]).strftime("%-d %b %Y")
        return f"Enrolled. {result['matched']} of {result['total']} test touches recognised on {date}"

    def next_step(self):
        """The most useful thing to do now, as (text, button label, action)."""
        def score(finger):
            result = self.results.get(finger)
            return f"{result['matched']} of {result['total']}"

        by_state = {state: [f for f in FINGERS if self.state_of(f) == state]
                    for state in ("poor", "untested", "fair")}
        if not self.enrolled:
            finger = RECOMMENDED[0]
            return f"Enroll your {label(finger)} to get started.", "Enroll", lambda: self.start_enroll(finger)
        if by_state["poor"]:
            finger = by_state["poor"][0]
            return (f"Your {label(finger)} was recognised only {score(finger)} times. Re-enroll it.",
                    "Re-enroll", lambda: self.start_enroll(finger))
        if by_state["untested"]:
            finger = by_state["untested"][0]
            return (f"Test your {label(finger)} to check that it was recorded well.",
                    "Test", lambda: self.start_test(finger))
        if by_state["fair"]:
            finger = by_state["fair"][0]
            return (f"Your {label(finger)} is borderline ({score(finger)} recognised). "
                    "Re-enrolling should make it more reliable.", "Re-enroll", lambda: self.start_enroll(finger))
        for finger in RECOMMENDED:
            if finger not in self.enrolled:
                return (f"Enroll your {label(finger)} too, as a spare for when another finger doesn't read.",
                        "Enroll", lambda: self.start_enroll(finger))
        return f"All set. {len(self.enrolled)} fingers are enrolled and recognised well.", None, None

    def update_home(self):
        text, button, self.next_action = self.next_step()
        self.next_text.set_label(text)
        self.next_button.set_visible(button is not None)
        self.next_button.set_label(button or "")
        enrolled = self.selected in self.enrolled
        self.finger_title.set_label(label(self.selected).capitalize())
        self.finger_status.set_label(self.status_text(self.selected))
        self.enroll_button.set_label("Re-enroll" if enrolled else "Enroll")
        self.test_button.set_visible(enrolled)
        self.delete_button.set_visible(enrolled)
        self.hands.queue_draw()

    def select(self, finger):
        self.selected = finger
        self.update_home()

    def confirm(self, heading, body, action, callback):
        dialog = Adw.AlertDialog(heading=heading, body=body, close_response="cancel", default_response="cancel")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", action)
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, response: callback() if response == "go" else None)
        dialog.present(self)

    def open_session(self, page):
        if self.nav.get_visible_page() is self.home:
            self.nav.push(page)
        else:
            self.nav.replace([self.home, page])
        page.start()

    def start_test(self, finger):
        self.select(finger)
        self.open_session(TestPage(self, finger))

    def start_enroll(self, finger):
        self.select(finger)
        if finger not in self.enrolled:
            self.open_session(EnrollPage(self, finger, replace=False))
            return
        self.confirm(
            f"Re-enroll {label(finger)}?",
            "The current fingerprint for this finger is deleted first. If you cancel partway, "
            "the finger stays unenrolled until you enroll it again.",
            "Re-enroll", lambda: self.open_session(EnrollPage(self, finger, replace=True)))

    def confirm_delete(self, finger):
        body = "This finger will no longer unlock the computer."
        if self.enrolled == [finger]:
            body += " It is the only enrolled finger, so you will need your password until you enroll another."
        self.confirm(f"Delete {label(finger)}?", body, "Delete", lambda: run(self.delete(finger)))

    def delete(self, finger):
        self.finger_buttons.set_sensitive(False)
        error = None
        try:
            yield self.device.call("Claim", "")
            try:
                yield self.device.call("DeleteEnrolledFinger", finger)
            except FprintError as e:
                error = e
            yield from quiet(self.device, "Release")
        except FprintError as e:
            error = e
        self.finger_buttons.set_sensitive(True)
        if error:
            self.toast(error.text)
        else:
            self.results.drop(finger)
            self.toast(f"{label(finger).capitalize()} deleted")
        yield from self.reload()


class App(Adw.Application):
    def __init__(self, demo):
        flags = Gio.ApplicationFlags.NON_UNIQUE if demo else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        self.demo = demo

    def do_activate(self):
        win = self.get_active_window()
        if not win and self.demo:
            from demo_device import DemoDevice
            win = Window(self, DemoDevice, Results(None))
        elif not win:
            path = os.path.join(GLib.get_user_data_dir(), "fingerprint-manager", "results.json")
            win = Window(self, Device, Results(path))
        win.present()


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    sys.exit(App(demo).run([arg for arg in sys.argv if arg != "--demo"]))
