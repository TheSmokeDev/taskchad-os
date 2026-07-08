# Ghost Phone — the Device Takeover (P4.1)

Status: shipped (Phase B + C, 2026-07-07), default-ON for the ghost, structurally
ghost-only, live-proven end to end
Owner:
`.claude/chat/ghost_capabilities.py` (the capability seam) +
`.claude/chat/ghost_device.py` (device ops) +
`.claude/chat/ghost_control.py` (lifecycle) +
`.claude/chat/adb_control.py` (binary-safe adb) +
`.claude/chat/browser_control.py` (serial confinement) +
`.claude/scripts/dashboard_api.py` (`/api/ghost-viewer/*`) +
`.claude/chat/cli.py` (`thehomie ghost`) +
`dashboard/web/src/pages/GhostViewer.tsx` +
`dashboard/server/src/routes/ghost-viewer.ts`
Last updated: 2026-07-07

## What It Does

The "ghost" is the Homie's OWN background Android — a device it fully owns and
can drive without ever touching the operator's personal phone. It has two
possible backends (see *Lifecycle & backends*): a headless emulator (AVD) the
framework boots itself, or a dedicated spare physical Android it only connects
to.

Two chapters of capability stack on top of that device:

- **P4.0 (the browser target)** made the ghost a *third browser target* —
  `desktop | phone | ghost`. It can drive its own Chrome over CDP, exactly like
  the desktop and phone targets, behind its own `HOMIE_GHOST_ENABLED` switch.
  That layer is documented in `docs/homie-mobile-manual.md` §7a and is unchanged.
- **P4.1 Phase B — the takeover** is this chapter. The Homie now operates the
  *whole device* RDP-style: see its live screen, tap / type / swipe anywhere,
  launch apps, and install APKs — from the dashboard `/ghost` page, the
  `/api/ghost-viewer/*` API, or a script. This is not Chrome-scoped; it is the
  Android device itself.

The operator's personal phone is **structurally unreachable** by any of these
powers. That is the one invariant that never bends — enforced in code before any
gate, not by convention (see *The safety model*).

### What shipped in P4.1

