#!/usr/bin/env python3
"""Reddit lead bot. One run = one check; GitHub Actions calls it on a schedule.

Finds new hiring posts in SUBREDDITS that match KEYWORDS, drafts a reply with Claude
(optional) and sends both to Telegram. It never posts, comments or DMs on Reddit.

Environment:
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   Reddit API app (read-only, no password needed).
                                           If unset, falls back to the public RSS feeds, which
                                           Reddit sometimes blocks from cloud servers.
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID     where alerts go
  ANTHROPIC_API_KEY                        optional; without it alerts come with no draft
"""
import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
SUBREDDITS = ["forhire", "hireaprogrammer", "ForHireFreelance"]
KEYWORDS = [
    "website", "web developer", "web dev", "wordpress", "webflow", "react", "landing page",
    "crm", "dashboard", "booking", "portal", "admin panel", "redesign", "frontend", "full stack",
]
MAX_AGE_HOURS = 3
MODEL = "claude-haiku-4-5-20251001"
USER_AGENT = "script:growbotiq-lead-bot:1.0 (personal use)"
SEEN_FILE = HERE / "state" / "seen.json"


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def reddit_token():
    creds = f"{os.environ['REDDIT_CLIENT_ID']}:{os.environ['REDDIT_CLIENT_SECRET']}"
    result = http(
        "https://www.reddit.com/api/v1/access_token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()},
    )
    return result["access_token"]


def fetch_new(subreddit, token):
    listing = http(
        f"https://oauth.reddit.com/r/{subreddit}/new?limit=50&raw_json=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    return [child["data"] for child in listing["data"]["children"]]


def fetch_rss(subreddit):
    """Fallback without API keys. The feed has no flair, so r/ForHireFreelance posts are all
    treated as possible hiring posts and the For Hire ads are filtered out by title."""
    req = urllib.request.Request(
        f"https://www.reddit.com/r/{subreddit}/new/.rss?limit=50", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        root = ET.fromstring(resp.read())
    atom = {"a": "http://www.w3.org/2005/Atom"}
    posts = []
    for entry in root.findall("a:entry", atom):
        link = entry.find("a:link", atom).get("href")
        stamp = entry.findtext("a:updated", "", atom) or entry.findtext("a:published", "", atom)
        posts.append({
            "name": entry.findtext("a:id", "", atom),
            "title": entry.findtext("a:title", "", atom),
            "selftext": re.sub(r"<[^>]+>", " ", entry.findtext("a:content", "", atom)),
            "created_utc": datetime.fromisoformat(stamp).timestamp(),
            "permalink": urllib.parse.urlparse(link).path,
            "assume_hiring": subreddit == "ForHireFreelance",
        })
    return posts


def is_lead(post):
    title = post["title"]
    flair = (post.get("link_flair_text") or "").lower()
    if "for hire" in flair or re.search(r"for\s*hire", title, re.I):
        return None
    if not post.get("assume_hiring") and "hiring" not in flair and not re.match(r"\s*\[hiring\]", title, re.I):
        return None
    text = f"{title} {post.get('selftext', '')}".lower()
    return [k for k in KEYWORDS if k in text] or None


def draft_reply(post):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    system = (
        "You write replies to Reddit hiring posts for the freelancer described below. "
        "Write one reply they can paste as a DM or comment: plain text, under 150 words, no markdown, "
        "no emojis. Open with the poster's specific need, mention only relevant experience from the "
        "profile, give a fixed price or price range and a delivery time, and end with one clear next "
        "step. Never invent experience, team members, clients or numbers that are not in the profile. "
        "If the job is far outside the profile, say so in one line starting with SKIP: instead.\n\n"
        + (HERE / "profile.md").read_text()
    )
    result = http(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": MODEL,
            "max_tokens": 600,
            "system": system,
            "messages": [{"role": "user", "content": f"Title: {post['title']}\n\n{post.get('selftext', '')[:6000]}"}],
        }).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    return result["content"][0]["text"].strip()


def telegram(text):
    http(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data=json.dumps({
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text[:4000],
            "disable_web_page_preview": True,
        }).encode(),
        headers={"content-type": "application/json"},
    )


def check(sub, token, seen, known):
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    for post in reversed(fetch_new(sub, token) if token else fetch_rss(sub)):
        if post["name"] in known:
            continue
        matched = is_lead(post) if post["created_utc"] >= cutoff else None
        if matched:
            age = int((time.time() - post["created_utc"]) / 60)
            telegram(
                f"New lead on r/{sub} ({age} min ago)\n{post['title']}\n"
                f"Matched: {', '.join(matched)}\nhttps://www.reddit.com{post['permalink']}"
            )
            try:
                draft = draft_reply(post)
            except Exception as exc:
                draft = None
                print(f"draft failed: {exc}")
            if draft:
                telegram(draft)
            print(f"alerted: r/{sub} {post['title']}")
        known.add(post["name"])
        seen.append(post["name"])


def main():
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    known = set(seen)
    before = len(seen)
    token = reddit_token() if os.environ.get("REDDIT_CLIENT_ID") else None
    failures = 0
    for sub in SUBREDDITS:
        try:
            check(sub, token, seen, known)
        except Exception as exc:
            print(f"r/{sub}: {exc}")
            failures += 1
    if len(seen) > before:
        SEEN_FILE.parent.mkdir(exist_ok=True)
        SEEN_FILE.write_text(json.dumps(seen[-3000:]))
    if failures:
        raise SystemExit(f"{failures} of {len(SUBREDDITS)} subreddits failed.")


if __name__ == "__main__":
    main()
