"""Cabinet voice subprocess module — PRD-8 Phase 6 (port-first from ClaudeClaw warroom/).

Ships single-user voice cabinet via the actual ClaudeClaw upstream architecture:

* Server-rendered HTML page (port of ``src/warroom-html.ts`` 2057 LOC) at
  :mod:`cabinet.voice.voice_html`.
* Bundled Pipecat browser client (vendored from ``warroom/client.bundle.js``)
  served from :mod:`cabinet.voice.static`.
* Pipecat ``WebsocketServerTransport`` voice subprocess at
  :mod:`cabinet.voice.voice_server` (port of ``warroom/server.py:751-779``
  legacy mode pipeline).
* Pipecat FrameProcessors at :mod:`cabinet.voice.voice_router` (verbatim port
  of ``warroom/router.py``) and :mod:`cabinet.voice.agent_bridge` (structure
  verbatim from ``warroom/agent_bridge.py``; ``_call_agent`` body REPLACED to
  invoke Phase 5a's :func:`integrations.cabinet_api.send_message` +
  :func:`integrations.cabinet_api.stream_meeting`).
* Personas at :mod:`cabinet.voice.personas` (verbatim port of
  ``warroom/personas.py``; AGENT_PERSONAS hardcoded dict NOT ported per Q5
  single-config-yaml lock; per-persona prompt sourced from
  ``<profile>/config.yaml.cabinet.voice_persona_prompt``).

The voice subprocess never invokes any LLM directly. All persona reasoning
goes through Phase 5a's :func:`cabinet.text_orchestrator.handle_text_turn`
via HTTP to the orchestration API process — exactly the pattern Phase 5b
uses for the Telegram cabinet handlers. This preserves the "no watered-down
personas" lock: voice and Telegram cabinet meetings share the same brain.
"""

__all__: list[str] = []
