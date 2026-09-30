"""A simulated sensor, for trying the UI without hardware: fingerprint_manager.py --demo"""

from gi.repository import GLib


class DemoDevice:
    name = "Demo sensor (no hardware)"
    stages = 13
    STEP_MS = 800
    ENROLL_SCRIPT = (["enroll-stage-passed"] * 3 + ["enroll-retry-scan"] + ["enroll-stage-passed"] * 4
                     + ["enroll-finger-not-centered"] + ["enroll-stage-passed"] * 5 + ["enroll-completed"])
    VERIFY_SCRIPT = [True, True, False, True, True]

    def __init__(self):
        self.listener = None
        self.enrolled = ["right-index-finger"]
        self.timer = 0
        self.verifies = 0

    def call(self, method, *args):
        def start(done):
            def reply():
                done(getattr(self, "_" + method)(*args), None)
                return False
            GLib.idle_add(reply)
        return start

    def _emit(self, kind, *args):
        if self.listener:
            self.listener(kind, *args)

    def _stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        return ()

    _EnrollStop = _VerifyStop = _stop

    def _Claim(self, _user):
        return ()

    def _Release(self):
        return self._stop()

    def _ListEnrolledFingers(self, _user):
        return (list(self.enrolled),)

    def _DeleteEnrolledFinger(self, finger):
        self.enrolled.remove(finger)
        return ()

    def _EnrollStart(self, finger):
        script = list(self.ENROLL_SCRIPT)

        def tick():
            status = script.pop(0)
            if not script:
                self.enrolled.append(finger)
                self.timer = 0
            self._emit("EnrollStatus", status, not script)
            return bool(script)
        self.timer = GLib.timeout_add(self.STEP_MS, tick)
        return ()

    def _VerifyStart(self, _finger):
        def tick():
            matched = self.VERIFY_SCRIPT[self.verifies % len(self.VERIFY_SCRIPT)]
            self.verifies += 1
            self.timer = 0
            self._emit("VerifyStatus", "verify-match" if matched else "verify-no-match", True)
            return False
        self.timer = GLib.timeout_add(self.STEP_MS, tick)
        return ()
