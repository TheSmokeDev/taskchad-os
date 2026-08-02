# Buzz Native Collaboration

Status: implemented; v0.5.3 local foundation installed, signed Homie activation pending
Owner: `.claude/chat/adapters/buzz.py`
Last updated: 2026-08-02

## Upstream Resources

- [Buzz repository](https://github.com/block/buzz)
- [Latest Buzz release](https://github.com/block/buzz/releases/latest)
- [Pinned pilot release: Buzz Desktop v0.5.3](https://github.com/block/buzz/releases/tag/desktop-v0.5.3)
- [Buzz changelog](https://github.com/block/buzz/blob/main/CHANGELOG.md)
- [Architecture](https://github.com/block/buzz/blob/main/ARCHITECTURE.md)
- [Contributing and source prerequisites](https://github.com/block/buzz/blob/main/CONTRIBUTING.md)
- [Security policy](https://github.com/block/buzz/security)

Check the repository, release notes, and security policy before every upgrade.
The Homie pins a reviewed release; it does not track upstream `main`.

## What It Does

Buzz is The Homie's signed collaboration transport. It does not replace the
Homie runtime, memory, sessions, skills, lane routing, cron, approval policy,
Dashboard, or Electron app. Stock Buzz Desktop is the companion room client;
the Homie Dashboard remains the operator and control plane.

```text
Buzz Desktop -> local Buzz relay -> native BuzzAdapter -> Homie router/runtime
                                                        |-> final answer
Homie work state -> redacted receipt outbox ------------|-> signal channel
```

The adapter supports rooms, DMs, Nostr reply threads, reactions, NIP-92 media
references/uploads, CLI-backed scheduled delivery, and four ordinary-chat
work receipts. ACP, Buzz workflow approvals, repositories, canvases, huddles,
and Buzz-driven orchestration are outside v1.

## Identity And Authorization

Each Homie profile owns one Nostr private key in that profile's secret `.env`.
The public key is the normalized `User.platform_id`; Buzz channel UUIDs are
`Channel.platform_id`; signed event ids are platform message ids. A machine-wide
lock keyed by normalized relay URL plus public key prevents two profile
processes from driving the same identity. Different profile keys may connect to
the same relay concurrently.

Buzz membership is transport access, not Homie authorization. An inbound event
must pass all of these checks before dispatch:

1. bounded, well-formed frame and kind-9 event;
2. canonical Nostr event-id match;
3. valid BIP-340 Schnorr signature through `coincurve`/libsecp256k1;
4. not a self-echo;
5. the signed event contains exactly one `h` tag matching the subscribed channel;
6. sender public key is in `BUZZ_ALLOWED_PUBKEYS`;
7. sender resolves through `BUZZ_PUBKEY_ROLES`, defaulting to `viewer`;
8. room mention gate, when enabled;
9. existing Homie router role and action policy.

DMs do not require mentions. Rooms require them by default. DM status comes
from the official CLI's relay-confirmed kind-41001 discovery or Buzz v0.5.2's
structured `channel_type=dm` projection of relay-only kind-39000 metadata. A
mutable channel name such as `DM` never bypasses the room mention gate. A
leading Homie mention is stripped before slash-command detection, so
`@Homie /status` reaches the same command router as other channels.

## Transport State Machine

`BUZZ_TRANSPORT=auto` is the default:

```text
connecting -> connected/websocket
                    |
                    v disconnect
              degraded/polling -> periodic authenticated WebSocket recovery
```

NIP-42 WebSocket subscriptions are primary. The official `buzz` CLI owns
profile/channel/DM discovery, polling, sends, replies, reactions, and file
uploads. CLI content travels over stdin, the private key travels only in the
subprocess environment, and commands always use argument arrays without a
shell.

Per-channel high-water seconds, every event id observed at the high-water
second, and a bounded recent-id set are stored in the active profile's
`STATE_DIR/buzz-state.db`. First connection seeds the latest cursor without
dispatching history. Inclusive polling overlap then prevents both same-second
loss and duplicate delivery across restarts.

## Configuration

Required in every Buzz-enabled Homie profile:

| Variable | Purpose |
|---|---|
| `BUZZ_RELAY_URL` | The profile's Buzz community relay. |
| `BUZZ_PRIVATE_KEY` | 64-character hex or `nsec`; secret storage only. |
| `BUZZ_ALLOWED_PUBKEYS` | Comma-separated authorized hex pubkeys or `npub`s. |

Routing and optional behavior:

| Variable | Default | Purpose |
|---|---|---|
| `BUZZ_CHANNELS` | all joined | Comma-separated room UUIDs to watch. |
| `BUZZ_PUBKEY_ROLES` | empty | Comma-separated `pubkey=viewer|operator|admin`; unmapped allowlisted senders are `viewer`. |
| `BUZZ_HOME_CHANNEL` | empty | Default scheduled-delivery destination. |
| `BUZZ_SIGNAL_CHANNEL` | empty | Only destination for redacted work receipts. |
| `BUZZ_CLI_PATH` | `buzz` | Explicit official CLI executable. |
| `BUZZ_TRANSPORT` | `auto` | `auto`, `websocket`, or `poll`. |
| `BUZZ_REQUIRE_MENTION` | `true` | Require a visible Homie mention in rooms. |
| `BUZZ_DESKTOP_PATH` | empty | Optional absolute stock Buzz Desktop executable. |

Pilot compatibility is Buzz `0.5.x`. The adapter was initially accepted against
`v0.5.2`; the local operator foundation is now pinned to `desktop-v0.5.3`.
The official v0.5.3 CLI does not expose a `--version` flag, so Homie may report
the CLI version as unknown even when that exact binary is installed. Treat that
as an honest provenance warning, not a failed relay. Until detection is
hardened, verify the source tag and commit and test the actual data path.

Secrets never appear in status JSON, Dashboard responses, logs, receipts, or
the public export. Do not put a key in Dashboard settings: settings are a
read-only view over the Python-owned status contract.

## Operating It

### Current Local Pilot Receipt

The 2026-08-02 Windows pilot established this foundation:

| Component | Verified state |
|---|---|
| Source | `desktop-v0.5.3` at commit `3a96acea09b4a9e3f02c3a26cfb0607d2ccacf42` |
| Infrastructure | Postgres, Redis, MinIO, Adminer, Keycloak, and Prometheus started through Buzz's official development Compose file |
| Relay | Migrations and local-community seed completed; `GET /_liveness` returned `200 ok` on loopback |
| Agent tools | Release builds of the official `buzz`, `buzz-admin`, and `buzz-relay` binaries |
| Desktop | Official v0.5.3 Windows package installed; `buzz://` registered |
| Homie adapter | Not yet activated: no Homie private key, allowlist, or channel routing was written |

This receipt proves installed local infrastructure, not background-service
durability after reboot and not a completed signed room/DM acceptance run. The
relay may be running locally while `thehomie status --json` correctly reports
Buzz disabled until the active Homie profile has all required configuration.

### Install Or Rebuild The Local Stack

Use a dedicated upstream checkout outside `thehomie`; never vendor Buzz
into the Homie repository. On Windows, install Docker Desktop, Git for Windows,
Rust through rustup, Node 24+, pnpm 10+, and `just`. Then pin the reviewed tag:

```powershell
git clone https://github.com/block/buzz.git C:\src\buzz
cd C:\src\buzz
git fetch --tags
git checkout desktop-v0.5.3
git rev-parse HEAD
Copy-Item .env.example .env
```

Review `.env` before starting. The upstream local-development defaults are for
loopback only. Do not place a Homie profile's `BUZZ_PRIVATE_KEY` in the relay
checkout; agent identities belong in profile-owned secret storage.

Buzz's canonical source setup uses Git Bash and Hermit:

```bash
. ./bin/activate-hermit
just setup
just build
```

If Hermit cannot obtain a native Windows artifact, leave the upstream checkout
unchanged and use its documented global-toolchain fallback. The checked-out
release's `rust-toolchain.toml` remains authoritative:

```powershell
rustup override set 1.95.0
docker compose up -d
cargo build --release -p buzz-cli -p buzz-admin -p buzz-relay
.\target\release\buzz-admin.exe migrate
& "$env:ProgramFiles\Git\bin\bash.exe" ./scripts/seed-local-community.sh
.\target\release\buzz-relay.exe
```

Install Buzz Desktop only from the matching upstream release page. The v0.5.3
Windows package is explicitly an alpha unsigned build, so Windows will not show
an Authenticode publisher. Verify the downloaded asset against the digest
published by GitHub before running it. The verified v0.5.3 Windows installer
SHA-256 is:

```text
43295e951f851a91b5012ae87c30a3c375f07f762ab382763247fa11697a89f3
```

After installation, verify the local service and desktop link without exposing
any identity material:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/_liveness
Test-Path "$env:LOCALAPPDATA\Buzz\Buzz.exe"
Test-Path Registry::HKEY_CURRENT_USER\Software\Classes\buzz
```

For an unattended long-running relay, use an operator-owned service supervisor
with bounded logs. A manually launched terminal process is pilot proof, not an
autostart contract.

### Start Homie's Adapter

Run only Buzz:

```powershell
cd .claude\scripts
uv run thehomie chat --buzz
```

Deliver a scheduled or cron result to the profile's fixed home channel:

```powershell
uv run thehomie buzz deliver "Daily result"
```

The command accepts repeatable `--file` attachments and `--json` for a bounded
machine receipt. It cannot select an arbitrary destination; routing is fixed by
`BUZZ_HOME_CHANNEL` in the active profile.

Or start the normal multi-channel bot; Buzz auto-registers when all three
required variables are present.

Inspect operator truth:

```powershell
uv run thehomie status --json
uv run thehomie doctor
```

`/diagnostics`, status JSON, the Capability Gateway, and Dashboard Settings
show enabled state, `connected|degraded|failed`, active transport, relay host,
truncated `npub`, watched-channel count, last event time, CLI version and
compatibility, lock conflict, and last error. A stale persisted runtime snapshot
fails closed instead of pretending the adapter is connected.

The Dashboard's **Open Buzz** action asks Electron to open the fixed `buzz://`
scheme. If `BUZZ_DESKTOP_PATH` is explicitly set, Electron validates that it is
an existing absolute file and opens that file. Electron does not install,
embed, start, stop, or update Buzz.

### Stop, Upgrade, And Roll Back

From the pinned Buzz source checkout:

```powershell
docker compose ps
docker compose down
```

`docker compose down` stops the development services while preserving named
volumes. Do not run `just reset` during normal operations: upstream defines it
as a development-data wipe. It requires explicit approval plus a verified
backup of relay data and desktop/profile identities.

For an upgrade:

1. Read the upstream release notes, changelog, architecture changes, and security policy.
2. Record the installed tag, commit, binary hashes, and local backup location.
3. Stop the Homie Buzz adapter before stopping the relay.
4. Fetch and check out the exact reviewed release tag; never upgrade from a moving branch.
5. Rebuild the three release binaries, run migrations, and restart the relay.
6. Run the focused Homie suite and the signed room, DM, restart, and receipt acceptance checks.
7. Keep the prior binaries/tag and data backup until the new build passes.

If validation fails, stop the adapter and relay, restore the previous binaries
and tag, restore data only if the migration requires it, and rerun liveness plus
the last known-good signed-message smoke before re-enabling Homie.

## Activation Plan

The dependency/install phase is complete. Activate the collaboration surface in
these bounded gates:

1. **Owner bootstrap:** open stock Buzz Desktop, create the operator identity,
   and create one home room plus one signal-only room on the local relay.
2. **Homie identity:** generate a separate Nostr keypair for each Homie profile,
   add only its public identity to Buzz, and store its private key only in that
   profile's secret store.
3. **Authorization and routing:** set the relay URL, explicit sender allowlist,
   watched channels, home channel, signal channel, roles, CLI path, and desktop
   path. Keep room mentions required and ACP/workflow approvals disabled.
4. **First signed acceptance:** start `thehomie chat --buzz`; prove an authorized
   room mention receives one reaction and one final answer, a DM works without a
   mention, and an unauthorized pubkey executes nothing.
5. **Continuity and signals:** restart both sides and prove no replay or loss;
   then prove started, approval-required, completed, and failed receipts reach
   only the signal channel and contain no private material.
6. **Multi-profile and durability:** add a second Homie key, prove distinct
   identities and lock behavior, then decide on relay autostart, backup cadence,
   retention, and a reviewed upgrade window.

Do not call the pilot active until steps 1-5 pass with real signed events and
the Dashboard reports the adapter's actual state. Approvals remain executable
only through Homie's guarded operator surfaces.

## Work Receipts

The signal channel receives ordinary Buzz chat messages for:

- `work.started`
- `work.approval_required`
- `work.completed`
- `work.failed`

The durable outbox accepts only `work_id`, work type, Homie profile, bounded
redacted summary, status, timestamp, and a local Dashboard path. It rejects
external/query-bearing Dashboard paths and excludes prompts, memory, mailbox
bodies, tool arguments, credentials, errors, and full audit records.

Convoy creation and terminal transitions enqueue started/completed/failed.
Mailbox messages of type `approval_request` enqueue approval-required without
copying the message body. A unique idempotency key prevents duplicate receipt
delivery. Only `BUZZ_SIGNAL_CHANNEL` is used.

Buzz reactions and workflow events never approve work. Approval is executable
solely through Homie's existing guarded surfaces.

## Safety Boundaries

- ACP remains disabled because it does not match Homie's default-deny tool boundary.
- Buzz workflow approval gates are not consumed.
- Channels are not treated as end-to-end encrypted; the first pilot is local-only.
- Relay membership never bypasses the Homie public-key allowlist or action policy.
- Sender roles are profile-owned mappings; no Buzz event can elevate its own role.
- The adapter sends one receipt reaction and the final answer. Progress chatter is disabled.
- The old Mission Control relay and heartbeat are deprecated compatibility code; deletion is a separate audited retirement change.
- The official Buzz stack remains external. Do not vendor or fork it into this repository.

## Source And Verification

| Layer | Files |
|---|---|
| Adapter | `.claude/chat/adapters/buzz.py` |
| Nostr/crypto boundary | `.claude/chat/buzz_nostr.py`, `coincurve` dependency |
| Identity lock | `.claude/chat/buzz_lock.py` |
| Profile config/status | `.claude/scripts/buzz_config.py`, `.claude/scripts/buzz_status.py` |
| Cursor/dedupe/outbox | `.claude/scripts/buzz_state.py`, `.claude/scripts/buzz_signals.py` |
| Work projection | `orchestration/convoy_service.py`, `orchestration/mailbox_service.py` |
| Operator UI | `orchestration/capability_gateway.py`, `dashboard/web`, `dashboard/desktop` |

Focused verification:

```powershell
cd .claude\scripts
uv run pytest tests/test_buzz_nostr.py tests/test_buzz_state_and_signals.py `
  tests/test_adapter_buzz.py tests/test_buzz_transport_integration.py `
  tests/test_buzz_operator_surfaces.py -q
```

The fake-relay integration proves NIP-42 authentication, signed dispatch,
disconnect degradation to inclusive CLI polling, and authenticated WebSocket
recovery. The adapter's original local acceptance passed against official
v0.5.2, including a post-hardening signed mention-free DM over WebSocket. The
v0.5.3 relay/Desktop foundation is now installed separately; its real Homie
identity and channel acceptance remains the next gate. No public relay,
production deployment, or public-framework export is claimed.
