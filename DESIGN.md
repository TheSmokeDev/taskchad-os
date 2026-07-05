---
version: alpha
name: Homie Framework
description: "A dense, operator-first design system for The Homie runtime, Mission Control, and framework dashboards. It favors calm status clarity, lane-first runtime language, compact data surfaces, and visible proof over decorative marketing."

colors:
  canvas: "#0B0D10"
  surface-1: "#11151A"
  surface-2: "#171C22"
  surface-3: "#20262E"
  inverse-canvas: "#F7F8FA"
  inverse-surface: "#FFFFFF"
  ink: "#F4F7FA"
  ink-muted: "#B4BEC9"
  ink-subtle: "#788391"
  inverse-ink: "#121417"
  hairline: "#2A323C"
  hairline-strong: "#3A4653"
  primary: "#E56F4A"
  on-primary: "#121417"
  primary-hover: "#F08563"
  accent-soft: "#3A2019"
  success: "#29A36A"
  warning: "#D89D31"
  danger: "#D95C5C"
  info: "#4E9DD8"
  lane-auto: "#A6B0BA"
  lane-claude: "#D78356"
  lane-codex: "#5FA7E8"
  lane-gemini: "#77C7B2"
  lane-local: "#A88CE6"

typography:
  display:
    fontFamily: "Inter, SF Pro Display, Segoe UI, Arial, sans-serif"
    fontSize: 44px
    fontWeight: 650
    lineHeight: 1.08
    letterSpacing: 0
  headline:
    fontFamily: "Inter, SF Pro Display, Segoe UI, Arial, sans-serif"
    fontSize: 28px
    fontWeight: 650
    lineHeight: 1.18
    letterSpacing: 0
  title:
    fontFamily: "Inter, SF Pro Text, Segoe UI, Arial, sans-serif"
    fontSize: 18px
    fontWeight: 620
    lineHeight: 1.3
    letterSpacing: 0
  body:
    fontFamily: "Inter, SF Pro Text, Segoe UI, Arial, sans-serif"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: 0
  body-sm:
    fontFamily: "Inter, SF Pro Text, Segoe UI, Arial, sans-serif"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: 0
  label:
    fontFamily: "Inter, SF Pro Text, Segoe UI, Arial, sans-serif"
    fontSize: 12px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: 0
  mono:
    fontFamily: "JetBrains Mono, SFMono-Regular, Consolas, monospace"
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: 0

rounded:
  xs: 3px
  sm: 4px
  md: 6px
  lg: 8px
  xl: 12px
  pill: 9999px

spacing:
  xxs: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 24px
  xl: 32px
  xxl: 48px
  shell-sidebar: 248px
  shell-max: 1440px

components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: 9px 13px
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.on-primary}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
  button-secondary:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: 9px 13px
  card:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: 16px
  input:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: 9px 11px
  status-pill:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  app-shell:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xs}"
    padding: 16px
  panel-selected:
    backgroundColor: "{colors.surface-3}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: 16px
  docs-page:
    backgroundColor: "{colors.inverse-canvas}"
    textColor: "{colors.inverse-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xs}"
    padding: 24px
  table-meta:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-subtle}"
    typography: "{typography.mono}"
    rounded: "{rounded.sm}"
    padding: 6px 8px
  active-soft-row:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.md}"
    padding: 8px 10px
  divider:
    backgroundColor: "{colors.hairline}"
    textColor: "{colors.ink}"
    typography: "{typography.mono}"
    rounded: "{rounded.xs}"
    height: 1px
  divider-strong:
    backgroundColor: "{colors.hairline-strong}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.mono}"
    rounded: "{rounded.xs}"
    height: 1px
  status-success:
    backgroundColor: "{colors.success}"
    textColor: "{colors.inverse-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  status-warning:
    backgroundColor: "{colors.warning}"
    textColor: "{colors.inverse-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  status-danger:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.inverse-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  status-info:
    backgroundColor: "{colors.info}"
    textColor: "{colors.inverse-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  lane-auto:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.lane-auto}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  lane-claude:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.lane-claude}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  lane-codex:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.lane-codex}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  lane-gemini:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.lane-gemini}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
  lane-local:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.lane-local}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 5px 9px
---

# Homie Framework Design

## Overview

The Homie interface is an operator cockpit, not a marketing site. It should feel calm, technical, and useful under pressure. The main jobs are: show what is running, which lane owns execution, what evidence exists, what needs attention, and what action is safe next.

