Natural language data queries are intercepted by the router before reaching the engine, eliminating token waste. The router detects data intents via the `ExtensionManager` registry and fetches data directly via Python API calls (~200ms each, zero runtime tokens).

**Location:** `.claude/chat/router.py` → `ExtensionManager.detect_intents()`, `_wants_analysis()`

### Two Paths

| Path | Trigger | Token Cost | Time |
|------|---------|------------|------|
| **Data-only** | "check my leads", "show email" | 0 tokens | ~1-3s |
| **Analysis** | "good morning, how are we looking?" | ~2-3K (TEXT_REASONING) | ~15-20s |

### How It Works

1. Each extension registers `IntentSpec` entries (keywords + target command) via its manifest
2. Router calls `manager.detect_intents(text)` — matches user text against registered keyword sets
3. Broad queries ("across all boards", "how are we looking") trigger all intents marked `included_in_brief=True`
4. Data is fetched via `manager.dispatch(command, collect_only=True)` — same execution path as slash commands
5. **Data-only** (no analysis signals): raw data returned immediately, engine never invoked
6. **Analysis wanted** (greetings, "summary", "prioritize", etc.): pre-fetched data attached to `incoming.prefetched_context`, engine invoked with `TEXT_REASONING` (no tools, cheapest provider, max 1 turn)

### Key Files

| File | Role |
|------|------|
| `extension_manager.py` | `ExtensionManager` — central registry for commands, `IntentSpec`, analysis/broad-query signals |
| `router.py` | Calls `manager.detect_intents()`, `_wants_analysis()`, orchestrates data-only vs analysis path |
| `models.py` | `IncomingMessage.prefetched_context` field |
| `engine.py` | Detects `prefetched_context` → injects into system prompt, forces `TEXT_REASONING` |

### Adding New Data Intents

Register an `IntentSpec` in your extension manifest with `keywords` and `command` (the target slash command). Set `included_in_brief=True` if the intent should be included in "show me everything" broad queries. The `ExtensionManager` auto-discovers intents from extension metadata at load time.
