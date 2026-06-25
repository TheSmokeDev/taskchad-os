---
name: whatsapp-bridge
description: Add WhatsApp as a chat channel for the agent, routing inbound WhatsApp messages into the chat engine and replies back out. Use when the user wants to talk to the bot over WhatsApp, set up a WhatsApp number, or bridge WhatsApp into the existing multi-platform chat interface.
version: 1.0.0
author: TaskChad OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  taskchad:
    category: communication
    tags: [whatsapp, channel, chat, webhook, integration]
    related_skills: [voice-transcribe]
    mutates: true
    capability_gate: chat.send
---

# WhatsApp Bridge

Wire WhatsApp into the existing chat interface as one more channel adapter. The
framework already abstracts Telegram/Slack/Discord (see `.claude/sections/02_*`);
this adds a WhatsApp adapter behind the same interface — it does not fork the
engine.

## Provider

Use the WhatsApp Cloud API (Meta) or an equivalent gateway (e.g. Twilio for
WhatsApp). Both deliver inbound messages via webhook and accept outbound sends
via REST.

Required `.env` values:

```
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_VERIFY_TOKEN=          # for webhook handshake
WHATSAPP_ALLOWED_NUMBERS=       # comma-separated allowlist
```

## Adapter contract

Implement the channel adapter to match the existing chat interface:

1. **Inbound** — verify the webhook signature, drop senders not in
   `WHATSAPP_ALLOWED_NUMBERS`, normalize the payload to the engine's message
   shape, and hand it to the router.
2. **Outbound** — render the engine's reply to WhatsApp's message format and POST
   it to the Cloud API.

## Security

- Enforce `WHATSAPP_ALLOWED_NUMBERS` on every inbound message — mirror the
  `TELEGRAM_ALLOWED_USER_IDS` allowlist pattern. Unknown senders are dropped, not
  answered.
- Outbound sends are mutations: route them through the `chat.send` capability
  gate with an audit trail. Never call the WhatsApp API directly from a tool.
- Verify the webhook `X-Hub-Signature-256` HMAC before trusting any payload.
