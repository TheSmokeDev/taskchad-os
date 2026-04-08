# Contributing to The Homie

## Architecture Overview

See [docs/architecture.md](docs/architecture.md) for the full 9-layer cognitive stack.

The codebase follows a vertical slice architecture:

| Slice | Ownership |
|-------|-----------|
| `.claude/chat/` | Chat interface, routing, adapters |
| `.claude/chat/adapters/` | Platform-specific adapters |
| `.claude/chat/cognition/` | Cognitive modules (recall, capture, etc.) |
| `.claude/scripts/` | Scheduled jobs, integrations, config |
| `.claude/scripts/orchestration/` | Convoy DAGs, mailbox, team sessions, executor adapters, local API (port 4322) |
| `vault/memory/` | Obsidian vault (memory substrate) |

## Adding a New Adapter

1. Create `adapters/your_platform.py` implementing the `PlatformAdapter` protocol
2. Add `YOURPLATFORM = "yourplatform"` to `Platform` enum in `models.py`
3. Add config constants in `config.py`
4. Add dependencies to `pyproject.toml`
5. Wire the adapter in `main.py` (flag + registration)
6. Write tests in `tests/test_adapter_yourplatform.py`

See [docs/adapters.md](docs/adapters.md) for the full protocol reference.

## Adding a Cognition Module

1. Create `.claude/chat/cognition/your_module.py`
2. Use guarded imports (see `engine.py:50-63` for the pattern)
3. Add to `cognition/__init__.py` with a `_YOUR_MODULE_AVAILABLE` flag
4. Write tests in `tests/test_cognition_your_module.py`

## Testing

```bash
# Run all tests
cd .claude/scripts && uv run pytest tests/ -v

# Run specific test file
cd .claude/scripts && uv run pytest tests/test_adapter_discord.py -v

# Run with coverage
cd .claude/scripts && uv run pytest tests/ -v --tb=short
```

## Code Style

- **Formatter/linter**: ruff (line length 100)
- **Python version**: 3.12+
- **Type hints**: Required on all public functions
- **Imports**: Use `from __future__ import annotations` at the top of every file

```bash
# Lint
cd .claude/scripts && uv run ruff check .

# Format
cd .claude/scripts && uv run ruff format .
```

## Pull Request Guidelines

- One feature per PR
- All tests must pass
- Include tests for new functionality
- Update docs if adding adapters or cognition modules
