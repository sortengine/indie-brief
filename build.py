"""
Indie Brief: builds one daily podcast episode.

Steps:
  1. Read the news feeds listed in SOURCES
  2. Drop stories already covered on a previous day (seen.json)
  3. Ask Claude Haiku to write a short spoken briefing
  4. Turn the script into an MP3 with a free voice (edge-tts)
  5. Rebuild feed.xml so podcast apps see the new episode

Run by GitHub Actions. The API key comes from the ANTHROPIC_API_KEY secret.
"""

import asyncio
import datetime as dt
import email.utils
import html
import json
import os
import re
from pathlib import Path
from xml.sax.saxutils import escape

import anthropic
import edge_tts
import feedparser

# ---------------------------------------------------------------- settings

SITE = "https://sortengine.github.io/indie-brief"
SHOW_TITLE = "Indie Brief"
SHOW_DESCRIPTION = "A daily news briefing for independent film and documentary."
VOICE = "en-GB-RyanNeural"          # try en-GB-SoniaNeural for a female voice
MODEL = "claude-haiku-4-5-20251001"
LOOKBACK_HOURS = 48                  # ignore stories older than this
MAX_PER_SOURCE = 8
KEEP_EPISODES = 14                   # older MP3s are deleted to keep the repo small

SOURCES = {
    "Deadline": "https://deadline.com/feed/",
    "Screen Daily": "https://www.screendaily.com/rss",
    "Realscreen": "https://realscreen.com/feed/",
    "Cineuropa": "https://cineuropa.org/en/rss/",
    "International Documentary Association": "https://www.documentary.org/rss.xml",
    "Modern Times Review": "https://www.moderntimes.review/feed/",
}

ROOT = Path(__file__).parent
EPISODES_DIR = ROOT / "episodes"
SEEN_FILE = ROOT / "seen.json"
EPISODES_FILE = ROOT / "episodes.json"
FEED_FILE = ROOT / "feed.xml"

PROMPT = """You write the script for Indie Brief, a short daily audio news briefing
for people working in UK and European independent film and documentary:
producers, production managers, financiers.

Below are today's candidate stories as JSON. Choose the 4 to 6 most useful
for that audience. Favour documentary, independent film, financing, funds,
commissioning, co-production, festivals and markets, and policy. Skip
celebrity news, box office for studio blockbusters, and TV gossip.

Write a spoken script of about 300 words (roughly two minutes).
Open with "Good morning, this is Indie Brief for {date}." and close with a
one-line sign-off. For each story, say the source name naturally, for
example "Realscreen reports that...". Do not invent facts beyond the story text.

This will be read aloud by a text-to-speech voice, so output plain spoken
text only: no markdown, no asterisks, no hashtags, no bullet points, no
headings, no URLs, no code formatting. Write numbers and abbreviations the
way they should be spoken.

Stories:
{stories}
"""


# ---------------------------------------------------------------- helpers

def clean(text: str) -> str:
    """Strip HTML tags and tidy whitespace from a feed summary."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def fetch_stories(seen: set) -> list:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    stories = []
    for name, url in SOURCES.items():
        feed = feedparser.parse(url, agent="Mozilla/5.0 (indie-brief podcast bot)")
        if not feed.entries:
            print(f"  ! {name}: no stories (feed failed or empty), skipping")
            continue
        count = 0
        for entry in feed.entries:
            link = entry.get("link", "")
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                when = dt.datetime(*published[:6], tzinfo=dt.timezone.utc)
                if when < cutoff:
                    continue
            if not link or link in seen:
                continue
            stories.append({
                "source": name,
                "title": clean(entry.get("title", "")),
                "summary": clean(entry.get("summary", ""))[:600],
                "link": link,
            })
            count += 1
            if count >= MAX_PER_SOURCE:
                break
        print(f"  {name}: {count} new stories")
    return stories


def write_script(stories: list, date_spoken: str) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY automatically
    stories_for_ai = [{k: s[k] for k in ("source", "title", "summary")} for s in stories]
    message = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        messages=[{
            "role": "user",
            # json.dumps safely handles quotes and line breaks in the news text
            "content": PROMPT.format(date=date_spoken,
                                     stories=json.dumps(stories_for_ai, indent=1)),
        }],
    )
    script = message.content[0].text
    # Safety net in case any formatting slips through
    script = re.sub(r"[*#`_>]", "", script)
    return script.strip()


def build_feed(episodes: list) -> str:
    items = []
    for ep in episodes:
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['description'])}</description>
      <pubDate>{ep['pubDate']}</pubDate>
      <guid isPermaLink="false">{ep['guid']}</guid>
      <enclosure url="{SITE}/{ep['file']}" length="{ep['size']}" type="audio/mpeg"/>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{SHOW_TITLE}</title>
    <link>{SITE}/</link>
    <description>{SHOW_DESCRIPTION}</description>
    <language>en-gb</language>
    <itunes:author>Sort Engine</itunes:author>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
"""


# ---------------------------------------------------------------- main

def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    date_spoken = now.strftime("%A %-d %B")

    # seen.json maps each story link to the day it was first used.
    # Only stories from EARLIER days are skipped, so re-running on the
    # same day rebuilds the episode from the same stories.
    seen_map = load_json(SEEN_FILE, {})
    if isinstance(seen_map, list):   # old format from the first version
        seen_map = {}
    seen = {link for link, day in seen_map.items() if day != today}
    episodes = load_json(EPISODES_FILE, [])

    print("Fetching news...")
    stories = fetch_stories(seen)

    if stories:
        print(f"Writing script from {len(stories)} stories...")
        script = write_script(stories, date_spoken)
    else:
        script = (f"Good morning, this is Indie Brief for {date_spoken}. "
                  "It's a quiet one: no new stories from our sources since yesterday. "
                  "Back tomorrow.")
    print("\n--- SCRIPT ---\n" + script + "\n--------------\n")

    print("Recording audio...")
    EPISODES_DIR.mkdir(exist_ok=True)
    mp3_path = EPISODES_DIR / f"{today}.mp3"
    asyncio.run(edge_tts.Communicate(script, VOICE).save(str(mp3_path)))

    # Replace any earlier episode from today (e.g. a re-run), newest first
    episodes = [e for e in episodes if e["guid"] != f"indie-brief-{today}"]
    episodes.insert(0, {
        "guid": f"indie-brief-{today}",
        "title": f"Indie Brief, {date_spoken}",
        "description": script[:400],
        "pubDate": email.utils.format_datetime(now),
        "file": f"episodes/{today}.mp3",
        "size": mp3_path.stat().st_size,
    })

    # Delete old episodes beyond the keep limit
    for old in episodes[KEEP_EPISODES:]:
        (ROOT / old["file"]).unlink(missing_ok=True)
    episodes = episodes[:KEEP_EPISODES]

    # Remember covered stories; forget anything older than a week
    for s in stories:
        seen_map.setdefault(s["link"], today)
    week_ago = (now - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    seen_map = {link: day for link, day in seen_map.items() if day >= week_ago}
    SEEN_FILE.write_text(json.dumps(seen_map, indent=1))
    EPISODES_FILE.write_text(json.dumps(episodes, indent=1))
    FEED_FILE.write_text(build_feed(episodes))
    (ROOT / "latest-script.txt").write_text(script)
    print("Done.")


if __name__ == "__main__":
    main()