The visual language is dense but not cramped. It uses dark operational surfaces for runtime work, light documentation surfaces for manuals and public framework pages, restrained color, compact tables, explicit status states, and real product screenshots or live artifact previews when visuals are needed.

Mission Control, runtime dashboards, Cabinet rooms, BrowserOps observers, relay chat, and public framework docs should feel like parts of the same system even when they live in separate repos.

## Colors

Use neutral dark surfaces for operator work. Use `accent` only for primary action, current selection, or a single important chart series. Do not spread accent color across every icon and badge.

- `canvas` is the primary runtime shell background.
- `surface-1`, `surface-2`, and `surface-3` are stacked panels, tables, and selected rows.
- `ink`, `ink-muted`, and `ink-subtle` define text hierarchy.
- `hairline` and `hairline-strong` define structure without heavy shadows.
- `success`, `warning`, `danger`, and `info` are semantic state colors only.
- Lane colors identify runtime selection and execution ownership: auto, claude, codex, gemini, and local.

Public docs may invert to `inverse-canvas` and `inverse-surface`, but should preserve the same accent, spacing, and component rhythm.

## Typography

Use system sans typography for the product shell. Headings should be clear and compact. Do not use oversized hero typography inside dashboards, cards, sidebars, chat panes, or status panels.

Use `mono` only for command names, runtime ids, JSON fields, paths, model names, traces, and terminal-style output. Runtime metadata should align with tabular spacing and be easy to scan.

Do not use negative letter spacing. Keep labels short and literal.

## Layout

Dashboards use a fixed left navigation region, a compact top status bar, and a scrollable main pane. The sidebar width should be near `shell-sidebar`; content should not stretch past `shell-max` without a reason.

Prefer tables, split panes, command rows, event feeds, and status boards over decorative card grids. Repeated cards are acceptable for individual agents, channels, meetings, workflows, or proof packets, but do not place cards inside cards.

Mobile layouts should collapse side navigation into a top segmented control or drawer. Runtime status, active lane, errors, and primary action remain visible without horizontal scrolling.

## Elevation & Depth

Hierarchy comes from surface steps, hairline borders, spacing, and typography. Use shadows sparingly and never as the main structure. Avoid glassmorphism, blurred panels, floating blobs, and decorative gradient backgrounds.

Runtime-critical states should be obvious through labels plus semantic color. Do not rely on color alone.

## Shapes

Corners are restrained. Cards and panels should usually use `lg` or smaller. Buttons and inputs use `md`. Pills are for status, lane, scope, and filters only.

Avoid oversized rounded rectangles with text when a known icon button would work better. Use icons for actions like refresh, retry, expand, copy, download, inspect, and stop, with accessible labels or tooltips.

## Components

Core product surfaces should include:

- Runtime lane selector with lane-first labels.
- Status pills for live, stale, error, queued, running, blocked, and verified.
- Evidence rows that separate local/test proof from live/runtime proof.
- Dense data tables with sticky headers, sorted columns, and empty/error/loading states.
- Chat transcript panes with channel, session, actor, timestamp, and tool-call metadata.
- Cabinet and Team Room boards with roster, vote, confidence, interrupt register, and synthesis.
- BrowserOps observer panels that clearly indicate read-only state and current CDP session.
- Command palettes and slash-command menus that show direct integrations before browser fallback.
- Artifact previews with source file path, generated time, export actions, and validation status.

Buttons should use icons where the command is familiar. Text buttons are reserved for explicit commands like "Run doctor", "Open artifact", "Export HTML", or "Restart runtime".

## Do's and Don'ts

Do:

- Preserve lane-first language in UI: lane, provider, model, cost, tool calls, execution time.
- Show the actual data flow and proof state instead of only HTTP health.
- Keep operator pages quiet, compact, and scannable.
- Use real screenshots, live previews, or generated artifacts that reveal the product state.
- Keep direct integrations visually distinct from browser/UI fallback actions.
- Make every generated design pass a responsive desktop and mobile sanity check.

Don't:

- Reintroduce provider-first wording on runtime surfaces.
- Hide runtime metadata behind marketing copy or decorative layout.
- Use purple-blue gradient hero sections, floating orb backgrounds, bokeh, or generic AI dashboards.
- Invent metrics, proof claims, customer counts, or integration status.
- Copy third-party brand identities, proprietary typefaces, or exact signature palettes from design catalogs.
- Put secret, account, memory, OAuth, or tenant-specific details in exportable design docs.
