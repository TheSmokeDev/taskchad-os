"""Chat router connecting platform adapters to the conversation engine.

Uses ExtensionManager for command dispatch instead of hardcoded elif chains.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any

from commands import get_command_min_role, get_engine_command_description, get_piv_instruction
from engine import ConversationEngine
from extension_manager import ExtensionManager
from models import OutgoingMessage, Platform
from session import Session
from session_keys import build_session_key, resolve_thread_id

# gap-4 URL ingest — raw-regex match on the original message text BEFORE any
# command parsing. Routing on parsed[0] == "vault-ingest" would NOT fire because
# vault-ingest is a Skill (not in the router_commands registry); this regex is
# the only path that triggers URL ingest from chat surface.
_VAULT_INGEST_URL_RE = re.compile(
    r"^/vault-ingest\s+(https?://\S+)\s*$", re.IGNORECASE
)

ENGINE_TIMEOUT_SECONDS = 90


class ChatRouter:
    """Routes messages between platform adapters and the conversation engine.

    Handles concurrent message processing — each incoming message spawns
    its own task so multiple conversations can run simultaneously.
    """

    def __init__(self, engine: ConversationEngine, manager: ExtensionManager) -> None:
        self.engine = engine
        self.adapters: dict[Platform, Any] = {}
        self.manager = manager
        self._transcript_reset_commands = {"clear", "reload"}

    def register(self, adapter: Any) -> None:
        """Register a platform adapter."""
        self.adapters[adapter.platform] = adapter
        print(f"[{datetime.now()}] Registered adapter: {adapter.platform.value}")

    async def run(self) -> None:
        """Connect all adapters and start listening for messages."""
        if not self.adapters:
            print(f"[{datetime.now()}] No adapters registered, nothing to do")
            return

        # Initialize core handler context (engine, adapters, start time)
        try:
            from core_handlers import set_context
            set_context(engine=self.engine, adapters=self.adapters, bot_start_time=datetime.now())
        except ImportError:
            pass  # core_handlers not available — handlers will degrade gracefully

        # Connect adapters individually — one failing shouldn't block the rest
        for platform, adapter in list(self.adapters.items()):
            try:
                await adapter.connect()
            except Exception as e:
                print(f"[{datetime.now()}] FATAL: {platform.value} adapter failed to connect: {e}", flush=True)
                del self.adapters[platform]

        if not self.adapters:
            raise RuntimeError("All adapters failed to connect")

        print(f"[{datetime.now()}] All adapters connected")

        # Create a listen task per adapter
        tasks = [asyncio.create_task(self._listen(adapter)) for adapter in self.adapters.values()]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            print(f"[{datetime.now()}] Router shutting down...")

    async def _listen(self, adapter: Any) -> None:
        """Listen for incoming messages from a single adapter.

        Restarts automatically on transient errors with backoff.
        Only gives up after 5 consecutive failures.
        """
        max_retries = 5
        retry_count = 0
        while retry_count < max_retries:
            try:
                async for incoming in adapter.listen():
                    retry_count = 0  # Reset on successful message
                    asyncio.create_task(self._handle(adapter, incoming))
                # listen() returned without error — generator exhausted (shouldn't happen)
                print(f"[{datetime.now()}] WARNING: {adapter.platform.value} listener exited cleanly — restarting", flush=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                retry_count += 1
                backoff = min(2 ** retry_count, 30)
                print(
                    f"[{datetime.now()}] Listener error ({adapter.platform.value}): {e} "
                    f"— retry {retry_count}/{max_retries} in {backoff}s",
                    flush=True,
                )
                await asyncio.sleep(backoff)
        print(f"[{datetime.now()}] FATAL: {adapter.platform.value} listener gave up after {max_retries} retries", flush=True)

    def _parse_command(self, text: str) -> tuple[str, str] | None:
        """Return (command, args) if text is a known bot command, else None."""
        m = self.manager.command_regex.match(text.strip())
        if m:
            return m.group(1).lower(), m.group(2).strip()
        return None

    def _parse_multi_commands(self, text: str) -> list[tuple[str, str]] | None:
        """Parse multiple /commands from a single message (e.g. '/email /gsc /analytics').

        Returns list of (command, args) tuples, or None if <2 commands found.
        """
        all_names = self.manager.get_all_command_names()
        pattern = re.compile(r"/(" + "|".join(all_names) + r")\b", re.IGNORECASE)
        matches = list(pattern.finditer(text.strip()))
        if len(matches) < 2:
            return None
        result = []
        for i, m in enumerate(matches):
            cmd = m.group(1).lower()
            arg_start = m.end()
            arg_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            args = text[arg_start:arg_end].strip()
            result.append((cmd, args))
        return result

    async def _handle(self, adapter: Any, incoming: Any) -> None:
        """Handle a single incoming message: post placeholder, run engine, update.

        Wrapped in a top-level try/except so unhandled errors always send an
        error message back to the user instead of silently dying.
        """
        try:
            await self._handle_inner(adapter, incoming)
        except Exception as e:
            # Last resort — never leave the user on read
            print(f"[{datetime.now()}] UNHANDLED error in _handle: {e}")
            try:
                await adapter.send(
                    OutgoingMessage(
                        text=f"Something broke unexpectedly: {type(e).__name__}: {e}",
                        channel=incoming.channel,
                        thread=incoming.thread,
                        is_error=True,
                    )
                )
            except Exception:
                pass  # If even the error message fails, nothing more we can do

    async def _handle_inner(self, adapter: Any, incoming: Any) -> None:
        """Core message handling logic."""
        text = incoming.text or ""

        # --- Button clicks: __button:{custom_id} ---
        if text.startswith("__button:"):
            await self._handle_button(adapter, incoming, text[len("__button:"):])
            return

        # --- gap-4 URL ingest: /vault-ingest <url> short-circuits to deterministic
        # router-side fetch + archive + compile. Raw-regex match on the message
        # text — does NOT route through _parse_command (vault-ingest is a Skill,
        # not a router command, so parsed[0] would never reach the router_commands
        # registry). Plain text fall-through preserves the existing skill flow
        # for file-path inputs.
        stripped_text = text.strip()
        m = _VAULT_INGEST_URL_RE.match(stripped_text)
        if m:
            url = m.group(1)
            await self._handle_vault_ingest_url(adapter, incoming, url)
            return

        router_commands = self.manager.get_router_commands()

        # --- Multi-command: /email /gsc /analytics -> chain all ---
        multi = self._parse_multi_commands(text)
        if multi:
            router_cmds = [(cmd, a) for cmd, a in multi if cmd in router_commands]
            if router_cmds:
                replies: list[str] = []
                had_error = False
                for cmd, cmd_args in router_cmds:
                    try:
                        r = await self.manager.dispatch(
                            cmd, adapter, incoming, cmd_args, collect_only=True,
                        )
                        if r:
                            replies.append(f"*/{cmd}*\n{r}")
                    except Exception as e:
                        had_error = True
                        replies.append(f"*/{cmd}*\nError: {e}")
                if replies:
                    combined = "\n\n━━━━━━━━━━━━━━━\n\n".join(replies)
                    await adapter.send(
                        OutgoingMessage(
                            text=combined,
                            channel=incoming.channel,
                            thread=incoming.thread,
                            is_error=had_error,
                        )
                    )
                    if not any(cmd in self._transcript_reset_commands for cmd, _ in router_cmds):
                        self._persist_router_turn(incoming, combined)
                    return

        # --- /file accept|diff <id> — gap-6 conversational compounding ---
        # Intercept here so the engine never sees these subcommands. The
        # base /file slash command remains routed through the engine /
        # extension system; only accept|diff are deterministic Python.
        parsed = self._parse_command(text)
        if parsed and parsed[0] == "file":
            args = parsed[1]
            if args.startswith("accept ") or args.startswith("diff "):
                sub, _, sub_args = args.partition(" ")
                auto_id = sub_args.strip().split()[0] if sub_args else ""
                reply = await self._handle_file_subcommand(sub, auto_id)
                await adapter.send(
                    OutgoingMessage(
                        text=reply,
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
                self._persist_router_turn(incoming, reply)
                return

        # --- Single command: /email -> handle directly ---
        if parsed:
            command, args = parsed
            if command in router_commands:
                reply = await self.manager.dispatch(command, adapter, incoming, args)
                if reply is not None:
                    await adapter.send(
                        OutgoingMessage(
                            text=reply,
                            channel=incoming.channel,
                            thread=incoming.thread,
                        )
                    )
                    if command not in self._transcript_reset_commands:
                        self._persist_router_turn(incoming, reply)
                return

            # Role check for engine commands
            user_role = getattr(incoming, "user_role", "admin")
            min_role = get_command_min_role(command)
            role_level = {"viewer": 0, "operator": 1, "admin": 2}
            if role_level.get(user_role, 0) < role_level.get(min_role, 0):
                await adapter.send(
                    OutgoingMessage(
                        text=f"Permission denied: /{command} requires {min_role} role.",
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
                return

            # Engine command — convert to natural language for the SDK
            piv_content = get_piv_instruction(command, args)
            if piv_content:
                incoming.text = piv_content
                incoming.is_piv = True
                incoming.piv_command = command
            elif command == "clutch":
                clutch_prompt = (
                    f"Use the Skill tool to invoke the 'clutch' skill with arguments: {args}"
                    if args
                    else "Use the Skill tool to invoke the 'clutch' skill"
                )
                incoming.text = clutch_prompt
            elif command == "quote":
                quote_prompt = (
                    f"Use the Skill tool to invoke the 'turborater-quote' skill with arguments: {args}"
                    if args
                    else "Use the Skill tool to invoke the 'turborater-quote' skill. Ask the user for: full name, vehicle (year make model), zip code, and coverage type (liability or full coverage)."
                )
                incoming.text = quote_prompt
                incoming.is_piv = True
                incoming.piv_command = "clutch"
            else:
                desc = get_engine_command_description(command)
                if args:
                    incoming.text = f"{desc}: {args}"
                else:
                    incoming.text = desc or command

        # --- Smart intent detection: natural language -> router commands ---
        if not parsed:
            requires_confirmation = getattr(
                self.manager, "requires_external_action_confirmation", None
            )
            if callable(requires_confirmation) and requires_confirmation(text):
                build_confirmation = getattr(
                    self.manager,
                    "build_external_action_confirmation",
                    None,
                )
                if callable(build_confirmation):
                    reply = build_confirmation(text)
                else:
                    reply = (
                        "That sounds like it may contact a real person or mutate "
                        "a live surface. Reply with a clear direct instruction "
                        "or use the explicit slash command if you want me to proceed."
                    )
                await adapter.send(
                    OutgoingMessage(
                        text=reply,
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
                self._persist_router_turn(incoming, reply)
                return

            intents = self.manager.detect_intents(text)
            if intents:
                data_parts: list[str] = []
                for cmd in intents:
                    try:
                        r = await self.manager.dispatch(
                            cmd, adapter, incoming, "", collect_only=True,
                        )
                        if r:
                            data_parts.append(f"## /{cmd}\n{r}")
                    except Exception as e:
                        data_parts.append(f"## /{cmd}\nError: {e}")

                # Drop pure-error results — let them fall through to the engine
                data_parts = [
                    p for p in data_parts
                    if not p.split("\n", 1)[-1].strip().startswith("Error ")
                ]

                if data_parts:
                    if self.manager.wants_analysis(text):
                        incoming.prefetched_context = "\n\n".join(data_parts)
                    else:
                        if len(intents) == 1:
                            reply = data_parts[0].split("\n", 1)[1]
                        else:
                            reply = "\n\n━━━━━━━━━━━━━━━\n\n".join(
                                f"*/{cmd}*\n{p.split(chr(10), 1)[1]}"
                                for cmd, p in zip(intents, data_parts)
                            )
                        await adapter.send(
                            OutgoingMessage(
                                text=reply,
                                channel=incoming.channel,
                                thread=incoming.thread,
                            )
                        )
                        self._persist_router_turn(incoming, reply)
                        return

        try:
            print(
                f"[{datetime.now()}] Message from {incoming.user.platform_id} "
                f"in {incoming.channel.platform_id}: {text[:80]}..."
            )
        except UnicodeEncodeError:
            safe_text = text[:80].encode("ascii", "replace").decode()
            print(
                f"[{datetime.now()}] Message from {incoming.user.platform_id} "
                f"in {incoming.channel.platform_id}: {safe_text}..."
            )

        # Post "Thinking..." placeholder
        placeholder_id: str | None = None
        try:
            placeholder_id = await adapter.send(
                OutgoingMessage(
                    text="Thinking...",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        except Exception as e:
            print(f"[{datetime.now()}] Failed to send placeholder: {e}")

        # Run engine with a progress ticker
        progress: dict[str, Any] = {"tool_calls": 0, "started": time.time()}

        async def _tick_progress() -> None:
            """Update placeholder with elapsed time every 12 seconds."""
            while True:
                await asyncio.sleep(12)
                elapsed = int(time.time() - progress["started"])
                calls = progress.get("tool_calls", 0)
                status = f"Working... ({elapsed}s)"
                if calls:
                    status += f" | {calls} tool calls"
                try:
                    if placeholder_id:
                        await adapter.update(
                            OutgoingMessage(
                                text=status,
                                channel=incoming.channel,
                                thread=incoming.thread,
                                is_update=True,
                                update_message_id=placeholder_id,
                            )
                        )
                except Exception:
                    pass

        progress_task = asyncio.create_task(_tick_progress()) if placeholder_id else None

        final_text = ""
        final_is_error = False
        final_footer: str | None = None
        final_components: list[Any] = []

        async def _run_engine() -> None:
            nonlocal final_text, final_is_error, final_footer, final_components
            async for outgoing in self.engine.handle_message(incoming, progress=progress):
                final_text = outgoing.text
                final_is_error = getattr(outgoing, "is_error", False)
                # gap-6: capture engine-side footer + components (concept draft).
                # Persistence (_persist_router_turn) keeps using final_text only —
                # footer never enters chat_history.
                final_footer = getattr(outgoing, "footer", None)
                yielded_components = getattr(outgoing, "components", None) or []
                if yielded_components:
                    final_components = list(yielded_components)

        try:
            await asyncio.wait_for(_run_engine(), timeout=ENGINE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print(f"[{datetime.now()}] Engine timed out after {ENGINE_TIMEOUT_SECONDS}s")
            final_text = (
                f"That took too long (>{ENGINE_TIMEOUT_SECONDS}s). "
                "Try a more specific question or break it into smaller queries."
            )
            final_is_error = True
        except Exception as e:
            print(f"[{datetime.now()}] Engine error: {e}")
            final_text = f"Sorry, something went wrong: {e}"
            final_is_error = True
        finally:
            if progress_task:
                progress_task.cancel()

        # Update the placeholder with the final response
        if not final_text.strip():
            final_text = "I processed your request but had no text response."

        # Parse <<BLOG_RESULTS>> marker — attach Publish/Skip buttons
        components = self._extract_result_buttons(final_text)
        # If the engine attached components (e.g. concept draft Accept/Diff),
        # carry them into the outgoing message alongside any blog buttons.
        if final_components:
            components = list(components) + list(final_components)

        try:
            if placeholder_id:
                await adapter.update(
                    OutgoingMessage(
                        text=final_text,
                        channel=incoming.channel,
                        thread=incoming.thread,
                        is_update=True,
                        update_message_id=placeholder_id,
                        is_error=final_is_error,
                        footer=final_footer,
                    )
                )
                # Buttons can't be added to edits — send as follow-up
                if components:
                    await adapter.send(
                        OutgoingMessage(
                            text="Ready to publish?" if not final_components else "",
                            channel=incoming.channel,
                            thread=incoming.thread,
                            components=components,
                            footer=final_footer if final_components else None,
                        )
                    )
            else:
                await adapter.send(
                    OutgoingMessage(
                        text=final_text,
                        channel=incoming.channel,
                        thread=incoming.thread,
                        is_error=final_is_error,
                        components=components,
                        footer=final_footer,
                    )
                )
        except Exception as e:
            print(f"[{datetime.now()}] Failed to send response: {e}")

    async def _handle_file_subcommand(self, sub: str, auto_id: str) -> str:
        """Dispatch /file accept|diff <id> to the concept_drafter module.

        gap-6 — deterministic Python path; no engine round-trip.
        Returns a user-facing reply string. Never raises — surfaces errors
        as text replies so the user sees what went wrong.
        """
        if not auto_id:
            return f"Usage: `/file {sub} <draft-id>` (8-char prefix or full UUID)."
        try:
            from concept_drafter import (
                DraftAmbiguityError,
                accept_draft,
                diff_draft,
            )
            from config import MEMORY_DIR
        except Exception as e:  # noqa: BLE001
            return f"Drafter unavailable: {e}"

        try:
            if sub == "accept":
                result = accept_draft(auto_id, MEMORY_DIR)
            elif sub == "diff":
                result = diff_draft(auto_id, MEMORY_DIR)
            else:
                return f"Unknown subcommand `/file {sub}`."
        except DraftAmbiguityError as e:
            slugs = []
            for p in e.candidates:
                stem = getattr(p, "stem", str(p))
                slugs.append(f"`{stem}`")
            joined = ", ".join(slugs[:5])
            return (
                f"Multiple drafts match `{auto_id}`. Be more specific. "
                f"Candidates: {joined}"
            )
        except Exception as e:  # noqa: BLE001
            return f"Couldn't {sub} draft `{auto_id}`: {e}"

        status = result.get("status", "")
        if status == "not_found":
            return f"No draft matched `{auto_id}`. It may have been swept."
        if status == "error":
            return f"Failed to {sub} draft `{auto_id}`: {result.get('error', 'unknown error')}"
        if sub == "accept":
            path = result.get("path", "")
            connections = result.get("connections", []) or []
            contradictions = result.get("contradictions", []) or []
            lines = [f"Filed draft to `{path}`."]
            if connections:
                lines.append(f"Connections: {len(connections)}")
            if contradictions:
                lines.append(f"Contradictions flagged: {len(contradictions)}")
            return "\n".join(lines)
        # diff
        preview = result.get("preview", "")
        return f"Draft preview (`{auto_id}`):\n\n{preview}"

    async def _handle_vault_ingest_url(
        self, adapter: Any, incoming: Any, url: str
    ) -> None:
        """Router-side URL ingest (gap-4).

        Fetch + archive html+md to ``{vault}/raw/clipped/`` then run the entity
        compilation cascade. Reply with concept/connection/contradiction counts.
        Never reaches the engine — fully deterministic.
        """
        try:
            await adapter.send(
                OutgoingMessage(
                    text=f"Fetching {url}...",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        except Exception:
            # Placeholder send is best-effort; if it fails we still try to fetch.
            pass

        try:
            html_path, md_path, content, report = await asyncio.to_thread(
                self._url_ingest_pipeline, url
            )
        except Exception as e:
            await adapter.send(
                OutgoingMessage(
                    text=(
                        f"Couldn't fetch {url}: {type(e).__name__}: {e}. "
                        "Try saving the page as a file and ingesting that instead."
                    ),
                    channel=incoming.channel,
                    thread=incoming.thread,
                    is_error=True,
                )
            )
            return

        title = content.title or md_path.stem
        n_concepts = len(report.pages_created) + len(report.pages_updated)
        n_connections = len(report.connections_created)
        n_contradictions = len(report.contradictions_found)
        reply = (
            f"Ingested '{title}'. "
            f"{n_concepts} concepts, {n_connections} connections, "
            f"{n_contradictions} contradictions. "
            f"Raw: `{html_path.name}`, `{md_path.name}`."
        )
        await adapter.send(
            OutgoingMessage(
                text=reply,
                channel=incoming.channel,
                thread=incoming.thread,
            )
        )
        self._persist_router_turn(incoming, reply)

    @staticmethod
    def _url_ingest_pipeline(url: str):
        """Synchronous fetch + archive + compile pipeline.

        Runs off the event loop (called via ``asyncio.to_thread`` from the
        async handler). Returns ``(html_path, md_path, content, report)``.
        """
        from url_fetch import fetch_and_archive
        from entity_extractor import compile_entities, extract_entities_heuristic
        from config import MEMORY_DIR

        vault_dir = MEMORY_DIR
        html_path, md_path, content = fetch_and_archive(url, vault_dir)
        md_text = md_path.read_text(encoding="utf-8")
        ents = extract_entities_heuristic(md_text, str(md_path))
        report = compile_entities(ents, str(md_path), vault_dir, MEMORY_DIR)
        return html_path, md_path, content, report

    def _extract_result_buttons(self, text: str) -> list[Any]:
        """Parse <<BLOG_RESULTS>> or <<QUOTE_RESULTS>> markers and return action buttons.

        Returns empty list if no marker found.
        """
        from models import MessageComponent
        import json

        # Blog results → Publish/Skip buttons
        if "<<BLOG_RESULTS>>" in text:
            try:
                start = text.index("<<BLOG_RESULTS>>") + len("<<BLOG_RESULTS>>")
                end = text.index("<</BLOG_RESULTS>>")
                data = json.loads(text[start:end].strip())
                draft_id = data.get("draft_id", "")
                if draft_id:
                    return [
                        MessageComponent(label="Publish", custom_id=f"blog_publish:{draft_id}", style="success"),
                        MessageComponent(label="Skip", custom_id=f"blog_skip:{draft_id}", style="secondary"),
                    ]
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                print(f"[{datetime.now()}] Failed to parse BLOG_RESULTS: {e}")

        # Quote results — no buttons needed, just let the text through
        # (quote results are self-contained carrier rate displays)

        return []

    async def _handle_button(self, adapter: Any, incoming: Any, custom_id: str) -> None:
        """Handle a button click routed as __button:{custom_id}.

        Supports blog_publish:{draft_id}, blog_skip:{draft_id}, and
        other future button patterns.
        """
        if custom_id.startswith("blog_publish:"):
            draft_id = custom_id.split(":", 1)[1]
            try:
                # Lazy import — extension may not be available
                from extensions.blog.handlers import handle_publish

                reply = await handle_publish(adapter, incoming, draft_id)
            except ImportError:
                reply = f"Blog extension not loaded. Manually publish at admin.your-business.example.com > Blog (ID: {draft_id})"
            except Exception as e:
                reply = f"Publish failed: {e}"

            await adapter.send(
                OutgoingMessage(
                    text=reply,
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("blog_skip:"):
            draft_id = custom_id.split(":", 1)[1]
            await adapter.send(
                OutgoingMessage(
                    text=f"Skipped. Draft `{draft_id}` saved for later — view at admin.your-business.example.com > Blog.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id == "quote_approve":
            # Run the TurboRater quote via PIV
            incoming.text = "Use the Skill tool to invoke the 'turborater-quote' skill with the lead info from our conversation."
            incoming.is_piv = True
            incoming.piv_command = "quote"
            # Re-route through normal engine handling
            await self._handle_inner(adapter, incoming)
        elif custom_id == "quote_cancel":
            await adapter.send(
                OutgoingMessage(
                    text="Quote cancelled.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("concept_accept:"):
            auto_id = custom_id.split(":", 1)[1]
            reply = await self._handle_file_subcommand("accept", auto_id)
            await adapter.send(
                OutgoingMessage(
                    text=reply,
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("concept_diff:"):
            auto_id = custom_id.split(":", 1)[1]
            reply = await self._handle_file_subcommand("diff", auto_id)
            await adapter.send(
                OutgoingMessage(
                    text=reply,
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("concept_ignore:"):
            await adapter.send(
                OutgoingMessage(
                    text="Skipped. Draft will sweep itself in 24h.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        else:
            # Unknown button — log and ignore
            print(f"[{datetime.now()}] Unknown button: {custom_id}")

    def _persist_router_turn(self, incoming: Any, reply: str) -> None:
        """Persist direct router-path turns into the transcript store."""

        store = getattr(self.engine, "session_store", None)
        if store is None or not hasattr(store, "add_message"):
            return
        if not reply.strip():
            return

        platform_str = incoming.platform.value
        channel_id = incoming.channel.platform_id
        thread_id = resolve_thread_id(
            channel_id,
            incoming.thread.thread_id if incoming.thread else None,
        )
        session_id = build_session_key(platform_str, channel_id, thread_id)
        now = datetime.now()
        existing = store.get(platform_str, channel_id, thread_id)

        if existing:
            existing.message_count += 1
            existing.updated_at = now
            store.update(existing)
        else:
            # PRP-7d R1 B2: read source from incoming; set-once on create
            # (the `if existing:` UPDATE branch above MUST NOT touch source).
            message_source = getattr(incoming, "source", "interactive")
            store.create(
                Session(
                    session_id=session_id,
                    agent_session_id="",
                    platform=platform_str,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    user_id=incoming.user.platform_id,
                    created_at=now,
                    updated_at=now,
                    message_count=1,
                    source=message_source,
                )
            )

        timestamp = getattr(incoming, "timestamp", now)
        store.add_message(session_id, "user", incoming.text, timestamp)
        store.add_message(session_id, "assistant", reply, now)

    async def shutdown(self) -> None:
        """Disconnect all adapters gracefully."""
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception as e:
                print(f"[{datetime.now()}] Error disconnecting {adapter.platform.value}: {e}")
