// Makes the power button (where many laptops keep the fingerprint sensor) behave
// like a MacBook's: a press while working locks the screen, and on the lock screen
// a finger resting on the sensor unlocks. GNOME Shell only starts fingerprint
// verification once the lock screen shows its unlock prompt, so while locked this
// opens the prompt whenever the lock screen appears or lights up.
//
// Opening the prompt makes the shell look up the fingerprint reader with a
// synchronous D-Bus call (gdm/util.js, 5 s timeout), which freezes the whole
// screen for as long as fprintd takes to answer: a few hundred ms from cold, and
// seconds while the reader is coming back from suspend. So before opening the
// prompt this asks fprintd for the reader asynchronously, and only opens the
// prompt once fprintd has answered.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as LoginManager from 'resource:///org/gnome/shell/misc/loginManager.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// X11-style keycode of the power button (evdev KEY_POWER + 8).
const POWER_KEYCODE = 124;
const LOCK_MODES = Shell.ActionMode.LOCK_SCREEN | Shell.ActionMode.UNLOCK_SCREEN;
// How long to wait for fprintd to find the reader, and how often to ask again
// meanwhile. If it never answers the prompt is left closed (a key press opens
// it, as in stock GNOME) rather than freezing the screen.
const READER_WAIT_MS = 10000;
const READER_RETRY_MS = 500;
const FPRINT_NAME = 'net.reactivated.Fprint';
const FPRINT_MANAGER_PATH = '/net/reactivated/Fprint/Manager';
const FPRINT_MANAGER_IFACE = 'net.reactivated.Fprint.Manager';
const FPRINT_DEVICE_IFACE = 'net.reactivated.Fprint.Device';
const POWER_SCHEMA = 'org.gnome.settings-daemon.plugins.power';
const POWER_KEY = 'power-button-action';

function powerAction() {
    return global.display.get_keybinding_action(POWER_KEYCODE, 0);
}

export default class FingerprintPromptExtension extends Extension {
    // The extension stays enabled across locking and unlocking, so every hook
    // checks for itself whether the screen is locked.
    enable() {
        const shield = Main.screenShield;
        this._powerSettings = new Gio.Settings({schema_id: POWER_SCHEMA});
        this._loginManager = LoginManager.getLoginManager();

        this._acceleratorId = global.display.connect('accelerator-activated',
            (display, activated) => {
                if (activated !== powerAction())
                    return;
                if (shield.locked) {
                    // Only reached if the grab was made after we restricted it.
                    this._showPrompt();
                } else if (this._powerSettings.get_string(POWER_KEY) === 'nothing') {
                    // Desktop: the user chose "Nothing" for the power button, so the
                    // settings daemon leaves the press to us. The finger is still on
                    // the sensor, so the prompt must not open until the next press.
                    this._lockedByButton = true;
                    shield.lock(true);
                }
            });

        // The lock screen has just been drawn (after locking), or has lit up
        // again after blanking (the lightbox is the black cover).
        this._shownId = shield.connect('lock-screen-shown', () => {
            this._restrictPowerButton();
            if (this._lockedByButton)
                this._lockedByButton = false;
            else
                this._showPrompt();
        });
        this._lightboxId = shield._longLightbox.connect('notify::active', lightbox => {
            if (!lightbox.active)
                this._showPrompt();
        });

        // Closing the lid suspends: the shell locks the screen from its own
        // handler of this signal, which runs before ours, so the lock-screen-shown
        // hook above sees the suspend coming through loginManager.preparingForSleep
        // rather than through anything set here.
        this._sleepId = this._loginManager.connect('prepare-for-sleep',
            (_manager, aboutToSuspend) => {
                // The lock screen lights itself up on resume (and that runs
                // before this handler), which opens the prompt through
                // _showPrompt like any other lighting up.
                if (aboutToSuspend)
                    this._cancelReaderWait();
            });

        this._restrictPowerButton();
    }

    // This extension runs on the lock screen (unlock-dialog session mode) because
    // that is where it opens the unlock prompt; on the desktop it only locks.
    disable() {
        const shield = Main.screenShield;
        global.display.disconnect(this._acceleratorId);
        shield.disconnect(this._shownId);
        shield._longLightbox.disconnect(this._lightboxId);
        this._loginManager.disconnect(this._sleepId);
        this._loginManager = null;
        this._powerSettings = null;
        this._cancelReaderWait();
        this._releasePowerButton();
        this._lockedByButton = false;
    }

