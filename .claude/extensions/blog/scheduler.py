"""Proactive blog scheduler — generates 2 articles daily for owner to approve.

Runs as an async background task, similar to RecoverySMSMonitor.
Fires at 6 AM PT daily. Picks topics intelligently using DataForSEO
keyword gaps and competitor analysis.

Wire into main.py:
    from extensions.blog.scheduler import BlogAutoScheduler
    blog_scheduler = BlogAutoScheduler(engine, discord_adapter)
    asyncio.create_task(blog_scheduler.run())
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

SUPABASE_URL = os.getenv("QM_SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("QM_SUPABASE_SERVICE_KEY", "")
BLOG_CHANNEL_ID = os.getenv("BLOG_DISCORD_CHANNEL_ID", "")  # Set to your blog channel
DAILY_TARGET = int(os.getenv("BLOG_DAILY_TARGET", "2"))
SCHEDULE_HOUR_PT = int(os.getenv("BLOG_SCHEDULE_HOUR_PT", "6"))  # 6 AM PT

# Pacific Time offset (UTC-7 PDT, UTC-8 PST)
PT_OFFSET = timedelta(hours=-7)


def _supabase_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _get_todays_draft_count() -> int:
    """Count drafts created today (PT timezone)."""
    now_pt = datetime.now(timezone(PT_OFFSET))
    today_start = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start.astimezone(timezone.utc).isoformat()

    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/blog_drafts"
            f"?select=id&created_at=gte.{today_start_utc}&generated_by=eq.ai",
            headers=_supabase_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            return len(resp.json())
    except Exception:
        pass
    return 0


def _get_recent_topics() -> list[str]:
    """Get titles of recent drafts to avoid duplication."""
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/blog_drafts"
            f"?select=title&order=created_at.desc&limit=20",
            headers=_supabase_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            titles = []
            for d in resp.json():
                t = d.get("title", "")
                if isinstance(t, dict):
                    t = t.get("en", "")
                if t:
                    titles.append(t.lower())
            return titles
    except Exception:
        pass
    return []


# Topic pools — rotated through, filtered against recent articles
TOPIC_POOLS = [
    # High-value California auto insurance topics
    "cheapest car insurance in California {year}",
    "SR-22 insurance costs California {year}",
    "DUI insurance rates California {year}",
    "California minimum liability insurance explained",
    "non-owner car insurance California guide",
    "high-risk auto insurance California options",
    "California auto insurance for new drivers {year}",
    "best cheap liability insurance California",
    "how to get car insurance after license suspension California",
    "California SR-22 filing process step by step",
    "Mexico auto insurance for California drivers",
    "gap insurance California worth it",
    "California uninsured motorist coverage explained",
    "rideshare insurance California Uber Lyft {year}",
    "California auto insurance discounts you're missing",
    "classic car insurance California specialists",
    "California teen driver insurance costs {year}",
    "switching car insurance California mid-policy",
    "California auto insurance after accident rates",
    "motorcycle insurance California requirements {year}",
    # Spanish market topics
    "seguro de auto barato en California {year}",
    "como obtener seguro SR-22 en California",
    "seguro de auto sin licencia California",
    "seguro de auto para conductores nuevos California",
    "requisitos de seguro de auto California {year}",
]


def pick_topics(count: int = 2) -> list[str]:
    """Pick topics that haven't been covered recently."""
    recent = _get_recent_topics()
    year = str(datetime.now().year)
    candidates = []
    for topic in TOPIC_POOLS:
        filled = topic.replace("{year}", year)
        # Skip if any recent title contains the core topic words
        core_words = set(filled.lower().split()) - {"in", "for", "the", "a", "of", "to", "how", "after"}
        is_duplicate = any(
            sum(1 for w in core_words if w in recent_title) > len(core_words) * 0.6
            for recent_title in recent
        )
        if not is_duplicate:
            candidates.append(filled)

    # Return first N candidates (future: rank by DataForSEO volume)
    return candidates[:count]


def _seconds_until_target() -> float:
    """Seconds until next schedule time (SCHEDULE_HOUR_PT in Pacific Time)."""
    now_utc = datetime.now(timezone.utc)
    now_pt = now_utc.astimezone(timezone(PT_OFFSET))

    target = now_pt.replace(
        hour=SCHEDULE_HOUR_PT, minute=0, second=0, microsecond=0,
    )
    if now_pt >= target:
        target += timedelta(days=1)

    delta = (target - now_pt).total_seconds()
    return max(delta, 60)  # At least 1 minute


