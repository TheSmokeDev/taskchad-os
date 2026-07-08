@echo off
REM The Homie - Social Content Factory daily trigger.
REM
REM Fires the Archon "social-content-factory" workflow per brand channel. The
REM workflow generates copy + media (codex-image-gen images / HyperFrames video)
REM and QUEUES drafts for operator approval. It NEVER posts unattended unless the
REM operator has set HOMIE_SOCIAL_UNATTENDED=true (enforced inside the factory,
REM per-post audited) - default-deny.
REM
REM Runs from the repo root (Archon workflows resolve .archon/ from there).
REM If headless Archon is unavailable, swap each line for the direct factory:
REM   uv run python .claude\scripts\social\content_factory.py instagram --count 1 --media auto
REM (the direct path is proven and needs no Archon runtime.)

cd /d "%~dp0..\.."

echo [social-factory] YourBrand - Instagram
call archon workflow run social-content-factory "channel=instagram count=1 media=auto"

echo [social-factory] YourBrand - Facebook
call archon workflow run social-content-factory "channel=facebook count=1 media=image"

REM YouTube Shorts (vertical video render is minutes/clip) - arm once the
REM render cadence is confirmed:
REM echo [social-factory] YourBrand - YouTube
REM call archon workflow run social-content-factory "channel=youtube count=1 media=video"

echo [social-factory] done - drafts queued; approve in Telegram / dashboard.
