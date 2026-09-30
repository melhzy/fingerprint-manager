# Fingerprint Manager

A small GTK app for Linux that guides you through enrolling fingerprints and then
checks how well each one was recorded. It talks to [fprintd](https://fprint.freedesktop.org/),
so it works with any reader that fprintd supports.

| Home | Enrolling |
| --- | --- |
| ![Home screen with both hands and a status dot per finger](screenshots/home.png) | ![Enrollment progress ring](screenshots/enroll.png) |

## What it does

- **Shows every finger at a glance.** Both hands are drawn with a status dot on each
  fingertip: recognised well, not tested yet, borderline, poor, or not enrolled.
- **Tells you what to do next.** A card at the top names the most useful next step,
  such as testing a finger that was never tested or re-enrolling one that scored badly.
- **Explains each touch while enrolling.** A progress ring fills as touches are accepted.
  Rejected touches say why (off-centre, not read clearly, finger not lifted), and the
  tips move from the centre of the finger to its tip and edges.
- **Tests the result.** A five-touch test gives a verdict: 4–5 recognised is "Recorded
  well", 3 is "Borderline", fewer is "Poorly recorded" with a re-enroll button.

Match-on-chip readers never expose the fingerprint image, so quality is judged from the
reader's verdict on each touch and from the test score. The app stores only the last test
score and its date per finger, in `~/.local/share/fingerprint-manager/results.json`.

## Requirements

- fprintd with a supported fingerprint reader
- Python 3 with PyGObject
- GTK 4.14 or newer and libadwaita 1.6 or newer

On Ubuntu or Debian:

```sh
sudo apt install fprintd python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

On Fedora:

```sh
sudo dnf install fprintd python3-gobject gtk4 libadwaita
```

## Running

```sh
git clone https://github.com/melhzy/fingerprint-manager.git
cd fingerprint-manager
./fingerprint_manager.py
```

To add it to your app grid, run `./install.sh`. It writes a launcher to
`~/.local/share/applications` that points at this folder.

To try the interface without a reader, run `./fingerprint_manager.py --demo`. This uses a
simulated sensor and saves nothing.

## Good to know

- Enrolling or deleting a fingerprint asks for your password (fprintd's polkit rule).
  Testing does not.
- Re-enrolling a finger deletes its old print first, because readers that check for
  duplicates would otherwise reject it. If you cancel partway, that finger stays
  unenrolled until you enroll it again. The app warns before doing this.
- Developed and tested on Ubuntu 26.04 with a Goodix match-on-chip reader.

## License

MIT. See [LICENSE](LICENSE).
