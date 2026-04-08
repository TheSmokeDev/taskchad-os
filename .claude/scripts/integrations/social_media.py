"""Unified social media posting for X, Facebook, Instagram, LinkedIn.

Each platform checks for API keys first (fast, reliable), falls back to
a "not configured" message with setup instructions. No browser automation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()


@dataclass
class PostResult:
    platform: str
    success: bool
    message: str
    post_url: str = ""


# ── Account info ─────────────────────────────────────────────────

ACCOUNTS = {
    "x": {
        "name": "X (Twitter)",
        "handle": os.getenv("X_HANDLE", "@yourbrand"),
        "url": os.getenv("X_URL", ""),
        "has_api": bool(os.getenv("X_API_KEY")),
    },
    "facebook": {
        "name": "Facebook",
        "page": os.getenv("FACEBOOK_PAGE_NAME", "Your Business"),
        "url": os.getenv("FACEBOOK_URL", ""),
        "has_api": bool(os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")),
    },
    "instagram": {
        "name": "Instagram",
        "handle": os.getenv("INSTAGRAM_HANDLE", "@yourbrand"),
        "url": os.getenv("INSTAGRAM_URL", ""),
        "has_api": bool(os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")),
    },
    "linkedin": {
        "name": "LinkedIn",
        "page": os.getenv("LINKEDIN_PAGE_NAME", "Your Business"),
        "url": os.getenv("LINKEDIN_URL", ""),
        "has_api": bool(os.getenv("LINKEDIN_ACCESS_TOKEN")),
    },
    "yelp": {
        "name": "Yelp",
        "page": os.getenv("YELP_PAGE_NAME", "Your Business"),
        "url": os.getenv("YELP_URL", ""),
        "has_api": False,
    },
    "bbb": {
        "name": "BBB",
        "page": os.getenv("BBB_PAGE_NAME", "Your Business"),
        "url": os.getenv("BBB_URL", ""),
        "has_api": False,
    },
    "wallethub": {
        "name": "WalletHub",
        "page": os.getenv("WALLETHUB_PAGE_NAME", "Your Business"),
        "url": os.getenv("WALLETHUB_URL", ""),
        "has_api": False,
    },
}


def get_accounts_status() -> str:
    """Return a formatted overview of all social media accounts."""
    lines = ["*Social Media Accounts*\n"]

    postable = ("x", "facebook", "instagram", "linkedin")
    listings = ("yelp", "bbb", "wallethub")

    lines.append("*Posting (via browser):*")
    for key in postable:
        acct = ACCOUNTS[key]
        handle = acct.get("handle", acct.get("page", ""))
        has_creds = bool(os.getenv(f"{key.upper()}_PASSWORD", os.getenv(f"{key.upper()}_EMAIL", "")))
        if key == "x":
            has_creds = bool(os.getenv("X_USERNAME"))
        status = "ready" if has_creds else "no credentials"
        lines.append(f"  *{acct['name']}*: {handle} [{status}]")

    lines.append("\n*Business Listings (read-only):*")
    for key in listings:
        acct = ACCOUNTS[key]
        page = acct.get("page", "")
        lines.append(f"  *{acct['name']}*: {page}")
        if acct.get("url"):
            lines.append(f"    {acct['url']}")

    lines.append(f"\n*Business Contact:*")
    lines.append(f"  Phone: {os.getenv('BUSINESS_PHONE', 'N/A')}")
    lines.append(f"  Email: {os.getenv('BUSINESS_EMAIL', 'N/A')}")
    lines.append(f"  Address: {os.getenv('BUSINESS_ADDRESS', 'N/A')}")

    lines.append("\nUse /post <platform> <content> to post via browser.")
    lines.append("Platforms: x, facebook, instagram, linkedin")

    return "\n".join(lines)


# ── Posting functions ────────────────────────────────────────────

def post_to_x(text: str) -> PostResult:
    """Post a tweet using Twitter API v2."""
    api_key = os.getenv("X_API_KEY")
    api_secret = os.getenv("X_API_SECRET")
    access_token = os.getenv("X_ACCESS_TOKEN")
    access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        return PostResult(
            platform="X",
            success=False,
            message="API keys not configured. Go to developer.x.com > create app > add X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET to .env",
        )

    try:
        from requests_oauthlib import OAuth1

        auth = OAuth1(api_key, api_secret, access_token, access_token_secret)
        resp = requests.post(
            "https://api.x.com/2/tweets",
            json={"text": text[:280]},
            auth=auth,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        tweet_id = data.get("data", {}).get("id", "")
        return PostResult(
            platform="X",
            success=True,
            message=f"Posted ({len(text[:280])} chars)",
            post_url=(
                f"https://x.com/{os.getenv('X_HANDLE', '').lstrip('@')}/status/{tweet_id}"
                if tweet_id and os.getenv("X_HANDLE", "")
                else ""
            ),
        )
    except ImportError:
        return PostResult(
            platform="X",
            success=False,
            message="Missing requests-oauthlib package. Run: pip install requests-oauthlib",
        )
    except Exception as e:
        return PostResult(platform="X", success=False, message=f"Error: {e}")


def post_to_facebook(text: str) -> PostResult:
    """Post to Facebook Page using Graph API."""
    page_id = os.getenv("FACEBOOK_PAGE_ID")
    access_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")

    if not page_id or not access_token:
        return PostResult(
            platform="Facebook",
            success=False,
            message="API not configured. Go to developers.facebook.com > create app > get Page Access Token > add FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN to .env",
        )

    try:
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{page_id}/feed",
            data={"message": text, "access_token": access_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        post_id = data.get("id", "")
        return PostResult(
            platform="Facebook",
            success=True,
            message="Posted to page",
            post_url=f"https://facebook.com/{post_id}" if post_id else "",
        )
    except Exception as e:
        return PostResult(platform="Facebook", success=False, message=f"Error: {e}")


def post_to_instagram(text: str, image_url: str = "") -> PostResult:
    """Post to Instagram using Meta Graph API.

    Instagram API requires an image_url for feed posts (can't do text-only).
    For text-only, returns instructions.
    """
    account_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    access_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")  # Same token as FB

    if not account_id or not access_token:
        return PostResult(
            platform="Instagram",
            success=False,
            message="API not configured. Need INSTAGRAM_BUSINESS_ACCOUNT_ID + FACEBOOK_PAGE_ACCESS_TOKEN in .env. IG posting requires a Meta Developer App with instagram_content_publish permission.",
        )

    if not image_url:
        return PostResult(
            platform="Instagram",
            success=False,
            message="Instagram API requires an image URL for feed posts. Provide an image URL to post.",
        )

    try:
        # Step 1: Create media container
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{account_id}/media",
            data={
                "image_url": image_url,
                "caption": text,
                "access_token": access_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        container_id = resp.json().get("id")

        # Step 2: Publish the container
        resp2 = requests.post(
            f"https://graph.facebook.com/v19.0/{account_id}/media_publish",
            data={
                "creation_id": container_id,
                "access_token": access_token,
            },
            timeout=15,
        )
        resp2.raise_for_status()
        media_id = resp2.json().get("id", "")

        return PostResult(
            platform="Instagram",
            success=True,
            message="Posted to feed",
            post_url=f"https://instagram.com/p/{media_id}" if media_id else "",
        )
    except Exception as e:
        return PostResult(platform="Instagram", success=False, message=f"Error: {e}")


def post_to_linkedin(text: str) -> PostResult:
    """Post to LinkedIn using the LinkedIn API."""
    access_token = os.getenv("LINKEDIN_ACCESS_TOKEN")

    if not access_token:
        return PostResult(
            platform="LinkedIn",
            success=False,
            message="API not configured. Go to linkedin.com/developers > create app > get OAuth token with w_member_social scope > add LINKEDIN_ACCESS_TOKEN to .env",
        )

    try:
        # Get user URN first
        me_resp = requests.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        me_resp.raise_for_status()
        user_sub = me_resp.json().get("sub", "")

        # Create post
        post_body = {
            "author": f"urn:li:person:{user_sub}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }

        resp = requests.post(
            "https://api.linkedin.com/v2/ugcPosts",
            json=post_body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=15,
        )
        resp.raise_for_status()
        post_id = resp.headers.get("x-restli-id", "")

        return PostResult(
            platform="LinkedIn",
            success=True,
            message="Posted",
            post_url=f"https://linkedin.com/feed/update/{post_id}" if post_id else "",
        )
    except Exception as e:
        return PostResult(platform="LinkedIn", success=False, message=f"Error: {e}")


# ── Multi-platform posting ───────────────────────────────────────

PLATFORM_POSTERS = {
    "x": post_to_x,
    "twitter": post_to_x,
    "facebook": post_to_facebook,
    "fb": post_to_facebook,
    "instagram": post_to_instagram,
    "ig": post_to_instagram,
    "linkedin": post_to_linkedin,
    "li": post_to_linkedin,
}


def post_to_platform(platform: str, text: str, image_url: str = "") -> PostResult:
    """Post to a specific platform."""
    key = platform.lower().strip()
    poster = PLATFORM_POSTERS.get(key)
    if not poster:
        return PostResult(
            platform=platform,
            success=False,
            message=f"Unknown platform. Options: x, facebook, instagram, linkedin",
        )
    if key in ("instagram", "ig") and image_url:
        return post_to_instagram(text, image_url)
    return poster(text)


def post_to_all(text: str) -> list[PostResult]:
    """Post to all configured platforms."""
    results = []
    for platform in ("x", "facebook", "linkedin"):
        results.append(post_to_platform(platform, text))
    # Skip instagram for "all" since it requires an image
    return results


def format_post_results(results: list[PostResult]) -> str:
    """Format posting results for display."""
    lines = ["*Post Results*\n"]
    for r in results:
        status = "OK" if r.success else "FAIL"
        lines.append(f"  *{r.platform}*: [{status}] {r.message}")
        if r.post_url:
            lines.append(f"    {r.post_url}")
    return "\n".join(lines)