| Slice | What it delivered |
|---|---|
| **B1 — capability seam** | Defaults flipped **ON** for the ghost; 3 new capabilities registered (`ghost.screen.view`, `ghost.input.tap`, `ghost.app.install`); `ghost.app.launch` already existed from P4.0. |
| **B2 — live screen** | Full-device screen via raw `adb exec-out screencap -p` (binary-safe, never agent-browser). |
| **B3 — input** | Tap / type / swipe / keyevent via `adb shell input`, with **server-side** coordinate scaling — the client sends normalized `0..1` floats, the server reads the live device dimensions and scales. Client pixels are never trusted. |
| **B4 — apps** | App launch (`adb ... monkey`) + install (`adb install` of a local APK), with package-name and APK-path validation. |
| **B5 — dashboard** | The `/ghost` device page (poll-based live screen, tap-the-image, nav bar, type box, app bar) plus a thin Hono proxy. Python owns all policy, scaling, and audit. |
| **C1 — self-test rig** | `thehomie ghost test-app` (launches Expo Go against a local Metro dev server — the framework's own mobile self-test) plus this manual. |

## The takeover at a glance

| Surface | What it does |
|---|---|
| Dashboard `/ghost` page | Live screen (poll ~2.5 fps), tap the image, Back / Home / Recents, type text, launch an app by package, install an APK by host path. |
| `GET /api/ghost-viewer/screen` | PNG of the live screen + `X-Ghost-Screen-Width` / `X-Ghost-Screen-Height` headers. |
| `POST /api/ghost-viewer/tap` \| `/text` \| `/swipe` \| `/key` | Normalized-coordinate input (see *The coordinate contract*). |
| `POST /api/ghost-viewer/app/launch` \| `/app/install` | Launch by package / install a local APK. |
| `thehomie ghost status` | Physical ghost state (never boots). |
| `thehomie ghost up` \| `down` | Boot / shut down the ghost. |
| `thehomie ghost test-app [--package PKG] [--json]` | Launch the Homie's own app (Expo Go by default) on the ghost for self-testing. |

## The safety model

This is the heart of the feature. Four **independent** gates stack, in the exact
order a request hits them. The takeover ships default-ON *because* one of these
gates is structural, not a default.

### Gate 1 — master switch (`HOMIE_GHOST_ENABLED`)

Resolved call-time by `config.get_ghost_settings()`. **Off by default.** Until
it's set, the entire ghost surface 403s: every `/api/ghost-viewer/*` route calls
`_resolve_browser_target("ghost", …)`, which returns
*"Ghost is disabled — set HOMIE_GHOST_ENABLED=true"* and writes a blocked audit
row. The CLI `ghost up` / `test-app` refuse the same way.

### Gate 2 — kill-switch (`HOMIE_KILLSWITCH_GHOST`)

`HOMIE_KILLSWITCH_GHOST=disabled` refuses ghost boot and `test-app` through
`security.kill_switches.requireEnabled("ghost")` — the shared operator
kill-switch primitive with an audited refusal counter. Turning it off does not
require touching any code path; it is a live operator brake.

### Gate 3 — the structural ghost-only invariant (the real safety line)

`require_ghost_capability()` in `ghost_capabilities.py` checks `target != "ghost"`
**FIRST** — before the per-power gate, and before it even looks the capability up
in the registry:

```python
# 1. HARD INVARIANT — structurally ghost-only, checked before the registry
#    lookup AND before the gate, so it beats an enabled gate.
if target != GHOST_TARGET:            # GHOST_TARGET == "ghost"
    emit(... outcome="blocked",
         reason=f"...only available for target='ghost', not {target!r}")
    raise GhostCapabilityDenied(reason)
```

Because it is checked first, it **beats an enabled gate**: a `phone` or `desktop`
request can never reach a screen / tap / app / SMS / storage / notif call, *even
if that capability's gate is explicitly ON*. The operator's personal phone stays
Chrome-only forever, by construction.

**This is why default-ON is safe.** The structural invariant — not the
per-capability default — is what protects the outside world and the personal
device. A dedicated device the operator owns, that cannot reach the real phone,
does not need the default-deny posture that guards *mutating external* surfaces.
So the ghost's own powers ship default-ON (operator decision, 2026-07-07). Each
still has its own kill-switch (Gate 4's siblings; see *Capabilities*), and every
attempt is audited.

### Gate 4 — transport confinement (serial resolution refuses fallback)

Every adb call resolves the ghost's OWN serial via
`_resolve_adb_serial_or_raise("ghost")` in `browser_control.py`. If
`HOMIE_GHOST_ADB_SERIAL` is unset it **raises** rather than fall back to
single-device autodetect — autodetect could silently land on the operator's
attached phone. The confinement is symmetric (PhoneOps review F1, issue #89):
once a ghost serial is configured, a *phone*-target action that tries to
autodetect is also refused, so a paired ghost can never be driven under the
`phone` label either.

### The audit trail

Every attempt — allowed or refused — writes an audit row. The capability seam
writes a `surface="ghost"` row (with the resolved-or-rejected `target`), and the
dashboard endpoints add a `surface="dashboard"`, `target="ghost"` row on top. A
refusal is as auditable as a success.

## Capabilities

All seven capabilities live in the `GHOST_CAPABILITIES` registry
(`ghost_capabilities.py`), all ship **default-ON** for the ghost, and each has a
UNIQUE per-power env kill-switch. Only the literal `true` (case-insensitive,
trimmed) enables; any other value — including `false` — disables. Set any one
`HOMIE_GHOST_CAP_*=false` to kill exactly that power while every other power
stays on.

| Capability | Env kill-switch | Effect | adb it drives |
|---|---|---|---|
| `ghost.screen.view` | `HOMIE_GHOST_CAP_SCREEN_VIEW` | read | `exec-out screencap -p` |
| `ghost.input.tap` | `HOMIE_GHOST_CAP_INPUT_TAP` | write | `shell input tap` / `text` / `swipe` / `keyevent` |
| `ghost.app.launch` | `HOMIE_GHOST_CAP_APP_LAUNCH` | write | `shell monkey … LAUNCHER 1` |
| `ghost.app.install` | `HOMIE_GHOST_CAP_APP_INSTALL` | write | `install <local.apk>` |
| `ghost.sms.read` | `HOMIE_GHOST_CAP_SMS_READ` | read | seam declared; no device op wired yet |
| `ghost.storage.read` | `HOMIE_GHOST_CAP_STORAGE_READ` | read | seam declared; no device op wired yet |
| `ghost.notif.read` | `HOMIE_GHOST_CAP_NOTIF_READ` | read | seam declared; no device op wired yet |

**One capability, four input verbs.** Tap, type, swipe, and keyevent all gate on
`ghost.input.tap`. Killing `HOMIE_GHOST_CAP_INPUT_TAP=false` disables *all four*
input verbs at once while screen-view and the app powers stay live — the intended
"look but don't touch" mode. The three `*.read` capabilities are declared in the
seam (so the structural invariant and audit already cover them) but do not yet
have a wired device operation; they are the forward hooks for SMS / storage /
notification reads.

## Device operations

All device ops live in `ghost_device.py`. Every one follows the same three-step
shape: **(1)** route through `require_ghost_capability(<cap>, target="ghost")`
(Gate 3), **(2)** resolve the ghost's OWN serial via
`_resolve_adb_serial_or_raise("ghost")` (Gate 4), **(3)** drive raw adb — the
screen through the binary-safe `adb_exec_out`, input/app through text-mode
`run_adb` — **never** agent-browser.

### Screen — `ghost_screencap()`

Gated by `ghost.screen.view`. Runs `adb exec-out screencap -p`: PNG bytes go
straight to stdout — no on-device temp file, no `pull`, no Git-Bash path
mangling. The bytes come back through `adb_control.adb_exec_out`, the
**binary-safe** variant (`text=False`, raw stdout bytes). The width and height
are parsed straight from the PNG's IHDR header (`_png_dimensions`, no image
library) and returned alongside the bytes as `(png, width, height)`. Timeout: 20s.

> Why binary-safe matters: the ordinary `run_adb` decodes stdout as UTF-8, which
> silently corrupts binary payloads like a PNG. Screencap must go through
> `adb_exec_out`, or the image comes back mangled with no error.

### Input — tap / type / swipe / keyevent

All gated by `ghost.input.tap`. Coordinates arrive as normalized floats and are
scaled server-side (see *The coordinate contract*):

- **`ghost_tap(x_norm, y_norm)`** — reads the live device size, scales, runs
  `adb shell input tap <x> <y>`. Returns `{x, y, width, height}` (the resolved
  device pixels and the dimensions used).
- **`ghost_text(text)`** — caps to `MAX_TEXT_LEN` (500), escapes spaces to `%s`
  (adb `input text`'s space escape), runs one `adb shell input text` call — a
  single call, not a paste buffer.
- **`ghost_swipe(x1, y1, x2, y2, duration_ms=300)`** — scales both endpoints,
  clamps duration to `1..10000` ms, runs `adb shell input swipe`.
- **`ghost_keyevent(code)`** — validates `code` is an integer in `0..320`
  (never a free-form string), runs `adb shell input keyevent`. Common codes:
  `4` = BACK, `3` = HOME, `66` = ENTER, `187` = RECENTS. Input timeout: 10s.

### Apps — launch / install

- **`ghost_app_launch(package)`** — gated by `ghost.app.launch`. Validates the
  package against the Android package grammar
  (`^[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)+$`) so a free-form string
  can't smuggle extra `monkey` arguments, then runs
  `adb shell monkey -p <pkg> -c android.intent.category.LAUNCHER 1`. `monkey`
  prints *"No activities found … aborting"* while still exiting 0 when the
  package is absent, so the op surfaces that as a real failure instead of a false
  success.
- **`ghost_app_install(apk_path)`** — gated by `ghost.app.install`. Validates
  that the path is an existing `.apk` file on the host (suffix + `is_file()`),
  then runs `adb install <path>`. `adb install` can exit 0 while printing
  *"Failure [INSTALL_FAILED_*]"*, so the op treats any `Failure` in the output
  (or a non-zero exit) as an error. Install timeout: 120s.

## The coordinate contract

The server scales — clients never send device pixels. This is enforced at three
layers so it cannot be bypassed:

1. **Wire** — the client sends normalized floats `0.0..1.0` relative to the image
   it was shown. The Pydantic bodies bound them (`GhostTapBody.x/y` are
   `Field(ge=0.0, le=1.0)`; swipe endpoints likewise; a raw pixel would fail
   validation).
2. **Live dimensions** — the server resolves the device size FRESH per request
   via `ghost_device_size()` (`adb shell wm size`, preferring the *Override* size
   over the panel's *Physical* size). It is read, never a cached or assumed
   `1080x2400` (Rule 2 — physical state is the source of truth).
3. **Scale + clamp** — `_to_device_pixel()` clamps the normalized value to
   `[0,1]`, multiplies by the live dimension, and clamps the result to
   `[0, size-1]`. Only then does a real device pixel exist, and it exists only on
   the server.

The dashboard follows the same rule: `GhostViewer.tsx` computes
`(clientX - rect.left) / rect.width` against the *rendered* image and posts that
float — it never knows or sends a device pixel.

## The dashboard `/ghost` page

`dashboard/web/src/pages/GhostViewer.tsx` is the RDP-style room. It is
**ghost-only by construction** — there is no `target` param anywhere on the page.

- **Live screen** — polls `GET /api/ghost-viewer/screen` every 400 ms (~2.5 fps)
  when "Go live" is on, or a single "Capture" otherwise. It never overlaps two
  screencaps (a `capturing` ref guards re-entry) and revokes each prior blob URL.
- **Tap the image** — a click on the screenshot is converted to normalized coords
  and POSTed to `/tap`.
- **Nav bar** — Back (`keycode 4`), Home (`3`), Recents (`187`) via `/key`.
- **Type box** — POSTs text to `/text`.
- **App bar** — a package field → `/app/launch`, and a host APK-path field →
  `/app/install`.
- **Honest error surfacing** — a `403` renders *"Ghost is off or the screen
  capability is disabled (HOMIE_GHOST_ENABLED / HOMIE_GHOST_CAP_SCREEN_VIEW)"*; a
  `503` renders *"Ghost is enabled but the device is not reachable — boot it with
  /ghost up."*

### The Hono proxy is thin on purpose

`dashboard/server/src/routes/ghost-viewer.ts` only forwards the PNG bytes and the
JSON action bodies through `authedFetchBinary` / `authedFetchJson`. **Python owns
ALL policy** — the `HOMIE_GHOST_ENABLED` gate, the structural capability seam,
coordinate scaling, and every audit row. The proxy re-emits the
`X-Ghost-Screen-Width/Height` headers and otherwise carries nothing of its own.
This mirrors the framework's dashboard boundary: the Hono layer never opens a
database, never scales a coordinate, never decides a capability.

## The API surface

Every route resolves the target to `ghost` (enforcing `HOMIE_GHOST_ENABLED`),
runs the browser-viewer workflow gate, then calls into `ghost_device` (which
re-checks the structural invariant + the ghost's own serial). None of these
routes accept a `target` param — they are ghost-only by construction.

| Route | Body | Success |
|---|---|---|
| `GET /api/ghost-viewer/screen` | — | `200` PNG + `X-Ghost-Screen-Width/Height` |
| `POST /api/ghost-viewer/tap` | `{x, y}` (0..1) | `{ok, x, y, width, height}` |
| `POST /api/ghost-viewer/text` | `{text}` | `{ok, length}` |
| `POST /api/ghost-viewer/swipe` | `{x1, y1, x2, y2, duration_ms?}` | `{ok, …}` |
| `POST /api/ghost-viewer/key` | `{keycode}` (0..320) | `{ok, keycode}` |
| `POST /api/ghost-viewer/app/launch` | `{package}` | `{ok, package}` |
| `POST /api/ghost-viewer/app/install` | `{apk_path}` | `{ok, apk}` |

**Status-code contract** (shared by `_run_ghost_viewer`, and mirrored on the
screen route):

| Condition | Code | Meaning |
|---|---|---|
| `HOMIE_GHOST_ENABLED` off / workflow gate closed | `403` | ghost surface refused upstream |
| `GhostCapabilityDenied` (a killed `HOMIE_GHOST_CAP_*`) | `403` | that specific power is off — refused, not a device failure |
| `ValueError` (bad package, non-`.apk`, missing file, out-of-range key) | `400` | invalid input |
| any other exception (adb/device failure) | `503` | ghost enabled but not reachable / adb failed |

Each branch — success and every refusal — writes an audit row.

## The CLI — `thehomie ghost`

| Command | What it does |
|---|---|
| `thehomie ghost status [--json]` | Physical ghost state (`adb devices` + `getprop sys.boot_completed`). **Never boots.** |
| `thehomie ghost up` | Checks `HOMIE_GHOST_ENABLED` + the kill-switch, then boots the AVD (or connects a spare) and forwards the CDP port. |
| `thehomie ghost down` | Shuts the ghost down (`adb emu kill` for an AVD) and reclaims RAM. |
| `thehomie ghost test-app [--package PKG] [--json]` | The self-test rig — checks enabled + kill-switch + booted, then launches the Homie's own app (Expo Go by default, `host.exp.exponent`) on the ghost. |

## Lifecycle & backends

`ghost_control.py` owns the ghost's lifecycle: lazy, self-healing, fail-open.
"Is the ghost up?" is answered by physical state (`adb devices` / `getprop`),
never a cached "I started it" claim (Rule 2). It resolves one of two backends
from env:

- **AVD backend** (`HOMIE_GHOST_AVD` set) — a headless Android emulator this
  module boots itself. Boot flags are the proven headless set
  (`-no-window -no-audio -no-boot-anim -gpu swiftshader_indirect`), spawned
  detached so the emulator outlives the caller. `ensure_ghost_running` polls
  physical boot state (up to a 180s timeout) then forwards the CDP port.
- **Spare-device backend** (no `HOMIE_GHOST_AVD`) — a dedicated physical Android
  this module only **connects + forwards**. There is nothing to boot, so it
  never tries; `ghost_shutdown` leaves a spare running (not this module's to
  power off).

`ghost_status` **never boots** — auto-booting on a status poll would pin several
GB of RAM. `ensure_ghost_running` (i.e. `thehomie ghost up`) is the only boot
path, and it is operator-driven.

## Real device, real input — and its limits

This is the honest positioning. Read it before you point the ghost at any account
that fights automation.

**Why the ghost is different from browser automation.** The ghost installs and
runs *any real Android app*, and input goes through `adb shell input` — genuine
OS-level touch and key events delivered by Android's input system. That is
fundamentally different from, and stealthier than, browser automation
(Selenium / Playwright / agent-browser), which injects detectable automation
flags and runs an *instrumented* browser. An app on the ghost sees a real OS
running a real app driven by real touch — there is no automation flag on the app
to sniff, no `navigator.webdriver`, no CDP fingerprint on the app process.

**Humanized input (the behavioral layer).** Real OS-level events are necessary
but not sufficient: naive `input tap`/`swipe`/`text` still carry the *kinematic*
tells anti-bot systems sample for — pixel-perfect coordinates, zero press dwell,
dead-straight constant-velocity swipes, and instant whole-string typing (touch
and typing rhythm are documented behavioral biometrics). By default the ghost's
input verbs now shape input like a hand:

- **Tap** — lands within a few pixels of the target (real fingers don't hit the
  same pixel twice) as a short *dwell* touch with micro-drift (an `input swipe`
  DOWN-hold-UP), not `input tap`'s instantaneous zero-dwell touch.
- **Swipe** — a *curved*, *variable-velocity* path: a quadratic bezier with a
  random perpendicular bow, eased point spacing (slow at the ends, fast in the
  middle), and per-point jitter, emitted as an `input motionevent`
  DOWN / MOVE… / UP sequence with a jittered duration. Not a straight line.
- **Typing** — character by character with randomized inter-keystroke pauses, so
  the stream carries a human cadence instead of one instantaneous injection.

All of it is deterministic-testable (injectable rng + sleep) and can be turned
off per call (`humanize=false` on the API body) for a precise/scripted action.
Swipe fails safe: if a device lacks `input motionevent` (pre-Android-11) it
releases and completes via a plain swipe rather than leave a stuck finger.

**The one input tell we can't fix with `adb shell input`.** Injected events
report touch *pressure* = 0 and *tool type* = UNKNOWN. Faking those needs
`sendevent` (raw `/dev/input` writes), which is device-model-specific and
fragile; it is deliberately out of scope. So humanization covers position,
timing, path, and cadence — not pressure/tool-type.

**The honest limit — an emulator is still an emulator.** The default backend is a
headless **emulator** (a `google_apis` image, not Play-certified). Sophisticated
anti-bot / anti-emulator systems — notably TikTok and Instagram — can still
fingerprint an emulator through:

- qemu build properties (`ro.hardware`, `ro.product.model`, `ro.kernel.qemu`),
- generic / obviously-virtual hardware IDs,
- simulated or absent sensors (a real accelerometer / gyroscope / GPS trace vs.
  none), and
- the lack of Play certification / real Play Integrity attestation.

An emulator is **not** a guaranteed bypass of serious anti-bot. Treat it as a
real device for *your own* development, testing, and self-test — not as a cloak
against a platform that actively hunts emulators.

**The stronger path (already architected).** The ghost backend can be a **spare
physical Android device** — the connect-not-boot backend in `ghost_control.py`,
riding the same serial + CDP seam, selected simply by leaving `HOMIE_GHOST_AVD`
unset and pointing `HOMIE_GHOST_ADB_SERIAL` at the spare. A real phone gives a
real hardware fingerprint, real sensors, real Play Services, and real touch —
that is the anti-detection-grade setup. Recommendation:

- **Emulator** — development, testing, and the self-test rig.
- **Spare physical device** — accounts that actively fight automation.

**What this is, and is not.** The ghost is the operator's OWN dedicated device
driving the operator's OWN apps and accounts — a second phone that happens to
live on the PC. Nothing here is for fraud, fake-account farming, or mass abuse;
those depend on scale and impersonation the ghost neither provides nor
encourages. One operator, one dedicated device, their own logins.

## Self-test rig (Phase C)

The ghost is the framework's own mobile test device (`mobile/AGENTS.md` names the
AVD; the mobile app runs in Expo Go for v1). To smoke the Homie's app on its own
phone:

1. `thehomie ghost up` — boots the headless emulator (or connects a spare).
2. In `mobile/`, run `npx expo start` — Metro on `:8081`.
3. `thehomie ghost test-app` — launches Expo Go on the ghost; open the dev server
   from there (the ghost shares the emulator's host network).

If Expo Go isn't installed yet, install it first — an AVD `google_apis` image has
no Play Store: use the `/ghost` viewer's **Install APK** button, or
`adb -s <serial> install expo-go.apk`. `test-app` says exactly this when the
launch fails because the package is absent.

## Proven live

The whole chain was proven against the real emulator on 2026-07-07 (AVD, live
display `1080x2400`):

- **Screen** — `screencap` returned a real **1.37 MB PNG**; the IHDR parse read
  the dimensions straight from the bytes.
- **Device size** — read live as `1080x2400` (never assumed).
- **Tap** — normalized `(0.5, 0.5)` scaled server-side to device pixels
  `(540, 1200)`.
- **Keys** — HOME and BACK keyevents landed.
- **Swipe** — a scaled two-point swipe landed.
- **App launch** — `com.android.settings` launched via `monkey`.
- **App install** — a real **198 MB APK (Expo Go)** was pulled, uninstalled, then
  **reinstalled** through `ghost_app_install`, and launched.
- **The negative proof** — all four device powers (screen / tap / launch /
  install) were **refused** for `target="phone"` *even with their gates ON*, each
  refusal audited with the reason *"only available for target='ghost', not
  'phone'"*. The operator's personal phone was never attached.

That last line is the point of the whole design: the gates being ON changed
nothing for the phone, because Gate 3 is structural.

## Landmines (found live, do not rediscover)

- **Never agent-browser for the device screen.** Its daemon wedges on the
  emulator (confirmed 2026-07-06). `ghost.screen.view` uses raw
  `adb exec-out screencap -p` — bytes straight to stdout.
- **`run_adb` is text-mode.** Screencap must go through the binary-safe
  `adb_exec_out`; decoding PNG bytes as UTF-8 corrupts them silently.
- **`ghost status` never boots.** Booting is always the explicit `ghost up`; a
  multi-GB emulator must never spin up from a status poll.
- **The device screen and the ghost *browser* screenshot are different surfaces.**
  P4.0's ghost-target browser screenshot keeps its agent-browser/CDP path (that's
  the Chrome surface). Only the *device* screen uses `screencap`. Don't conflate
  them.
- **`monkey` and `adb install` lie about success.** Both can exit 0 while
  printing a failure line; the ops parse the output and raise, so a missing
  package or a failed install never returns a false `{ok: true}`.

## Config

| Env var | Purpose | Default |
|---|---|---|
| `HOMIE_GHOST_ENABLED` | Master switch for the whole ghost surface (Gate 1). | `false` |
| `HOMIE_KILLSWITCH_GHOST` | Operator kill-switch for boot + `test-app` (Gate 2). | (unset = enabled) |
| `HOMIE_GHOST_ADB_SERIAL` | The ghost's OWN adb serial (e.g. `emulator-5554`). Required — Gate 4 refuses to autodetect. | (unset) |
| `HOMIE_GHOST_CDP_PORT` | The ghost's CDP forward port (P4.0 browser target). | `18224` |
| `HOMIE_GHOST_AVD` | AVD name → the AVD backend (this module boots it). Unset → the spare-device backend (connect only). | (unset) |
| `HOMIE_GHOST_CAP_SCREEN_VIEW` | Kill-switch for `ghost.screen.view`. | on |
| `HOMIE_GHOST_CAP_INPUT_TAP` | Kill-switch for `ghost.input.tap` (tap / text / swipe / key). | on |
| `HOMIE_GHOST_CAP_APP_LAUNCH` | Kill-switch for `ghost.app.launch`. | on |
| `HOMIE_GHOST_CAP_APP_INSTALL` | Kill-switch for `ghost.app.install`. | on |
| `HOMIE_GHOST_CAP_SMS_READ` / `_STORAGE_READ` / `_NOTIF_READ` | Kill-switches for the declared read seams. | on |
| `HOMIE_EMULATOR_BIN` / `HOMIE_ADB_BIN` | Optional overrides for the emulator / adb binaries (else PATH, else SDK fallback). | (unset) |

## Tests & verification

| Suite | Covers |
|---|---|
| `tests/test_ghost_capabilities.py` | The capability seam — structural invariant (checked first, beats an enabled gate), per-power gate, default-on, audit rows. |
| `tests/test_ghost_device.py` | Every device op — capability + serial gating, coordinate scaling, PNG dimension parsing, package/APK/keycode validation, the `monkey`/`install` false-success guards. |
| `tests/test_ghost_control.py` | Lifecycle — status never boots, AVD boot poll, spare connect, fail-open shutdown. |
| `tests/test_ghost_cli.py` | The `thehomie ghost` group — enabled/kill-switch/booted preconditions on `up` / `test-app`. |
| `tests/test_ghost_command.py` | The P4.0 `/browser … ghost` target path. |