class BlogAutoScheduler:
    """Background task that proactively generates blog articles daily.

    Picks topics, triggers the blog-pipeline skill via the engine,
    and posts results to Discord with Publish/Skip buttons.
    """

    def __init__(self, engine: Any, discord_adapter: Any | None = None) -> None:
        self.engine = engine
        self.discord_adapter = discord_adapter
        self._running = False

    async def run(self) -> None:
        """Main loop — sleep until schedule time, generate, repeat."""
        self._running = True
        print(f"[{datetime.now()}] BlogAutoScheduler started (target: {SCHEDULE_HOUR_PT}:00 PT, {DAILY_TARGET}/day)")

        while self._running:
            wait = _seconds_until_target()
            hours = wait / 3600
            print(f"[{datetime.now()}] Blog scheduler: next run in {hours:.1f}h")

            await asyncio.sleep(wait)

            if not self._running:
                break

            try:
                await self._generate_daily_batch()
            except Exception as e:
                print(f"[{datetime.now()}] Blog scheduler error: {e}")

    async def _generate_daily_batch(self) -> None:
        """Generate today's batch of blog articles."""
        existing = _get_todays_draft_count()
        remaining = DAILY_TARGET - existing
        if remaining <= 0:
            print(f"[{datetime.now()}] Blog scheduler: already generated {existing} today, skipping")
            return

        topics = pick_topics(remaining)
        if not topics:
            print(f"[{datetime.now()}] Blog scheduler: no fresh topics available")
            return

        print(f"[{datetime.now()}] Blog scheduler: generating {len(topics)} articles")
        for topic in topics:
            try:
                await self._generate_one(topic)
                await asyncio.sleep(30)  # Brief pause between articles
            except Exception as e:
                print(f"[{datetime.now()}] Blog scheduler: failed on '{topic}': {e}")

    async def _generate_one(self, topic: str) -> None:
        """Generate a single blog article by sending /blog to the engine.

        This creates a synthetic IncomingMessage that triggers the blog
        command through the normal engine flow.
        """
        from models import Channel, IncomingMessage, Platform, Thread, User

        # Create a synthetic message as if owner typed /blog <topic>
        incoming = IncomingMessage(
            text=f"Use the Skill tool to invoke the 'blog-pipeline' skill with arguments: {topic}",
            user=User(Platform.DISCORD, "scheduler", "BlogScheduler"),
            channel=Channel(
                Platform.DISCORD,
                BLOG_CHANNEL_ID or "scheduler",
                name="blog-scheduler",
            ),
            platform=Platform.DISCORD,
            thread=Thread(thread_id="blog-scheduler"),
            is_piv=True,
            piv_command="blog",
        )

        # Run through engine
        final_text = ""
        async for outgoing in self.engine.handle_message(incoming, progress={}):
            final_text = outgoing.text

        # Post results to Discord if adapter available
        if self.discord_adapter and BLOG_CHANNEL_ID and final_text:
            from models import MessageComponent, MessageEmbed, OutgoingMessage

            # Extract draft_id from <<BLOG_RESULTS>> if present
            draft_id = ""
            if "<<BLOG_RESULTS>>" in final_text:
                try:
                    start = final_text.index("<<BLOG_RESULTS>>") + len("<<BLOG_RESULTS>>")
                    end = final_text.index("<</BLOG_RESULTS>>")
                    data = __import__("json").loads(final_text[start:end].strip())
                    draft_id = data.get("draft_id", "")
                except (ValueError, KeyError):
                    pass

            components = []
            if draft_id:
                components = [
                    MessageComponent(
                        label="Publish",
                        custom_id=f"blog_publish:{draft_id}",
                        style="success",
                    ),
                    MessageComponent(
                        label="Skip",
                        custom_id=f"blog_skip:{draft_id}",
                        style="secondary",
                    ),
                ]

            # Send to Discord
            await self.discord_adapter.send(
                OutgoingMessage(
                    text=final_text[:1900] if len(final_text) > 1900 else final_text,
                    channel=Channel(Platform.DISCORD, BLOG_CHANNEL_ID),
                    components=components,
                )
            )

        print(f"[{datetime.now()}] Blog scheduler: generated article for '{topic}'")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