    // The settings daemon grabs the power button as an accelerator, so a press
    // never reaches the lock screen as a key. Take the grab away in the lock
    // modes: the press then opens the prompt like any other key, and can no
    // longer suspend or shut down a locked computer. Re-checked at each lock,
    // in case the daemon has grabbed the button again since.
    _restrictPowerButton() {
        const action = powerAction();
        const binding = action ? Meta.external_binding_name_for_action(action) : null;
        if (binding === this._powerBinding)
            return;
        this._releasePowerButton();
        if (!binding)
            return;
        this._powerBinding = binding;
        this._powerModes = Main.wm._allowedKeybindings[binding];
        Main.wm.allowKeybinding(binding, this._powerModes & ~LOCK_MODES);
    }

    _releasePowerButton() {
        if (this._powerBinding)
            Main.wm.allowKeybinding(this._powerBinding, this._powerModes);
        this._powerBinding = null;
    }

    _cancelReaderWait() {
        this._readerWait?.cancel();
        this._readerWait = null;
    }

    _showPrompt() {
        // While suspending the reader is not usable: fprintd refuses to scan
        // ("Cannot run while suspended") and the failed attempt keeps the
        // reader claimed after resume. The lock screen lights up again on
        // resume, which comes back here once suspending is over.
        if (this._loginManager.preparingForSleep || this._readerWait)
            return;
        if (!Main.screenShield.locked)
            return;
        // Nothing to do when the prompt is already open. This also covers the
        // unlock fade-out, which switches the lightbox off like a lighting up.
        const dialog = Main.screenShield._dialog;
        if (dialog && dialog._activePage === dialog._promptBox)
            return;

        const cancellable = new Gio.Cancellable();
        const started = GLib.get_monotonic_time();
        this._readerWait = cancellable;
        this._waitForReader(cancellable).then(ready => {
            if (this._readerWait !== cancellable)
                return; // cancelled, or the extension was disabled
            this._readerWait = null;
            const waited = Math.round((GLib.get_monotonic_time() - started) / 1000);
            if (!ready) {
                console.warn(`fingerprint-prompt: fprintd did not report the reader within ${waited} ms; leaving the prompt closed`);
                return;
            }
            if (!this._loginManager.preparingForSleep && Main.screenShield.locked) {
                console.log(`fingerprint-prompt: reader ready after ${waited} ms, opening the prompt`);
                Main.screenShield._dialog?.activate();
            }
        });
    }

    // Resolves to true once fprintd has answered the two calls the shell makes
    // synchronously when the prompt opens (default device, then its
    // properties), or to false when READER_WAIT_MS is up or the wait was
    // cancelled. fprintd starts on the first call if needed, and reports no
    // device while the reader is still coming back from suspend, so ask again
    // until it does. Never rejects.
    async _waitForReader(cancellable) {
        const bus = Gio.DBus.system;
        const deadline = GLib.get_monotonic_time() + READER_WAIT_MS * 1000;
        for (;;) {
            try {
                const reply = await bus.call(FPRINT_NAME, FPRINT_MANAGER_PATH,
                    FPRINT_MANAGER_IFACE, 'GetDefaultDevice', null,
                    new GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE,
                    READER_WAIT_MS, cancellable);
                const [devicePath] = reply.deepUnpack();
                await bus.call(FPRINT_NAME, devicePath,
                    'org.freedesktop.DBus.Properties', 'GetAll',
                    new GLib.Variant('(s)', [FPRINT_DEVICE_IFACE]), null,
                    Gio.DBusCallFlags.NONE, READER_WAIT_MS, cancellable);
                return true;
            } catch (e) {
                if (cancellable.is_cancelled() ||
                    GLib.get_monotonic_time() >= deadline)
                    return false;
                console.log(`fingerprint-prompt: fprintd has no reader yet (${e.message}); asking again`);
            }
            await new Promise(resolve => {
                let id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, READER_RETRY_MS, () => {
                    id = 0;
                    resolve();
                    return GLib.SOURCE_REMOVE;
                });
                cancellable.connect(() => {
                    if (id)
                        GLib.source_remove(id);
                    id = 0;
                    resolve();
                });
            });
            if (cancellable.is_cancelled())
                return false;
        }
    }
}
