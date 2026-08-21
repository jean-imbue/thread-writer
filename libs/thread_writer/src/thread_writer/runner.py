"""Turn Imbue blog posts into social posts and read them in a Typefully-style view.

Threads are stored and viewed **per blog post**. Within a post, a thread is
identified by two independent axes:

* **Voice** -- the persona the copy is written in (normies, researchers, quotes).
* **Format** -- the shape of the output: ``tweet`` (a single standalone tweet),
  ``twitter`` (a thread of tweets, shown as the "Thread" tab), or ``linkedin``
  (a single long-form post).

Each blog post gets its own directory keyed by the last path segment of its
source URL (its "slug"), and every voice+format combination is stored in its
own file inside that directory:

``DATA_DIR/threads/<post_slug>/<voice>.<format>.json``

All files share the same JSON shape (a LinkedIn post is a single-element
``tweets`` array). ``DATA_DIR`` defaults to ``runtime/thread-writer/`` and
honors the ``THREAD_WRITER_DATA_DIR`` env var. The reader view is served
per-post at ``/post/<slug>`` (``/`` defaults to the most recent post that has
any thread). This is a synchronous Flask app served by the threaded Werkzeug
server, proxied at ``/service/thread-writer/``.
"""

import calendar
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, request
from litellm import ModelResponse, completion
from litellm.exceptions import BudgetExceededError
from litellm.types.utils import Choices
from loguru import logger
from openai import OpenAIError
from werkzeug.serving import run_simple
from werkzeug.wrappers import Response as WerkzeugResponse

DATA_DIR = Path(os.environ.get("THREAD_WRITER_DATA_DIR", "runtime/thread-writer"))
PORT = int(os.environ.get("THREAD_WRITER_PORT", "8080"))

# Ordered voice presets shown in the dropdown. A voice is "available" only when
# it has at least one saved format file on disk; otherwise it renders disabled
# with a note, so the UI never implies a voice is ready before its examples are
# gathered.
VOICE_PRESETS = [
    ("normies", "Normies · swyx + imbumans"),
    ("researchers", "Researchers · Ng + LeCun"),
    ("quotes", "Quotes · straight from the post"),
]

# Ordered output formats shown as a tab toggle next to the voice dropdown.
# ``tweet`` renders as a single tweet card; ``twitter`` (labelled "Thread")
# renders as a thread of cards; ``linkedin`` renders as a single long-form card.
FORMAT_PRESETS = [
    ("tweet", "Tweet"),
    ("twitter", "Thread"),
    ("linkedin", "LinkedIn"),
]

# Where "Publish" sends the user, per format. Publishing never posts
# automatically -- it copies the text and opens the composer so the user
# reviews and sends it themselves.
FORMAT_PUBLISH = {
    "tweet": {"compose_url": "https://x.com/compose/post", "site": "X"},
    "twitter": {"compose_url": "https://x.com/compose/post", "site": "X"},
    "linkedin": {
        "compose_url": "https://www.linkedin.com/feed/?shareActive=true",
        "site": "LinkedIn",
    },
}

# Shown when a chosen voice+format has no saved file yet.
_NOT_GENERATED_HTML = (
    '<div class="empty-format">'
    '<p class="empty-title">Not generated yet</p>'
    '<p class="empty-sub">This voice and format hasn\'t been written yet. '
    "Pick another combination, or generate it first.</p>"
    "</div>"
)

app = Flask("thread_writer", static_folder=None)

_URL_RE = re.compile(r"https?://[^\s]+")
_TWITTER_URL_WEIGHT = 23


def _tweet_char_count(text: str) -> int:
    without_urls = _URL_RE.sub("", text)
    return len(without_urls) + len(_URL_RE.findall(text)) * _TWITTER_URL_WEIGHT


def _render_tweet_body(text: str) -> str:
    parts, last = [], 0
    for m in _URL_RE.finditer(text):
        parts.append(html.escape(text[last : m.start()]))
        safe = html.escape(m.group(0))
        parts.append(f'<a href="{safe}" target="_blank" rel="noopener noreferrer">{safe}</a>')
        last = m.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts).replace("\n", "<br>")


def _post_slug(source_url: str) -> str:
    """Return the storage slug for a post: the last segment of its source URL."""
    return _normalize_url(source_url).rsplit("/", 1)[-1]


def _load_post_threads(slug: str, data_dir: Path = DATA_DIR) -> dict:
    """Return {voice_id: {format_id: thread_dict}} for one post's saved files.

    Files live under ``threads/<slug>/<voice>.<format>.json``. A voice+format
    with no file simply doesn't appear here; the UI renders that combination as
    a small "Not generated yet" state rather than erroring.
    """
    out: dict[str, dict] = {}
    post_dir = data_dir / "threads" / slug
    if not post_dir.is_dir():
        return out
    for voice_id, _label in VOICE_PRESETS:
        for format_id, _flabel in FORMAT_PRESETS:
            path = post_dir / f"{voice_id}.{format_id}.json"
            if path.exists():
                out.setdefault(voice_id, {})[format_id] = json.loads(path.read_text())
    return out


def _list_threaded_slugs(data_dir: Path = DATA_DIR) -> list[str]:
    """Return post slugs that have at least one thread file, newest first.

    "Newest" is by directory modification time so the most recently generated
    post surfaces first (e.g. as the ``/`` default).
    """
    threads_dir = data_dir / "threads"
    if not threads_dir.is_dir():
        return []
    slug_dirs = [d for d in threads_dir.iterdir() if d.is_dir() and any(d.glob("*.json"))]
    slug_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [d.name for d in slug_dirs]


def _default_slug(data_dir: Path = DATA_DIR) -> str | None:
    """Return the slug the ``/`` reader defaults to, or None if nothing exists."""
    slugs = _list_threaded_slugs(data_dir)
    return slugs[0] if slugs else None


def _post_meta_from_threads(threads: dict) -> tuple[str, str]:
    """Return (source_url, source_title) taken from any thread in the post."""
    for formats in threads.values():
        for thread in formats.values():
            return thread.get("source_url", ""), thread.get("source_title", "original post")
    return "", "original post"


# --- Trending links registry -------------------------------------------------
#
# Besides Imbue's own blog posts, a user can paste ANY URL (a trending article,
# a support doc, a competitor's announcement) and generate a thread that
# COMMENTS on it. Those pasted links live in a small registry file so they
# persist across restarts and can be listed in the Schedule view. Each entry is
# keyed by a stable, filesystem-safe slug derived from the URL; the slug is also
# the storage key under ``threads/<slug>/`` and the ``/post/<slug>`` route.

TRENDING_FILENAME = "trending.json"
# Cap the derived slug so a very long path segment can't produce an unwieldy
# directory name; collision suffixes (-2, -3) may push slightly past this.
_TRENDING_SLUG_MAX_CHARS = 60


def _url_host(url: str) -> str:
    """Return the bare host of a URL (no port, no userinfo, no leading www.)."""
    host = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _derive_trending_slug(url: str, existing: dict) -> str:
    """Return a stable, filesystem-safe slug for a pasted trending URL.

    The slug is the sanitized host plus the last meaningful path segment,
    lowercased with runs of non-alphanumerics collapsed to a single dash and
    truncated to ~60 chars. If the resulting slug already exists in ``existing``
    for a DIFFERENT url, a numeric suffix (-2, -3, ...) is appended so distinct
    URLs never share a slug; re-adding the SAME url reuses its existing slug.
    """
    parsed = urlsplit(url)
    host = _url_host(url)
    segments = [seg for seg in parsed.path.split("/") if seg]
    last_segment = segments[-1] if segments else ""
    raw = f"{host}-{last_segment}" if last_segment else host
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    slug = slug[:_TRENDING_SLUG_MAX_CHARS].strip("-") or "link"
    base = slug
    suffix = 2
    while slug in existing and existing[slug].get("url") != url:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _trending_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / TRENDING_FILENAME


def _load_trending(data_dir: Path = DATA_DIR) -> dict:
    """Return the persisted trending-links registry, or {} if none/garbage."""
    path = _trending_path(data_dir)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_trending(registry: dict, data_dir: Path = DATA_DIR) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _trending_path(data_dir).write_text(json.dumps(registry, indent=2, sort_keys=True))


def _trending_entries_newest_first(registry: dict) -> list[tuple[str, dict]]:
    """Return (slug, entry) pairs sorted by added date, newest first.

    Within a single date the most recently inserted entry comes first, so the
    Schedule view surfaces the freshest links at the top.
    """
    entries = list(registry.items())
    entries.reverse()  # newest-inserted first within an equal added date
    entries.sort(key=lambda kv: kv[1].get("added", ""), reverse=True)
    return entries


# --- Notes drafts registry ---------------------------------------------------
#
# A user can also paste freeform bullet points and get a drafted post written
# straight from them (no fetched web page). Those pasted notes live in their own
# registry file, mirroring the trending registry, so drafts persist across
# restarts and can be listed in the Schedule view. Each entry is keyed by a
# stable, filesystem-safe slug derived from the draft's title (its first line);
# the slug is also the storage key under ``threads/<slug>/`` and the
# ``/post/<slug>`` route.

DRAFTS_FILENAME = "drafts.json"
# Cap the derived title/slug so a very long first line can't produce an
# unwieldy heading or directory name; collision suffixes (-2, -3) may push the
# slug slightly past this.
_DRAFT_TITLE_MAX_CHARS = 60
_DRAFT_SLUG_MAX_CHARS = 60
# Leading list markers stripped from the first line when deriving the title so a
# bullet like "- Launching X" yields the title "Launching X".
_DRAFT_BULLET_PREFIX = "-*•‣◦ \t"


def _draft_title_from_notes(notes: str) -> str:
    """Return a short title from the first non-empty line of freeform notes.

    Leading list markers and whitespace are stripped, and the result is trimmed
    to ~60 chars. Returns "" when the notes have no non-empty line.
    """
    for line in notes.splitlines():
        stripped = line.strip().lstrip(_DRAFT_BULLET_PREFIX).strip()
        if stripped:
            return stripped[:_DRAFT_TITLE_MAX_CHARS].strip()
    return ""


def _derive_draft_slug(title: str, existing: dict) -> str:
    """Return a stable, filesystem-safe slug for a notes draft, from its title.

    The title is lowercased with runs of non-alphanumerics collapsed to a single
    dash and truncated to ~60 chars. Every "Draft it" makes a fresh entry, so any
    collision with an existing slug gets a numeric suffix (-2, -3, ...); distinct
    drafts never share a slug or clobber one another.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:_DRAFT_SLUG_MAX_CHARS].strip("-") or "draft"
    base = slug
    suffix = 2
    while slug in existing:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _drafts_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / DRAFTS_FILENAME


def _load_drafts(data_dir: Path = DATA_DIR) -> dict:
    """Return the persisted notes-drafts registry, or {} if none/garbage."""
    path = _drafts_path(data_dir)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_drafts(registry: dict, data_dir: Path = DATA_DIR) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _drafts_path(data_dir).write_text(json.dumps(registry, indent=2, sort_keys=True))


def _drafts_entries_newest_first(registry: dict) -> list[tuple[str, dict]]:
    """Return (slug, entry) pairs sorted by added date, newest first.

    Within a single date the most recently inserted entry comes first, so the
    Schedule view surfaces the freshest drafts at the top.
    """
    entries = list(registry.items())
    entries.reverse()  # newest-inserted first within an equal added date
    entries.sort(key=lambda kv: kv[1].get("added", ""), reverse=True)
    return entries


BLOG_URL = "https://imbue.com/blog"
# Roughly one posted thread per week is the target cadence.
CADENCE_DAYS = 7
# The blog listing rarely changes within a session; cache it briefly so the
# calendar view stays snappy without hammering imbue.com on every request.
_BLOG_CACHE_TTL_SECONDS = 900
_blog_cache: dict = {"fetched_at": 0.0, "posts": None}


def _normalize_url(url: str) -> str:
    """Canonicalize a blog URL for matching (absolute, no trailing slash)."""
    absolute = urljoin(BLOG_URL + "/", url)
    return absolute.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _parse_blog_posts(html_text: str) -> list[dict]:
    """Extract {title, url, date, category} for every post on the blog index."""
    soup = BeautifulSoup(html_text, "html.parser")
    posts: dict[str, dict] = {}
    for anchor in soup.select("a[href*='/blog/']"):
        href = anchor.get("href")
        if not isinstance(href, str) or href.rstrip("/") in ("/blog", BLOG_URL):
            continue
        date_el = anchor.select_one("div.font-mono")
        title_el = anchor.select_one("div.font-display")
        if date_el is None or title_el is None:
            continue
        try:
            when = datetime.strptime(date_el.get_text(strip=True), "%d %b %Y")
        except ValueError:
            continue
        title = " ".join(title_el.get_text(" ", strip=True).split())
        row = date_el.parent
        meta_divs = row.find_all("div", recursive=False) if row is not None else []
        category = meta_divs[-1].get_text(strip=True) if len(meta_divs) >= 3 else ""
        url = _normalize_url(href)
        # De-dupe by URL; keep the first (listing order) occurrence.
        posts.setdefault(url, {"title": title, "url": url, "date": when, "category": category})
    return sorted(posts.values(), key=lambda p: p["date"], reverse=True)


def _fetch_blog_posts() -> list[dict]:
    """Return parsed blog posts, using a short-lived in-memory cache."""
    now = time.monotonic()
    cached = _blog_cache["posts"]
    if cached is not None and now - _blog_cache["fetched_at"] < _BLOG_CACHE_TTL_SECONDS:
        return cached
    response = httpx.get(BLOG_URL, follow_redirects=True, timeout=30)
    response.raise_for_status()
    posts = _parse_blog_posts(response.text)
    _blog_cache["posts"] = posts
    _blog_cache["fetched_at"] = now
    return posts


def _threaded_source_urls(data_dir: Path = DATA_DIR) -> set[str]:
    """Normalized source URLs of every blog post that already has a thread.

    Used only for calendar chip styling and stats -- NOT for deciding whether a
    post is due in the queue (that is driven by whether it has been posted).
    """
    urls: set[str] = set()
    threads_dir = data_dir / "threads"
    files = list(threads_dir.glob("*/*.json")) if threads_dir.is_dir() else []
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        source = data.get("source_url")
        if source:
            urls.add(_normalize_url(source))
    return urls


# --- "Up next" scheduling queue ---------------------------------------------
#
# The queue is a flat list of two kinds of work that is currently due:
#   * Blog posts within the recent window that have not been posted/activated
#     yet, or whose last-activated date has aged past the weekly thread cadence.
#     Whether a draft thread has been pre-generated is IRRELEVANT to due-ness --
#     the queue tracks what still needs to be POSTED, not what has been drafted.
#   * YouTube videos due for a re-share ("revive"), measured against a 30-day
#     cadence from their last revived date (or their publish date if never
#     revived).
# User actions (mark-as-done, edit a scheduled date / note) persist to
# ``DATA_DIR/schedule_state.json``, keyed by a stable id (blog: its source_url,
# youtube: the video id). The queue never posts anything -- marking an item
# only records that the user did it, so it drops out of the "due" list.

# One posted thread per week for the blog; a re-share roughly monthly for video.
BLOG_THREAD_CADENCE_DAYS = CADENCE_DAYS
YOUTUBE_REVIVE_CADENCE_DAYS = 30

# Only blog posts published within roughly the last four months are candidates
# for the "Up next" queue. Older un-threaded posts stay off the queue (they'd
# otherwise flood it) but still show in the month-grid calendar below.
BLOG_QUEUE_WINDOW_DAYS = 120

# Spacing between recommended posting dates for the queue. The queue's due items
# are staggered rather than posted all at once: the first item is suggested for
# tomorrow and each subsequent item is pushed this many days further out.
RECOMMEND_SPACING_DAYS = 3

SCHEDULE_STATE_FILENAME = "schedule_state.json"

YOUTUBE_HANDLE_URL = "https://www.youtube.com/@imbue_ai"
_YT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; thread-writer/0.1)"}
# The channel id (UC...) shows up both in the page JSON and the canonical link.
_CHANNEL_ID_RE = re.compile(r'"(?:channelId|externalId)":"(UC[0-9A-Za-z_-]{22})"')
_CANONICAL_RE = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"'
)
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_YT_NS = "{http://www.youtube.com/xml/schemas/2015}"
# The RSS feed returns only the ~15 most recent uploads. That is an acceptable
# pool for a revive queue for now; older videos simply age out of consideration.
_YOUTUBE_CACHE_TTL_SECONDS = 900
_youtube_cache: dict = {"fetched_at": 0.0, "videos": None}


class YouTubeUnavailableError(Exception):
    """Raised when Imbue's YouTube channel or feed can't be read."""


def _resolve_youtube_channel_id() -> str | None:
    """Return Imbue's UC... channel id parsed from its handle page, or None."""
    response = httpx.get(
        YOUTUBE_HANDLE_URL, follow_redirects=True, timeout=30, headers=_YT_HEADERS
    )
    response.raise_for_status()
    for pattern in (_CHANNEL_ID_RE, _CANONICAL_RE):
        match = pattern.search(response.text)
        if match is not None:
            return match.group(1)
    return None


def _parse_youtube_feed(xml_text: str) -> list[dict]:
    """Extract {video_id, title, url, published} for each entry in the RSS feed."""
    root = ET.fromstring(xml_text)
    videos: list[dict] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        vid_el = entry.find(f"{_YT_NS}videoId")
        title_el = entry.find(f"{_ATOM_NS}title")
        pub_el = entry.find(f"{_ATOM_NS}published")
        if (
            vid_el is None
            or vid_el.text is None
            or title_el is None
            or title_el.text is None
            or pub_el is None
            or pub_el.text is None
        ):
            continue
        try:
            published = datetime.fromisoformat(pub_el.text.strip())
        except ValueError:
            continue
        video_id = vid_el.text.strip()
        videos.append(
            {
                "video_id": video_id,
                "title": " ".join(title_el.text.split()),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published": published,
            }
        )
    return videos


def _fetch_youtube_videos() -> list[dict]:
    """Return recent Imbue videos, using a short-lived in-memory cache.

    Raises ``YouTubeUnavailableError`` if the channel id can't be resolved.
    Network failures surface as ``httpx.HTTPError`` and are handled by callers.
    """
    now = time.monotonic()
    cached = _youtube_cache["videos"]
    if cached is not None and now - _youtube_cache["fetched_at"] < _YOUTUBE_CACHE_TTL_SECONDS:
        return cached
    channel_id = _resolve_youtube_channel_id()
    if channel_id is None:
        raise YouTubeUnavailableError("could not resolve Imbue's YouTube channel id")
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = httpx.get(feed_url, timeout=30, headers=_YT_HEADERS)
    response.raise_for_status()
    videos = _parse_youtube_feed(response.text)
    _youtube_cache["videos"] = videos
    _youtube_cache["fetched_at"] = now
    return videos


def _schedule_state_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / SCHEDULE_STATE_FILENAME


def _load_schedule_state(data_dir: Path = DATA_DIR) -> dict:
    """Return the persisted per-item schedule state, or {} if none exists yet."""
    path = _schedule_state_path(data_dir)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_schedule_state(state: dict, data_dir: Path = DATA_DIR) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _schedule_state_path(data_dir).write_text(json.dumps(state, indent=2, sort_keys=True))


def _blank_entry() -> dict:
    return {"last_activated": None, "scheduled_date": None, "note": ""}


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _recommended_post_date(index: int, today: datetime) -> date:
    """Return the suggested posting date for the queue item at ``index``.

    Items are staggered so the backlog isn't posted all at once: item 0 is
    suggested for tomorrow and each later item is pushed ``RECOMMEND_SPACING_DAYS``
    further out (item 1 -> tomorrow+3, item 2 -> tomorrow+6, ...).
    """
    return today.date() + timedelta(days=1 + index * RECOMMEND_SPACING_DAYS)


def _overdue_label(days: int) -> str:
    if days <= 0:
        return "due today"
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} overdue"


def _build_up_next(
    posts: list[dict], videos: list[dict], state: dict, today: datetime
) -> list[dict]:
    """Return the flat list of currently-due queue items (blog first, then video).

    An item is included only when it is due. Due-ness is driven by whether it
    has been POSTED/ACTIVATED (its ``last_activated``), never by whether a draft
    thread exists -- pre-generating a draft must not remove a post from the
    queue. Marking an item done sets its ``last_activated`` to today, which
    brings it back inside cadence and removes it from this list.
    """
    today_d = today.date()
    blog_items: list[dict] = []
    for post in posts:
        # Scope the queue to recently published posts; older ones would flood it.
        if (today_d - post["date"].date()).days > BLOG_QUEUE_WINDOW_DAYS:
            continue
        item_id = post["url"]
        entry = state.get(item_id) or {}
        last = entry.get("last_activated")
        if last and _is_iso_date(last):
            last_d = date.fromisoformat(last)
            days_since = (today_d - last_d).days
            due = days_since > BLOG_THREAD_CADENCE_DAYS
            overdue_days = days_since - BLOG_THREAD_CADENCE_DAYS
            last_display = last_d.strftime("%d %b %Y")
        else:
            # Never posted: always due while inside the window, regardless of
            # whether a draft thread has been pre-generated. Overdue is measured
            # from the post's own publish date so older posts sort to the top.
            due = True
            overdue_days = (today_d - post["date"].date()).days - BLOG_THREAD_CADENCE_DAYS
            last_display = None
        if not due:
            continue
        blog_items.append(
            {
                "kind": "Blog",
                "kind_class": "blog",
                "id": item_id,
                "title": post["title"],
                "url": item_id,
                "slug": _post_slug(item_id),
                "source_url": item_id,
                "last_display": last_display,
                "overdue_days": overdue_days,
                "scheduled_date": entry.get("scheduled_date"),
                "note": entry.get("note") or "",
            }
        )
    # Both source lists arrive newest-first; keep that order so the most recent
    # (most relevant) posts and videos sit at the top of the queue.
    video_items: list[dict] = []
    for video in videos:
        item_id = video["video_id"]
        entry = state.get(item_id) or {}
        last = entry.get("last_activated")
        if last and _is_iso_date(last):
            base_d = date.fromisoformat(last)
            last_display = base_d.strftime("%d %b %Y")
        else:
            base_d = video["published"].date()
            last_display = None
        days_since = (today_d - base_d).days
        if days_since <= YOUTUBE_REVIVE_CADENCE_DAYS:
            continue
        video_items.append(
            {
                "kind": "YouTube",
                "kind_class": "yt",
                "id": item_id,
                "title": video["title"],
                "url": video["url"],
                "slug": None,
                "source_url": None,
                "last_display": last_display,
                "overdue_days": days_since - YOUTUBE_REVIVE_CADENCE_DAYS,
                "scheduled_date": entry.get("scheduled_date"),
                "note": entry.get("note") or "",
            }
        )
    return blog_items + video_items


def _render_thread_cards(thread: dict) -> str:
    tweets = thread.get("tweets", [])
    name = html.escape(thread.get("author_name", "You"))
    handle = html.escape(thread.get("author_handle", "you"))
    initial = html.escape((thread.get("author_name") or "Y")[0].upper())
    total = len(tweets)
    blocks = []
    for i, text in enumerate(tweets):
        count = _tweet_char_count(text)
        body = _render_tweet_body(text)
        is_last = "last" if i == total - 1 else ""
        raw_attr = html.escape(json.dumps(text))
        blocks.append(
            f"""
      <article class="tweet {is_last}">
        <div class="rail"><div class="avatar">{initial}</div><div class="connector"></div></div>
        <div class="card">
          <header class="who"><span class="name">{name}</span><span class="handle">@{handle}</span></header>
          <div class="body">{body}</div>
          <footer class="meta">
            <span class="idx">{i + 1}/{total}</span>
            <span class="count">{count} chars</span>
            <button class="copy" data-text={raw_attr} type="button">Copy</button>
          </footer>
        </div>
      </article>"""
        )
    return "\n".join(blocks)


def _render_page(
    threads: dict,
    slug: str = "",
    post_title: str = "",
    schedule_href: str = "calendar",
    is_post_view: bool = False,
    error_message: str | None = None,
) -> str:
    # Default voice: first voice that has any saved format. Default format:
    # Twitter when available for that voice, else its first available format.
    default_voice = next((vid for vid, _ in VOICE_PRESETS if threads.get(vid)), None)
    default_formats = threads.get(default_voice, {}) if default_voice else {}
    default_format = "twitter" if "twitter" in default_formats else next(iter(default_formats), "twitter")
    # Fall back to the first voice so the dropdown, the JS state, and the Generate
    # button stay consistent even when nothing is saved for the default voice.
    default_voice = default_voice or VOICE_PRESETS[0][0]

    # Every voice is selectable so any voice+format can be generated on demand;
    # combinations with no saved file render the "Not generated yet" state with a
    # Generate button rather than being hidden.
    options = []
    for voice_id, label in VOICE_PRESETS:
        sel = " selected" if voice_id == default_voice else ""
        options.append(f'<option value="{voice_id}"{sel}>{html.escape(label)}</option>')
    options_html = "\n".join(options)

    tabs = []
    for format_id, flabel in FORMAT_PRESETS:
        active = " active" if format_id == default_format else ""
        tabs.append(
            f'<button class="format-tab{active}" role="tab" data-format="{format_id}" '
            f'type="button" onclick="switchFormat(\'{format_id}\')">{html.escape(flabel)}</button>'
        )
    tabs_html = "\n".join(tabs)

    # Pre-render every voice+format's cards + metadata for instant switching.
    # A missing combination is stored as null so the client can show the
    # "Not generated yet" state without a round trip.
    rendered: dict[str, dict] = {}
    for voice_id, _label in VOICE_PRESETS:
        rendered[voice_id] = {}
        formats = threads.get(voice_id, {})
        for format_id, _flabel in FORMAT_PRESETS:
            thread = formats.get(format_id)
            if thread is None:
                rendered[voice_id][format_id] = None
            else:
                rendered[voice_id][format_id] = {
                    "cards": _render_thread_cards(thread),
                    "tweets": thread.get("tweets", []),
                    "source_url": thread.get("source_url", ""),
                    "source_title": thread.get("source_title", "original post"),
                    "model": thread.get("model", ""),
                    "generated_from": thread.get("generated_from", ""),
                }

    default = rendered.get(default_voice, {}).get(default_format) if default_voice else None
    if default is None:
        default = {"cards": _NOT_GENERATED_HTML, "tweets": [], "source_url": "", "source_title": "", "model": "", "generated_from": ""}
        initial_cards = _NOT_GENERATED_HTML
    else:
        initial_cards = default["cards"]

    publish_json = json.dumps(FORMAT_PUBLISH)

    # Navigation to the Schedule/calendar view. A post page (one segment deeper
    # than "/") shows a plain, understated "Back to schedule" link at the very
    # top; the main reader shows a clear "View schedule" button in its controls
    # row. Only one of the two ever renders, so post pages aren't cluttered.
    safe_schedule_href = html.escape(schedule_href)
    if is_post_view:
        back_nav_html = (
            f'<a class="top-back" href="{safe_schedule_href}">'
            "&#8592; Back to schedule</a>"
        )
        controls_schedule_html = ""
    else:
        back_nav_html = ""
        controls_schedule_html = (
            f'<a class="schedule-btn reader-schedule" href="{safe_schedule_href}">'
            "View schedule</a>"
        )

    # A generation failure redirects back here with a short reason; show it as a
    # dismissible-looking banner at the very top of the reader.
    if error_message:
        error_banner_html = (
            f'<div class="error-banner">Generation failed: {html.escape(error_message)}</div>'
        )
    else:
        error_banner_html = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thread writer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#f7f5f2; --surface:#fff; --border:#e8e3db; --text:#1b1a17; --muted:#837c72;
  --accent:#c15f3c; --accent-soft:#f2e4dc; --connector:#e4ded4;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#131211; --surface:#1b1a18; --border:#2b2926; --text:#edeae4; --muted:#9a948a;
    --accent:#e07850; --accent-soft:#2e211b; --connector:#2c2a27;
  }}
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; }}
body {{ background:var(--bg); color:var(--text); font-family:"Inter",system-ui,sans-serif; line-height:1.5; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:640px; margin:0 auto; padding:28px 20px 120px; }}

.topbar {{ display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:10px; flex-wrap:wrap; }}
.topbar h1 {{ font-size:20px; font-weight:700; letter-spacing:-0.02em; margin:0; }}
.origin {{ font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); }}
.origin b {{ color:var(--accent); font-weight:500; }}
.post-title {{ width:100%; margin:2px 0 0; font-size:14px; color:var(--muted); }}
.post-title a {{ color:var(--text); text-decoration:none; font-weight:500; }}
.post-title a:hover {{ color:var(--accent); }}

.controls {{ display:flex; align-items:center; gap:10px; margin:14px 0 24px; flex-wrap:wrap; }}
.voice-field {{ display:flex; align-items:center; gap:8px; }}
.voice-field label {{ font-size:12px; color:var(--muted); font-weight:500; }}
select {{
  font-family:"Inter",sans-serif; font-size:13px; font-weight:500; color:var(--text);
  background:var(--surface); border:1px solid var(--border); border-radius:999px;
  padding:7px 32px 7px 14px; cursor:pointer; appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23837c72' stroke-width='2.5'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat; background-position:right 12px center;
}}
select:hover {{ border-color:var(--accent); }}
.format-field {{ display:flex; align-items:center; gap:8px; }}
.format-field label {{ font-size:12px; color:var(--muted); font-weight:500; }}
.format-tabs {{ display:inline-flex; background:var(--surface); border:1px solid var(--border); border-radius:999px; padding:3px; gap:2px; }}
.format-tab {{ font-family:"Inter",sans-serif; font-size:13px; font-weight:500; color:var(--muted); background:transparent; border:none; border-radius:999px; padding:6px 15px; cursor:pointer; transition:all .15s ease; }}
.format-tab:hover {{ color:var(--accent); }}
.format-tab.active {{ background:var(--accent); color:#fff; }}
.schedule-btn {{ display:inline-flex; align-items:center; gap:6px; font-family:"Inter",sans-serif; font-size:13px; font-weight:600; color:#fff; background:var(--accent); border:1px solid var(--accent); border-radius:999px; padding:8px 16px; text-decoration:none; transition:all .15s ease; }}
.schedule-btn:hover {{ opacity:.9; color:#fff; }}
.schedule-btn.reader-schedule {{ margin-left:auto; }}
.top-back {{ display:inline-flex; align-items:center; gap:6px; font-family:"Inter",sans-serif; font-size:13px; font-weight:600; color:#fff; background:var(--accent); border:1px solid var(--accent); border-radius:999px; padding:8px 16px; text-decoration:none; margin-bottom:18px; transition:opacity .15s ease; }}
.top-back:hover {{ opacity:.9; color:#fff; }}

.empty-format {{ text-align:center; padding:52px 24px; background:var(--surface); border:1px dashed var(--border); border-radius:14px; }}
.empty-title {{ font-size:15px; font-weight:600; color:var(--text); margin:0 0 6px; }}
.empty-sub {{ font-size:13px; color:var(--muted); margin:0 auto 18px; max-width:340px; }}
.gen-form {{ margin:0; }}
.gen-btn {{ font-family:"Inter",sans-serif; font-size:13px; font-weight:600; color:#fff; background:var(--accent); border:1px solid var(--accent); border-radius:999px; padding:9px 20px; cursor:pointer; transition:opacity .15s ease; }}
.gen-btn:hover {{ opacity:.9; }}
.gen-btn:disabled {{ opacity:.6; cursor:default; }}
.regen-form {{ margin:0; }}
.regen-btn {{ font-family:"Inter",sans-serif; font-size:13px; font-weight:500; color:var(--muted); background:transparent; border:1px solid var(--border); border-radius:999px; padding:8px 16px; cursor:pointer; transition:all .15s ease; }}
.regen-btn:hover {{ border-color:var(--accent); color:var(--accent); }}
.regen-btn:disabled {{ opacity:.6; cursor:default; }}
.error-banner {{ background:var(--accent-soft); color:var(--accent); border:1px solid var(--accent); border-radius:12px; padding:12px 16px; margin-bottom:16px; font-size:13.5px; font-weight:500; }}

.thread {{ position:relative; }}
.tweet {{ display:flex; gap:14px; }}
.rail {{ display:flex; flex-direction:column; align-items:center; flex:0 0 auto; }}
.avatar {{ width:40px; height:40px; border-radius:50%; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:16px; flex:0 0 auto; }}
.connector {{ width:2px; flex:1 1 auto; background:var(--connector); margin:6px 0; border-radius:2px; min-height:12px; }}
.tweet.last .connector {{ display:none; }}
.card {{ flex:1 1 auto; min-width:0; background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:14px 16px 10px; margin-bottom:14px; transition:border-color .15s ease; }}
.card:hover {{ border-color:var(--accent); }}
.who {{ display:flex; align-items:baseline; gap:6px; margin-bottom:4px; }}
.who .name {{ font-weight:600; font-size:15px; }}
.who .handle {{ color:var(--muted); font-size:14px; }}
.body {{ font-size:15.5px; word-wrap:break-word; }}
.body a {{ color:var(--accent); text-decoration:none; }}
.body a:hover {{ text-decoration:underline; }}
.meta {{ display:flex; align-items:center; gap:14px; margin-top:10px; padding-top:8px; border-top:1px solid var(--border); font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); }}
.meta .count {{ margin-left:auto; }}
.copy {{ font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); background:transparent; border:1px solid var(--border); border-radius:999px; padding:3px 10px; cursor:pointer; transition:all .15s ease; }}
.copy:hover, .copy.done {{ color:var(--accent); border-color:var(--accent); }}
.copy.done {{ background:var(--accent-soft); }}

.footer {{ margin-top:26px; padding-top:18px; border-top:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }}
.source {{ color:var(--muted); font-size:13px; text-decoration:none; }}
.source:hover {{ color:var(--accent); }}
.actions {{ display:flex; gap:10px; }}
.btn {{ font-family:"Inter",sans-serif; font-size:13px; font-weight:500; border-radius:999px; padding:8px 16px; cursor:pointer; border:1px solid var(--border); background:var(--surface); color:var(--text); transition:all .15s ease; }}
.btn:hover {{ border-color:var(--accent); color:var(--accent); }}
.btn.primary {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
.btn.primary:hover {{ opacity:.9; color:#fff; }}
.btn.done {{ background:var(--muted); border-color:var(--muted); color:#fff; }}
.note {{ font-size:11px; color:var(--muted); margin-top:8px; text-align:right; font-family:"JetBrains Mono",monospace; }}
:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
</style>
</head>
<body>
  <main class="wrap">
    {back_nav_html}
    {error_banner_html}
    <div class="topbar">
      <h1>Thread writer</h1>
      <div class="post-title">Post: <a href="{html.escape(default["source_url"])}" target="_blank" rel="noopener noreferrer">{html.escape(post_title or default["source_title"] or "original post")} &#8599;</a></div>
    </div>
    <div class="controls">
      <div class="voice-field">
        <label for="voice">Voice</label>
        <select id="voice" onchange="switchVoice(this.value)">
{options_html}
        </select>
      </div>
      <div class="format-field">
        <label>Format</label>
        <div class="format-tabs" id="format-tabs" role="tablist">
{tabs_html}
        </div>
      </div>
      {controls_schedule_html}
    </div>

    <div class="thread" id="thread">
{initial_cards}
    </div>

    <div class="footer">
      <a class="source" id="source" href="{html.escape(default["source_url"])}" target="_blank" rel="noopener noreferrer">{html.escape(default["source_title"])} &#8599;</a>
      <div class="actions">
        <form class="regen-form" id="regen-form" method="post" action="/generate" onsubmit="return onGenerateSubmit(this)">
          <input type="hidden" name="slug" value="{html.escape(slug)}">
          <input type="hidden" name="voice" id="regen-voice" value="">
          <input type="hidden" name="format" id="regen-format" value="">
          <button class="regen-btn" id="regen-btn" type="submit">Regenerate</button>
        </form>
        <button class="btn" id="copy-all" type="button" onclick="copyAll(this)">Copy thread</button>
        <button class="btn primary" id="publish" type="button" onclick="publish(this)">Publish</button>
      </div>
    </div>
    <div class="note" id="note">Publishing never posts automatically. It copies the thread and opens X so you review and send it yourself.</div>
  </main>
<script>
  const DATA = {json.dumps(rendered)};
  const PUBLISH = {publish_json};
  const SLUG = {json.dumps(slug)};
  const GENERATING_LABEL = 'Generating\\u2026 this can take up to ~30 seconds';
  let currentVoice = {json.dumps(default_voice)};
  let currentFormat = {json.dumps(default_format)};

  function escapeAttr(value) {{
    return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }}

  // Build the "Not generated yet" card with a Generate form for the current
  // voice+format. Rebuilt on every switch so its hidden fields stay in sync.
  function notGeneratedHtml() {{
    return '<div class="empty-format">'
      + '<p class="empty-title">Not generated yet</p>'
      + '<p class="empty-sub">This voice and format hasn\\'t been written yet. '
      + 'Generate it from the post, or pick another combination.</p>'
      + '<form class="gen-form" method="post" action="/generate" onsubmit="return onGenerateSubmit(this)">'
      + '<input type="hidden" name="slug" value="' + escapeAttr(SLUG) + '">'
      + '<input type="hidden" name="voice" value="' + escapeAttr(currentVoice) + '">'
      + '<input type="hidden" name="format" value="' + escapeAttr(currentFormat) + '">'
      + '<button class="gen-btn" type="submit">Generate this thread</button>'
      + '</form>'
      + '</div>';
  }}

  // Shared submit handler for both Generate and Regenerate: put the button into
  // a disabled "Generating..." state and let the synchronous POST run.
  function onGenerateSubmit(form) {{
    const btn = form.querySelector('button[type="submit"]');
    if (btn) {{ btn.disabled = true; btn.textContent = GENERATING_LABEL; }}
    return true;
  }}

  function bindCopies() {{
    document.querySelectorAll('.copy').forEach(btn => {{
      btn.onclick = () => navigator.clipboard.writeText(btn.dataset.text).then(() => {{
        const prev = btn.textContent; btn.textContent='Copied'; btn.classList.add('done');
        setTimeout(() => {{ btn.textContent=prev; btn.classList.remove('done'); }}, 1200);
      }});
    }});
  }}

  function currentData() {{
    return (DATA[currentVoice] || {{}})[currentFormat] || null;
  }}

  // The unit noun for the current format: a single tweet, a whole thread, or a
  // long-form post. Drives the copy/publish button labels and the safety note.
  function unitLabel() {{
    if (currentFormat === 'linkedin') return 'post';
    if (currentFormat === 'tweet') return 'tweet';
    return 'thread';
  }}

  function render() {{
    document.querySelectorAll('.format-tab').forEach(t => {{
      t.classList.toggle('active', t.dataset.format === currentFormat);
    }});
    const unit = unitLabel();
    const site = (PUBLISH[currentFormat] || {{}}).site || 'the composer';
    const d = currentData();
    const thread = document.getElementById('thread');
    const source = document.getElementById('source');
    const copyBtn = document.getElementById('copy-all');
    const pubBtn = document.getElementById('publish');
    const note = document.getElementById('note');
    const regenForm = document.getElementById('regen-form');
    copyBtn.textContent = 'Copy ' + unit;
    note.textContent = 'Publishing never posts automatically. It copies the ' + unit + ' and opens ' + site + ' so you review and send it yourself.';
    if (!d) {{
      // No saved thread for this combination -- offer to generate it, and hide
      // the publish/copy/regenerate controls that only apply to a real thread.
      thread.innerHTML = notGeneratedHtml();
      source.style.display = 'none';
      regenForm.style.display = 'none';
      copyBtn.disabled = true; pubBtn.disabled = true;
      copyBtn.style.opacity = '0.45'; pubBtn.style.opacity = '0.45';
      return;
    }}
    thread.innerHTML = d.cards;
    source.style.display = '';
    source.href = d.source_url; source.innerHTML = (d.source_title || 'original post') + ' \\u2197';
    // Point the Regenerate form at the current voice+format and show it.
    regenForm.style.display = '';
    document.getElementById('regen-voice').value = currentVoice;
    document.getElementById('regen-format').value = currentFormat;
    copyBtn.disabled = false; pubBtn.disabled = false;
    copyBtn.style.opacity = ''; pubBtn.style.opacity = '';
    bindCopies();
  }}

  function switchVoice(id) {{ currentVoice = id; render(); }}
  function switchFormat(id) {{ currentFormat = id; render(); }}

  render();

  function copyAll(btn) {{
    const d = currentData(); if (!d) return;
    const label = unitLabel();
    navigator.clipboard.writeText(d.tweets.join('\\n\\n')).then(() => {{
      btn.textContent='Copied ' + label; btn.classList.add('done');
      setTimeout(() => {{ btn.textContent = 'Copy ' + unitLabel(); btn.classList.remove('done'); }}, 1400);
    }});
  }}

  function publish(btn) {{
    const d = currentData(); if (!d) return;
    const pub = PUBLISH[currentFormat] || {{compose_url: 'https://x.com/compose/post', site: 'X'}};
    const what = currentFormat === 'twitter' ? ('all ' + d.tweets.length + ' tweets') : ('the ' + unitLabel());
    const ok = confirm('This will NOT post anything automatically.\\n\\nIt copies ' + what + ' to your clipboard and opens ' + pub.site + ' so you can review and send it yourself. Continue?');
    if (!ok) return;
    navigator.clipboard.writeText(d.tweets.join('\\n\\n')).then(() => {{
      btn.textContent='Copied \\u2014 opening ' + pub.site; btn.classList.add('done');
      window.open(pub.compose_url, '_blank', 'noopener');
      setTimeout(() => {{ btn.textContent='Publish'; btn.classList.remove('done'); }}, 2500);
    }});
  }}
</script>
</body>
</html>"""


def _no_threads_response(
    message: str, schedule_href: str = "/calendar", error_message: str | None = None
) -> Response:
    error_html = (
        f"<p style='color:#c15f3c;font-weight:600'>Generation failed: {html.escape(error_message)}</p>"
        if error_message
        else ""
    )
    return Response(
        "<!doctype html><body style='font-family:sans-serif;padding:40px'>"
        f"{error_html}<h1>No threads yet</h1><p>{html.escape(message)}</p>"
        f"<p><a href='{html.escape(schedule_href)}'>Open the Schedule</a></p></body>",
        mimetype="text/html",
    )


@app.route("/")
def index() -> Response:
    """Render the reader for the most recent post that has any thread."""
    slug = _default_slug()
    if slug is None:
        return _no_threads_response("Generate a thread for a blog post first.")
    threads = _load_post_threads(slug)
    _source_url, post_title = _post_meta_from_threads(threads)
    return Response(
        _render_page(
            threads,
            slug=slug,
            post_title=post_title,
            schedule_href="/calendar",
            error_message=request.args.get("error"),
        ),
        mimetype="text/html",
    )


@app.route("/post/<slug>")
def post_view(slug: str) -> Response:
    """Render the reader scoped to a single blog post's threads."""
    threads = _load_post_threads(slug)
    error_message = request.args.get("error")
    if not threads:
        return _no_threads_response(
            "This post doesn't have any generated threads yet.",
            schedule_href="/calendar",
            error_message=error_message,
        )
    _source_url, post_title = _post_meta_from_threads(threads)
    # Served one path segment deeper than "/", so the Schedule link steps up.
    return Response(
        _render_page(
            threads,
            slug=slug,
            post_title=post_title,
            schedule_href="/calendar",
            is_post_view=True,
            error_message=error_message,
        ),
        mimetype="text/html",
    )


# --- On-demand thread generation --------------------------------------------
#
# A user can generate a thread for a post+voice+format that hasn't been written
# yet. We fetch the post live from imbue.com, ground a voice/format-specific
# prompt in that post's own text, ask the model for strict JSON, parse it, and
# save it in the standard per-post file layout. The call is synchronous -- the
# threaded Werkzeug server absorbs the ~10-30s wait -- and every failure mode is
# turned into a redirect back to the reader with a short banner message rather
# than a 500.

_VOICE_LABEL_BY_ID = dict(VOICE_PRESETS)
_FORMAT_LABEL_BY_ID = dict(FORMAT_PRESETS)

# The keyed deployment (ANTHROPIC_API_KEY set) calls the model with litellm
# directly. Fable is the primary writer and is tried twice (it occasionally
# returns an empty reply); if it still fails, generation falls back to Opus.
# The models are routed through the Anthropic-compatible proxy at
# ANTHROPIC_BASE_URL, so they carry the ``anthropic/`` provider prefix. Only
# models this key can actually serve may appear here (no ``claude-sonnet-5`` --
# it is not available on this proxy and every call to it 400s).
_GENERATION_MODELS = (
    "anthropic/claude-fable-5",
    "anthropic/claude-fable-5",
    "anthropic/claude-opus-4-8",
)
_MODEL_LABEL_BY_ID = {
    "anthropic/claude-fable-5": "Fable 5",
    "anthropic/claude-opus-4-8": "Opus 4.8",
}
# litellm surfaces API-side failures (404/429/budget/timeout/auth/etc.) as these;
# every openai-derived error plus the budget guard is treated as retryable.
_MODEL_ERROR_TYPES = (OpenAIError, BudgetExceededError)
_MODEL_MAX_TOKENS = 4000
# Cap the post text fed into the prompt so a very long post can't blow up the
# context; the head of the post carries the thesis we need to ground on.
_MAX_POST_TEXT_CHARS = 12000
_TWEETS_PER_FORMAT = {"tweet": 1, "twitter": 3, "linkedin": 1}

# Per-voice register the copy is written in.
_VOICE_GUIDANCE = {
    "normies": (
        "Write as a real Imbue engineer in the register of swyx and the Imbue "
        "Slack. Be warm, specific, understated, and concrete, with no marketing "
        "gloss."
    ),
    "researchers": (
        "Write measured and precise like Andrew Ng, and be willing to be blunt "
        "like Yann LeCun. Use plain words, stay grounded in mechanism, and keep a "
        "constructive tone."
    ),
    "quotes": (
        "Anchor each part on a quote taken word-for-word from the post text, "
        'wrapped in "quotation marks" and copied as an exact substring, with only '
        "minimal connective wording between the quotes."
    ),
}

# --- Per-voice example corpora (the "1000-shot" style samples) ----------------
#
# Each writing voice is backed by a large corpus of REAL, verbatim writing in
# that register, scraped into markdown bullets under ``DATA_DIR/voices/``. At
# import we parse the example prose out of those bullets once (never per request)
# and assemble one big, static block per voice. That block is injected as a
# CACHED prefix on every generation call for the voice (see ``_complete_generation``),
# so the model imitates the real writing rather than a one-line description, and
# the expensive prefix is billed once and then served from Anthropic's prompt
# cache on subsequent calls. The block must be byte-identical across calls for a
# given voice for the cache to hit, which is why it is built once here.
#
# The ``quotes`` voice has NO corpus: it is verbatim-from-the-post and its
# generation stays on the plain, uncached path exactly as before.
_VOICE_CORPUS_DIR = DATA_DIR / "voices"

# Which corpus files feed each voice's example block, in order. Missing files are
# skipped so a partial or absent corpus never crashes generation -- the voice
# simply falls back to the inline ``_VOICE_GUIDANCE`` on the uncached path.
_VOICE_CORPUS_FILES = {
    "normies": ("normies_examples.md", "swyx.md"),
    "researchers": ("researchers_examples.md", "researchers.md"),
}

# Framing that precedes the raw examples so the model treats the block as a
# style reference, not content to reproduce.
_VOICE_CORPUS_INTRO = (
    "Below is a large sample of real writing in the target voice. Study the "
    "register, rhythm, and diction and write in that same voice. Do NOT copy "
    "their content or topics -- only imitate the style.\n\n"
    "REAL WRITING SAMPLES\n"
)

# A top-level ``## <title>`` section whose title contains this marks the end of
# the verbatim examples (the hand-written style notes that trail each corpus).
_CORPUS_PROFILE_MARKER = "voice profile"
# Strips a leading ``[#channel]`` / ``[url]`` source decoration from a bullet.
_CORPUS_LEAD_DECORATION_RE = re.compile(r"^\[[^\]]*\]\s*")
# Splits off a trailing `` -- <source>`` (em-dash) attribution from a bullet.
_CORPUS_TRAIL_DECORATION_RE = re.compile(r"\s+—\s+")


def _clean_corpus_line(bullet: str) -> str:
    """Return the clean example prose from one ``- ...`` corpus bullet.

    Strips the markdown ``- `` marker, a leading ``[#channel]``/``[url]`` source
    decoration (Slack/HN lines), and a trailing `` -- <url>`` attribution
    (researcher lines). Quoted snippets (swyx/Ng/LeCun) are unwrapped to their
    inner text, dropping any trailing ``(date)`` or source note that follows the
    closing quote.
    """
    text = bullet.strip()
    if text.startswith("- "):
        text = text[2:].strip()
    text = _CORPUS_LEAD_DECORATION_RE.sub("", text).strip()
    if text.startswith('"'):
        close = text.find('"', 1)
        if close != -1:
            return text[1:close].strip()
    return _CORPUS_TRAIL_DECORATION_RE.split(text, maxsplit=1)[0].strip()


def _parse_corpus_examples(path: Path) -> list[str]:
    """Return the verbatim example lines parsed from one corpus markdown file.

    Only bullets inside the file's verbatim ``## `` sections are kept: bullets
    before the first ``## `` section (the source lists) and everything from the
    trailing ``## Voice profile`` section onward (hand-written style notes, not
    examples) are skipped, as are ``- **metadata**`` bullets. Returns [] for a
    missing file so a voice with no corpus degrades gracefully.
    """
    if not path.exists():
        return []
    examples: list[str] = []
    in_examples = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_examples = _CORPUS_PROFILE_MARKER not in line[3:].lower()
            continue
        if line.startswith("#"):
            continue
        if not in_examples or not line.startswith("- ") or line.startswith("- **"):
            continue
        cleaned = _clean_corpus_line(line)
        if cleaned:
            examples.append(cleaned)
    return examples


def _build_voice_corpus_block(voice_id: str, corpus_dir: Path = _VOICE_CORPUS_DIR) -> str:
    """Assemble one voice's full example block (intro + one example per line).

    Returns "" when the voice has no corpus files on disk, which routes the
    voice onto the plain uncached path with only its inline guidance.
    """
    files = _VOICE_CORPUS_FILES.get(voice_id, ())
    examples: list[str] = []
    for name in files:
        examples.extend(_parse_corpus_examples(corpus_dir / name))
    if not examples:
        return ""
    return _VOICE_CORPUS_INTRO + "\n".join(f"- {line}" for line in examples)


# Built once at import so every call for a voice sends a byte-identical prefix
# that Anthropic's prompt cache can hit. ``quotes`` is intentionally absent.
_VOICE_CORPUS_BLOCKS = {
    voice_id: _build_voice_corpus_block(voice_id) for voice_id in _VOICE_CORPUS_FILES
}
for _voice_id, _block in _VOICE_CORPUS_BLOCKS.items():
    if _block:
        logger.info(
            "thread-writer loaded {} voice corpus: {} chars (~{} tokens est.)",
            _voice_id,
            len(_block),
            len(_block) // 4,
        )
    else:
        logger.warning(
            "thread-writer found no corpus files for {} voice; falling back to inline guidance",
            _voice_id,
        )


def _voice_corpus_block(voice_id: str) -> str | None:
    """Return the cached example block for a voice, or None if it has none.

    None routes the voice onto the plain, uncached completion path (used for the
    ``quotes`` voice and as the graceful fallback when a corpus file is absent).
    """
    block = _VOICE_CORPUS_BLOCKS.get(voice_id)
    return block or None

# Per-format shape of the output.
_FORMAT_GUIDANCE = {
    "tweet": (
        "Produce ONE tweet as a single string. Open with the headline announcement, "
        "then add one or two tight supporting sentences that still vary in length, "
        "and end with the link. It can run a little over 280 characters, but keep it "
        "punchy. Full https:// URLs are fine."
    ),
    "twitter": (
        "Produce 3 tight tweets. The first roughly 280 characters of tweet 1 must "
        "land as a strong hook that opens with the headline announcement. Full "
        "https:// URLs are fine."
    ),
    "linkedin": (
        "Produce ONE long-form post as a single string. Separate paragraphs with "
        "\\n\\n, open with a strong standalone first line that is the headline "
        "announcement, and keep it roughly 900 to 1600 characters. Use scheme-less "
        "links (imbue.com/..., github.com/...) so there are no colons anywhere."
    ),
}

# Words the tuned voice never uses.
_BANNED_WORDS = (
    "just",
    "honestly",
    "weirdly",
    "fun",
    "obvious",
    "delve",
    "robust",
    "leverage",
    "utilize",
    "seamless",
    "elevate",
    "unlock",
    "harness",
    "empower",
    "foster",
    "landscape",
    "realm",
    "tapestry",
    "testament",
    "game-changer",
    "cutting-edge",
    "furthermore",
    "moreover",
    "additionally",
)

_CODE_FENCE_OPEN_RE = re.compile(r"^\s*```(?:json)?\s*\n?")
_CODE_FENCE_CLOSE_RE = re.compile(r"\n?\s*```\s*$")


class ThreadWriterError(Exception):
    """Base error for the thread-writer service."""


class ThreadGenerationError(ThreadWriterError):
    """Raised when a thread can't be generated (fetch, model, or parse failure).

    The message is user-facing -- it is rendered verbatim in the reader's error
    banner -- so keep it short and free of internal detail.
    """


def _fetch_post_content(url: str) -> dict:
    """Fetch a blog post and return its ``{title, text, links}``.

    ``links`` are outbound, non-imbue.com URLs found in the post body (repos,
    downloads, references) so the copy can cite them. Nav/footer/script chrome
    is stripped before the body text is read.
    """
    try:
        response = httpx.get(url, follow_redirects=True, timeout=30)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ThreadGenerationError(f"couldn't reach the post at {url}") from error

    soup = BeautifulSoup(response.text, "html.parser")

    # Title: the post's <h1>, falling back to the document <title>.
    heading = soup.select_one("h1") or soup.select_one("title")
    title = " ".join(heading.get_text(" ", strip=True).split()) if heading is not None else ""

    # Drop non-content chrome before reading the body.
    for chrome in soup.find_all(["nav", "footer", "header", "aside", "script", "style", "noscript"]):
        chrome.decompose()
    body = soup.select_one("article") or soup.select_one("main") or soup.body or soup

    text = " ".join(body.get_text(" ", strip=True).split())[:_MAX_POST_TEXT_CHARS]
    if not text:
        raise ThreadGenerationError("the post had no readable text to work from")

    # Outbound, non-imbue links in listing order, de-duplicated.
    links: list[str] = []
    for anchor in body.find_all("a", href=True):
        href_value = anchor.get("href")
        if not isinstance(href_value, str):
            continue
        href = href_value.strip()
        if href.startswith("http") and "imbue.com" not in href and href not in links:
            links.append(href)

    return {"title": title, "text": text, "links": links}


def _extract_external_title(html_text: str) -> str:
    """Return a best-effort title for an arbitrary page: og:title, then <title>, then <h1>."""
    soup = BeautifulSoup(html_text, "html.parser")
    og = soup.find("meta", attrs={"property": "og:title"})
    if og is not None:
        content = og.get("content")
        if isinstance(content, str) and content.strip():
            return " ".join(content.split())
    for selector in ("title", "h1"):
        element = soup.select_one(selector)
        if element is not None:
            text = " ".join(element.get_text(" ", strip=True).split())
            if text:
                return text
    return ""


def _fetch_external_content(url: str) -> dict:
    """Fetch an arbitrary (non-imbue) URL and return its ``{title, text, links}``.

    Mirrors ``_fetch_post_content`` but reads the title from og:title/<title>/<h1>
    since arbitrary pages don't follow the blog's markup. Network, empty-body,
    and unreadable pages surface as ``ThreadGenerationError`` for the caller to
    turn into an error banner.
    """
    try:
        response = httpx.get(
            url, follow_redirects=True, timeout=30, headers={"User-Agent": _YT_HEADERS["User-Agent"]}
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ThreadGenerationError(f"couldn't reach the link at {url}") from error

    title = _extract_external_title(response.text)

    soup = BeautifulSoup(response.text, "html.parser")
    for chrome in soup.find_all(["nav", "footer", "header", "aside", "script", "style", "noscript"]):
        chrome.decompose()
    body = soup.select_one("article") or soup.select_one("main") or soup.body or soup

    text = " ".join(body.get_text(" ", strip=True).split())[:_MAX_POST_TEXT_CHARS]
    if not text:
        raise ThreadGenerationError("the link had no readable text to work from")

    # Outbound links found in the body, in listing order, de-duplicated.
    links: list[str] = []
    for anchor in body.find_all("a", href=True):
        href_value = anchor.get("href")
        if not isinstance(href_value, str):
            continue
        href = href_value.strip()
        if href.startswith("http") and href not in links:
            links.append(href)

    return {"title": title, "text": text, "links": links[:20]}


def _close_directive(is_external: bool, source_url: str, is_notes: bool = False) -> str:
    """Return the CLOSE rule line, which is the only part that differs by template.

    Imbue blog posts close on Imbue's mission with a CTA to the writeup/repo.
    External (trending) content is COMMENTARY on someone else's work, so it must
    NOT invoke Imbue's mission or pitch Imbue/Bouncer -- it closes with a plain
    link to the source and a short genuine take. Notes drafts are written from
    the user's own bullet points, which ARE the source, so they close naturally
    with no mission, no CTA, and no source link appended.
    """
    if is_notes:
        return (
            "- These notes are the user's own draft material, not commentary on someone else's "
            "work. Do not close on any mission and do not pitch Imbue or any product. Do not append "
            "a call to action and do not append a source link, because there is no external source. "
            "End the post naturally. If the notes themselves contain a URL or a call to action, keep "
            "it, but add none of your own."
        )
    if is_external:
        return (
            "- This is commentary on someone else's content, not Imbue's own writing. Do not "
            "close on Imbue's mission and do not pitch Imbue or Bouncer or any Imbue product. "
            "Keep the source's facts straight and invent nothing beyond the text below. Close "
            f"with a plain link to the source at {source_url} and one short, genuine take on it."
        )
    return (
        "- Close on Imbue's mission as full sentences. Imbue builds software that is open "
        "source, runs on your own device, and that you own. Then add a call to action that "
        f"links the writeup at {source_url} along with any repo or download links found in the post."
    )


def _build_generation_prompt(
    voice_id: str,
    format_id: str,
    source_url: str,
    post_content: dict,
    is_external: bool = False,
    is_notes: bool = False,
) -> str:
    """Build the grounded, voice/format-specific prompt for one thread."""
    tweet_count = _TWEETS_PER_FORMAT[format_id]
    unit = {"twitter": "tweets", "tweet": "tweet", "linkedin": "post"}[format_id]
    links = post_content.get("links") or []
    links_block = "\n".join(f"- {link}" for link in links) if links else "(none found)"
    banned = ", ".join(_BANNED_WORDS)
    if format_id == "twitter":
        shape = '{"tweets": ["first tweet", "second tweet", "third tweet"]}'
    elif format_id == "tweet":
        shape = '{"tweets": ["the single tweet as one string"]}'
    else:
        shape = '{"tweets": ["the entire long-form post as one string"]}'

    if is_notes:
        intro = (
            "You are drafting a social post from the freeform notes below. The notes ARE the source "
            "material. Expand and connect them into a natural, well-written post in the chosen voice "
            "and format, staying faithful to what they say. You may rephrase, reorder, and connect "
            "the points, but invent no facts beyond them."
        )
        source_label = "NOTES"
    elif is_external:
        intro = (
            "You are writing a social thread that COMMENTS on the linked content below, "
            "grounded ONLY in its text. You are not the author of this content."
        )
        source_label = "SOURCE"
    else:
        intro = "You are writing social copy for Imbue, grounded ONLY in the blog post below."
        source_label = "POST"

    return f"""{intro}

VOICE
{_VOICE_GUIDANCE[voice_id]}

FORMAT
{_FORMAT_GUIDANCE[format_id]}

RULES (all mandatory)
- The very first sentence is a flat announcement-style headline that states the news plainly, with no throat-clearing and no build-up, and the rest follows from it. On twitter the first tweet opens with this headline, on linkedin the first line is this headline, and a single tweet is essentially this headline plus one or two supporting sentences and the link. The headline is only that ONE opening sentence, so every sentence after it must still vary in length and flow rather than becoming stacked one-liners.
- Use sentence case, not all-lowercase and not Title Case.
- Vary sentence rhythm, and never stack short punchy declarative sentences of the same shape. Mix lengths and connect clauses.
- Every sentence is complete with a subject and a verb, with no fragments or trailing appositives.
- Use no colons in prose. URLs are the only exception on twitter, and linkedin uses scheme-less links.
- Never use any of these words: {banned}.
- Never use "the [X] part was" constructions. Do not open with a rhetorical question. Do not use "not X, it's Y" inversion. Use no emojis and no hashtags.
- Ground every claim only in the text below, and invent nothing.
{_close_directive(is_external, source_url, is_notes)}

{source_label} TITLE
{post_content.get("title") or "(untitled)"}

{source_label} LINKS (outbound, may include repo or download links to cite)
{links_block}

{source_label} TEXT
{post_content.get("text", "")}

Return ONLY strict JSON in exactly this shape, with no markdown fences and no commentary:
{shape}
The "tweets" array must contain exactly {tweet_count} {unit}."""


def _strip_code_fences(raw: str) -> str:
    """Remove a leading ```json / ``` fence and a trailing ``` fence, if present."""
    text = raw.strip()
    if not text.startswith("```"):
        return text
    without_open = _CODE_FENCE_OPEN_RE.sub("", text)
    return _CODE_FENCE_CLOSE_RE.sub("", without_open).strip()


def _parse_model_thread_json(raw: str, format_id: str) -> list[str]:
    """Parse the model's JSON reply into a validated list of tweet strings.

    Tolerates ``` fences. Raises ThreadGenerationError if the payload is not
    JSON, is not shaped like ``{"tweets": [...]}`` (or a bare list), or has no
    non-empty strings. LinkedIn is collapsed to a single element.
    """
    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ThreadGenerationError("the model did not return valid JSON") from error

    candidate = parsed.get("tweets") if isinstance(parsed, dict) else parsed
    if not isinstance(candidate, list):
        raise ThreadGenerationError("the model's reply was not a list of posts")

    tweets = [item.strip() for item in candidate if isinstance(item, str) and item.strip()]
    if not tweets:
        raise ThreadGenerationError("the model returned no usable text")

    # LinkedIn (one long-form post) and tweet (one standalone tweet) are both
    # single-element formats; keep only the first element if the model
    # over-produced. Twitter keeps whatever tweets came back.
    return tweets[:1] if format_id in ("linkedin", "tweet") else tweets


def _model_api_base() -> str | None:
    """Return the Anthropic-compatible proxy base URL, or None for the default."""
    base = os.environ.get("ANTHROPIC_BASE_URL")
    return base.rstrip("/") if base else None


def _generation_message_content(prompt: str, corpus_block: str | None) -> str | list[dict]:
    """Build the user-message ``content`` for one generation call.

    When ``corpus_block`` is set, the big static per-voice sample of real writing
    is sent as the FIRST content block and marked ``cache_control`` ephemeral, so
    it forms a cached prefix that Anthropic bills once and then serves from its
    prompt cache; the small, task-specific ``prompt`` follows as a second block
    and is the only part that varies between calls. When it is None (the
    ``quotes`` voice, or a voice whose corpus is missing) the content stays a
    plain string on the uncached path, exactly as before.
    """
    if not corpus_block:
        return prompt
    return [
        {"type": "text", "text": corpus_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": prompt},
    ]


def _complete_generation(
    prompt: str, format_id: str, corpus_block: str | None = None
) -> tuple[list[str], str]:
    """Call the model for a thread, returning ``(tweets, model_label)``.

    Tries each model in order; a model that errors, returns an empty reply, or
    returns text that does not parse into a valid thread falls through to the
    next one. Parsing happens here (not in the caller) so a bad-JSON reply from
    the primary model still falls back. Raises ThreadGenerationError only if
    every model fails.

    ``corpus_block`` is the voice's large example corpus. When present it is sent
    as a cached prefix (see ``_generation_message_content``); when None the call
    is a plain uncached completion.
    """
    api_base = _model_api_base()
    content = _generation_message_content(prompt, corpus_block)
    logger.info(
        "thread-writer generation call: cached_corpus={} ({} chars), task_prompt={} chars",
        corpus_block is not None,
        len(corpus_block) if corpus_block else 0,
        len(prompt),
    )
    last_error: Exception | None = None
    for model_id in _GENERATION_MODELS:
        try:
            response = completion(
                model=model_id,
                api_base=api_base,
                messages=[{"role": "user", "content": content}],
                max_tokens=_MODEL_MAX_TOKENS,
            )
        except _MODEL_ERROR_TYPES as error:
            last_error = error
            logger.warning("Model {} failed to generate a thread: {}", model_id, error)
            continue
        logger.info("thread-writer usage for {}: {}", model_id, getattr(response, "usage", None))
        choice = response.choices[0] if isinstance(response, ModelResponse) else None
        text = choice.message.content or "" if isinstance(choice, Choices) else ""
        if not text.strip():
            last_error = ThreadGenerationError(f"model {model_id} returned an empty reply")
            logger.warning("Model {} returned an empty or malformed reply", model_id)
            continue
        try:
            tweets = _parse_model_thread_json(text, format_id)
        except ThreadGenerationError as error:
            last_error = error
            logger.warning("Model {} returned unparseable output: {}", model_id, error)
            continue
        return tweets, _MODEL_LABEL_BY_ID[model_id]
    raise ThreadGenerationError(f"the model was unavailable ({last_error})") from last_error


def _write_thread_file(
    slug: str,
    voice_id: str,
    format_id: str,
    source_url: str,
    post_content: dict,
    model_label: str,
    tweets: list[str],
    data_dir: Path = DATA_DIR,
    generated_from: str = "imbue.com/blog",
) -> Path:
    """Write one generated thread to ``threads/<slug>/<voice>.<format>.json``."""
    post_dir = data_dir / "threads" / slug
    post_dir.mkdir(parents=True, exist_ok=True)
    path = post_dir / f"{voice_id}.{format_id}.json"
    payload = {
        "voice_id": voice_id,
        "voice_label": _VOICE_LABEL_BY_ID[voice_id],
        "format": format_id,
        "source_url": source_url,
        "source_title": post_content.get("title") or "original post",
        "author_name": "Imbue",
        "author_handle": "imbue_ai",
        "model": model_label,
        "generated_from": generated_from,
        "tweets": tweets,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return path


def _redirect_after_generate(slug: str, error_message: str | None) -> WerkzeugResponse:
    """Redirect back to the reader (or the index), carrying any error in the query.

    The target is *relative* to ``/generate`` so it resolves correctly whether
    the app is reached directly or behind the ``/service/thread-writer/`` proxy
    (which does not rewrite redirect ``Location`` headers). This mirrors the
    existing schedule/mark redirect.
    """
    target = f"post/{slug}" if slug else "."
    if error_message:
        target = f"{target}?error={quote(error_message)}"
    return redirect(target)


def _resolve_source(slug: str, data_dir: Path = DATA_DIR) -> tuple[str, str]:
    """Return ``(source_url, source_kind)`` for a slug.

    ``source_kind`` is one of ``"external"``, ``"notes"``, or ``"imbue"``. The
    lookup order is: the trending registry first (a slug present there is
    external commentary on a pasted link), then the notes-drafts registry (a
    draft written from freeform notes, which has no source URL), and otherwise
    the slug is treated as an imbue.com blog slug. This is what lets the reader's
    Generate/Regenerate buttons work for imbue posts, trending links, and notes
    drafts alike.
    """
    entry = _load_trending(data_dir).get(slug)
    if entry and entry.get("url"):
        return entry["url"], "external"
    if slug in _load_drafts(data_dir):
        return "", "notes"
    return f"{BLOG_URL}/{slug}", "imbue"


def _generate_thread(
    slug: str,
    voice_id: str,
    format_id: str,
    source_url: str,
    source_kind: str,
    data_dir: Path = DATA_DIR,
) -> None:
    """Fetch/read the source, generate one thread, and save it. Raises on failure.

    Chooses the source path and the CLOSE template by ``source_kind``: external
    (trending) content is fetched and written as commentary; a notes draft reads
    its stored bullet points from the drafts registry (no fetch, no source link);
    an imbue blog post is fetched and closes on Imbue's mission. Raises
    ``ThreadGenerationError`` for the caller to turn into an error banner.
    """
    if source_kind == "notes":
        entry = _load_drafts(data_dir).get(slug)
        if not entry or not (entry.get("notes") or "").strip():
            raise ThreadGenerationError("this draft's notes could not be found")
        content = {"title": entry.get("title") or "notes draft", "text": entry["notes"], "links": []}
        source_url = ""
        generated_from = "notes"
    elif source_kind == "external":
        content = _fetch_external_content(source_url)
        generated_from = _url_host(source_url)
    else:
        content = _fetch_post_content(source_url)
        generated_from = "imbue.com/blog"
    prompt = _build_generation_prompt(
        voice_id,
        format_id,
        source_url,
        content,
        is_external=source_kind == "external",
        is_notes=source_kind == "notes",
    )
    tweets, model_label = _complete_generation(
        prompt, format_id, corpus_block=_voice_corpus_block(voice_id)
    )
    _write_thread_file(
        slug,
        voice_id,
        format_id,
        source_url,
        content,
        model_label,
        tweets,
        data_dir=data_dir,
        generated_from=generated_from,
    )


@app.route("/generate", methods=["POST"])
def generate() -> WerkzeugResponse:
    """Generate one voice+format thread for a post on demand and save it.

    Works for both imbue blog posts and pasted trending links: the slug is
    resolved through the trending registry first, so Generate/Regenerate use the
    right source URL and template. Never returns a 500: any failure redirects
    back to the reader with a short message rendered in the error banner.
    """
    slug = (request.form.get("slug") or "").strip()
    voice_id = (request.form.get("voice") or "").strip()
    format_id = (request.form.get("format") or "").strip()

    if (
        not slug
        or "/" in slug
        or voice_id not in _VOICE_LABEL_BY_ID
        or format_id not in _FORMAT_LABEL_BY_ID
    ):
        return _redirect_after_generate(slug, "that generation request wasn't valid")

    source_url, source_kind = _resolve_source(slug)
    try:
        _generate_thread(slug, voice_id, format_id, source_url, source_kind)
    except ThreadGenerationError as error:
        logger.warning("Failed to generate {}/{} for {}: {}", voice_id, format_id, slug, error)
        return _redirect_after_generate(slug, str(error))

    # Relative redirect so it resolves under the proxy (see _redirect_after_generate).
    return redirect(f"post/{slug}")


def _redirect_to_calendar(error_message: str | None = None) -> WerkzeugResponse:
    """Redirect to the Schedule view, carrying any error in the query string.

    Relative ("calendar") so it resolves under the ``/service/thread-writer/``
    proxy, which does not rewrite redirect ``Location`` headers.
    """
    target = "calendar"
    if error_message:
        target = f"{target}?error={quote(error_message)}"
    return redirect(target)


@app.route("/trending", methods=["POST"])
def trending() -> WerkzeugResponse:
    """Register a pasted link and generate a (voice, twitter) commentary thread.

    Fetches the URL live, saves a registry entry, generates an EXTERNAL thread
    grounded in the fetched text, and redirects to the new post's reader. Any
    fetch/parse/model failure redirects back to the Schedule view with a short
    message in the error banner rather than returning a 500.
    """
    url = (request.form.get("url") or "").strip()
    voice_id = (request.form.get("voice") or "").strip()

    if not url or not url.lower().startswith(("http://", "https://")):
        return _redirect_to_calendar("paste a full link starting with http")
    if voice_id not in _VOICE_LABEL_BY_ID:
        return _redirect_to_calendar("pick a voice for the thread")

    try:
        content = _fetch_external_content(url)
    except ThreadGenerationError as error:
        logger.warning("Failed to fetch trending link {}: {}", url, error)
        return _redirect_to_calendar(str(error))

    registry = _load_trending()
    slug = _derive_trending_slug(url, registry)
    registry[slug] = {
        "url": url,
        "title": content.get("title") or url,
        "added": datetime.now().date().isoformat(),
    }
    _save_trending(registry)

    try:
        prompt = _build_generation_prompt(voice_id, "twitter", url, content, is_external=True)
        tweets, model_label = _complete_generation(
            prompt, "twitter", corpus_block=_voice_corpus_block(voice_id)
        )
    except ThreadGenerationError as error:
        logger.warning("Failed to generate trending thread for {}: {}", url, error)
        # The registry entry is already saved, so the link still shows in the
        # Trending section; surface the generation failure on the Schedule view.
        return _redirect_to_calendar(str(error))

    _write_thread_file(
        slug, voice_id, "twitter", url, content, model_label, tweets, generated_from=_url_host(url)
    )
    # Relative redirect so it resolves under the proxy.
    return redirect(f"post/{slug}")


@app.route("/draft", methods=["POST"])
def draft() -> WerkzeugResponse:
    """Draft a post from pasted freeform notes and save it.

    Derives a title and slug from the notes' first line, saves a registry entry
    to ``drafts.json``, generates the chosen voice+format draft grounded in the
    notes (no fetch), and redirects to the new post's reader. An empty-notes
    request or a generation failure redirects back to the Schedule view with a
    short message in the error banner rather than returning a 500.
    """
    notes = (request.form.get("notes") or "").strip()
    voice_id = (request.form.get("voice") or "").strip()
    format_id = (request.form.get("format") or "").strip()

    if not notes:
        return _redirect_to_calendar("paste some notes to draft from")
    if voice_id not in _VOICE_LABEL_BY_ID:
        return _redirect_to_calendar("pick a voice for the draft")
    if format_id not in _FORMAT_LABEL_BY_ID:
        return _redirect_to_calendar("pick a format for the draft")

    title = _draft_title_from_notes(notes)
    if not title:
        return _redirect_to_calendar("paste some notes to draft from")

    registry = _load_drafts()
    slug = _derive_draft_slug(title, registry)
    registry[slug] = {
        "title": title,
        "notes": notes,
        "added": datetime.now().date().isoformat(),
    }
    _save_drafts(registry)

    content = {"title": title, "text": notes, "links": []}
    try:
        prompt = _build_generation_prompt(voice_id, format_id, "", content, is_notes=True)
        tweets, model_label = _complete_generation(
            prompt, format_id, corpus_block=_voice_corpus_block(voice_id)
        )
    except ThreadGenerationError as error:
        logger.warning("Failed to draft from notes for {}: {}", slug, error)
        # The draft entry is already saved, so it still shows in the Drafts list;
        # surface the generation failure on the Schedule view.
        return _redirect_to_calendar(str(error))

    _write_thread_file(
        slug, voice_id, format_id, "", content, model_label, tweets, generated_from="notes"
    )
    # Relative redirect so it resolves under the proxy.
    return redirect(f"post/{slug}")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


_WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _cadence_status(days_since: int) -> tuple[str, str]:
    """Return (headline, sub) copy for the cadence suggestion given days elapsed."""
    if days_since > CADENCE_DAYS:
        overdue = days_since - CADENCE_DAYS
        unit = "day" if overdue == 1 else "days"
        return ("A new thread is suggested", f"About {overdue} {unit} past the weekly cadence.")
    remaining = CADENCE_DAYS - days_since
    if remaining == 0:
        return ("A new thread is suggested today", "You're right at the weekly cadence.")
    unit = "day" if remaining == 1 else "days"
    return (f"Next suggested thread in {remaining} {unit}", "Based on a rough one-per-week cadence.")


def _render_day_cell(day: int, year: int, month: int, by_day: dict, today: datetime) -> str:
    if day == 0:
        return '<div class="day empty"></div>'
    is_today = year == today.year and month == today.month and day == today.day
    chips = []
    for post in by_day.get((year, month, day), []):
        cls = "chip threaded" if post["threaded"] else "chip"
        title = html.escape(post["title"])
        if post["threaded"]:
            # A threaded post links into its own thread reader (same tab).
            state = "Has a thread"
            href = f'/post/{html.escape(_post_slug(post["url"]))}'
            target = ""
        else:
            # No thread yet -- link out to the original post so it can be read.
            state = "No thread yet"
            href = html.escape(post["url"])
            target = ' target="_blank" rel="noopener noreferrer"'
        chips.append(
            f'<a class="{cls}" href="{href}"{target} title="{title} — {state}">'
            f'<span class="dot"></span><span class="chip-title">{title}</span></a>'
        )
    today_cls = " today" if is_today else ""
    chips_html = "".join(chips)
    return (
        f'<div class="day{today_cls}"><span class="daynum">{day}</span>'
        f'<div class="chips">{chips_html}</div></div>'
    )


def _render_month(year: int, month: int, by_day: dict, today: datetime) -> str:
    weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(year, month)
    label = f"{calendar.month_name[month]} {year}"
    dow = "".join(f'<div class="dow">{d}</div>' for d in _WEEKDAY_LABELS)
    cells = "".join(
        _render_day_cell(day, year, month, by_day, today) for week in weeks for day in week
    )
    return (
        f'<section class="month"><h2 class="month-name">{html.escape(label)}</h2>'
        f'<div class="cal-grid">{dow}{cells}</div></section>'
    )


def _render_queue_row(item: dict, recommended: date) -> str:
    title = html.escape(item["title"])
    kind = html.escape(item["kind"])
    item_id = html.escape(item["id"])
    last = html.escape(item["last_display"]) if item["last_display"] else "never posted"
    verb = "posted" if item["kind"] == "Blog" else "revived"
    action = "Mark thread posted" if item["kind"] == "Blog" else "Mark video re-shared"
    overdue = html.escape(_overdue_label(item["overdue_days"]))
    # A staggered, suggested posting date so the backlog isn't all-at-once.
    suggested = html.escape(recommended.strftime("%a, %b %-d"))

    # A blog row links into that post's own thread reader (same tab), with a
    # small secondary link back to the original imbue.com post. A YouTube row
    # has no thread, so it links straight out to the video.
    if item["kind"] == "Blog":
        title_href = f'/post/{html.escape(item["slug"])}'
        title_link = f'<a class="q-title" href="{title_href}">{title}</a>'
        source_link = (
            f'<a class="q-source" href="{html.escape(item["source_url"])}" '
            f'target="_blank" rel="noopener noreferrer">source &#8599;</a>'
        )
    else:
        title_link = (
            f'<a class="q-title" href="{html.escape(item["url"])}" '
            f'target="_blank" rel="noopener noreferrer">{title} &#8599;</a>'
        )
        source_link = ""

    return f"""
      <div class="q-row">
        <span class="q-kind {item['kind_class']}">{kind}</span>
        <div class="q-main">
          {title_link}
          {source_link}
          <div class="q-meta">
            <span>Last {verb}: {last}</span>
            <span class="q-overdue">{overdue}</span>
            <span class="q-suggest">Suggested: {suggested}</span>
          </div>
        </div>
        <form class="q-mark" method="post" action="schedule/mark">
          <input type="hidden" name="item_id" value="{item_id}">
          <button type="submit" title="{action}" aria-label="{action}">&#10003;</button>
        </form>
      </div>"""


def _render_up_next(items: list[dict], youtube_ok: bool, today: datetime) -> str:
    note = ""
    if not youtube_ok:
        note = (
            '<div class="q-warn">Couldn\'t reach YouTube right now, so only blog '
            "posts are shown below.</div>"
        )
    if not items:
        body = (
            '<div class="q-empty">Nothing is due right now. New blog posts and '
            "videos will appear here as they come due.</div>"
        )
    else:
        # Assign staggered suggested dates in queue order (item 0 -> tomorrow).
        body = "".join(
            _render_queue_row(item, _recommended_post_date(index, today))
            for index, item in enumerate(items)
        )
    return f"""
    <section class="queue">
      <div class="queue-head">
        <h2 class="queue-title">Up next</h2>
        <span class="queue-count">{len(items)} due</span>
      </div>
      {note}
      {body}
    </section>"""


def _render_trending_saved(registry: dict) -> str:
    """Render the list of saved trending links, newest first, or an empty note."""
    entries = _trending_entries_newest_first(registry)
    if not entries:
        return (
            '<div class="t-empty">No trending threads yet. Paste a link above to write one.</div>'
        )
    rows = []
    for slug, entry in entries:
        title = html.escape(entry.get("title") or entry.get("url") or slug)
        url = html.escape(entry.get("url", ""))
        added = html.escape(entry.get("added", ""))
        rows.append(
            f'<div class="t-row">'
            f'<a class="t-title" href="/post/{html.escape(slug)}">{title}</a>'
            f'<a class="t-source" href="{url}" target="_blank" rel="noopener noreferrer">source &#8599;</a>'
            f'<span class="t-added">{added}</span>'
            f"</div>"
        )
    return '<div class="t-list">' + "".join(rows) + "</div>"


def _render_trending_block(registry: dict) -> str:
    """Render the paste-a-link input plus the list of saved trending threads."""
    options = "\n".join(
        f'<option value="{voice_id}">{html.escape(label)}</option>'
        for voice_id, label in VOICE_PRESETS
    )
    saved = _render_trending_saved(registry)
    return f"""
    <section class="trending">
      <div class="trending-head">
        <h2 class="trending-title">Trending link</h2>
        <span class="trending-sub">Paste any link to write a thread commenting on it.</span>
      </div>
      <form class="trending-form" method="post" action="trending">
        <input class="trending-url" type="url" name="url" required
               placeholder="https://example.com/article-to-comment-on">
        <select class="trending-voice" name="voice" aria-label="Voice">
{options}
        </select>
        <button class="trending-btn" type="submit">Generate thread</button>
      </form>
      <div class="trending-saved">
        <h3 class="trending-saved-title">Trending</h3>
        {saved}
      </div>
    </section>"""


def _render_drafts_saved(registry: dict) -> str:
    """Render the list of saved notes drafts, newest first, or an empty note."""
    entries = _drafts_entries_newest_first(registry)
    if not entries:
        return '<div class="t-empty">No drafts yet. Paste some notes above to write one.</div>'
    rows = []
    for slug, entry in entries:
        title = html.escape(entry.get("title") or slug)
        added = html.escape(entry.get("added", ""))
        rows.append(
            f'<div class="t-row">'
            f'<a class="t-title" href="/post/{html.escape(slug)}">{title}</a>'
            f'<span class="t-added">{added}</span>'
            f"</div>"
        )
    return '<div class="t-list">' + "".join(rows) + "</div>"


def _render_notes_block(registry: dict) -> str:
    """Render the paste-your-notes input plus the list of saved notes drafts."""
    voice_options = "\n".join(
        f'<option value="{voice_id}">{html.escape(label)}</option>'
        for voice_id, label in VOICE_PRESETS
    )
    format_options = "\n".join(
        f'<option value="{format_id}">{html.escape(flabel)}</option>'
        for format_id, flabel in FORMAT_PRESETS
    )
    saved = _render_drafts_saved(registry)
    return f"""
    <section class="trending notes">
      <div class="trending-head">
        <h2 class="trending-title">Draft from notes</h2>
        <span class="trending-sub">Paste bullet points and get a drafted post written from them.</span>
      </div>
      <form class="notes-form" method="post" action="draft">
        <textarea class="notes-input" name="notes" rows="5" required
                  placeholder="- one thought per line&#10;- what you shipped&#10;- why it matters"></textarea>
        <div class="notes-controls">
          <select class="trending-voice" name="voice" aria-label="Voice">
{voice_options}
          </select>
          <select class="trending-voice" name="format" aria-label="Format">
{format_options}
          </select>
          <button class="trending-btn" type="submit">Draft it</button>
        </div>
      </form>
      <div class="trending-saved">
        <h3 class="trending-saved-title">Drafts</h3>
        {saved}
      </div>
    </section>"""


def _render_calendar_page(
    posts: list[dict],
    threaded: set[str],
    today: datetime,
    up_next_html: str,
    trending_html: str = "",
    notes_html: str = "",
    error_message: str | None = None,
) -> str:
    if error_message:
        error_banner_html = f'<div class="cal-error">{html.escape(error_message)}</div>'
    else:
        error_banner_html = ""

    for post in posts:
        post["threaded"] = post["url"] in threaded

    by_day: dict[tuple[int, int, int], list[dict]] = {}
    for post in posts:
        d = post["date"]
        by_day.setdefault((d.year, d.month, d.day), []).append(post)

    # Recent months that actually contain posts, newest first (cap the span).
    months = sorted({(p["date"].year, p["date"].month) for p in posts}, reverse=True)[:6]
    months_html = "".join(_render_month(y, m, by_day, today) for y, m in months)

    total = len(posts)
    threaded_count = sum(1 for p in posts if p["threaded"])
    threaded_posts = [p for p in posts if p["threaded"]]

    if threaded_posts:
        last = max(threaded_posts, key=lambda p: p["date"])
        days_since = (today.date() - last["date"].date()).days
        big = str(days_since)
        big_label = "day since the last posted thread" if days_since == 1 else "days since the last posted thread"
        last_line = (
            f'Last thread: <a href="{html.escape(last["url"])}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(last["title"])}</a> '
            f'&middot; {last["date"].strftime("%d %b %Y")}'
        )
        head, sub = _cadence_status(days_since)
        overdue_cls = " overdue" if days_since > CADENCE_DAYS else ""
        nudge_html = (
            f'<div class="nudge{overdue_cls}"><span class="nudge-head">{html.escape(head)}</span>'
            f'<span class="nudge-sub">{html.escape(sub)}</span></div>'
        )
    else:
        big = "—"
        big_label = "no posted threads yet"
        last_line = "No blog post has a thread yet."
        nudge_html = (
            '<div class="nudge"><span class="nudge-head">Start the cadence</span>'
            '<span class="nudge-sub">Write a thread for a recent post to begin tracking.</span></div>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thread writer &middot; Schedule</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#f7f5f2; --surface:#fff; --border:#e8e3db; --text:#1b1a17; --muted:#837c72;
  --accent:#c15f3c; --accent-soft:#f2e4dc; --connector:#e4ded4;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#131211; --surface:#1b1a18; --border:#2b2926; --text:#edeae4; --muted:#9a948a;
    --accent:#e07850; --accent-soft:#2e211b; --connector:#2c2a27;
  }}
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; }}
body {{ background:var(--bg); color:var(--text); font-family:"Inter",system-ui,sans-serif; line-height:1.5; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:820px; margin:0 auto; padding:28px 20px 120px; }}

.topbar {{ display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:22px; flex-wrap:wrap; }}
.topbar h1 {{ font-size:20px; font-weight:700; letter-spacing:-0.02em; margin:0; }}

.hero {{ display:flex; align-items:center; gap:24px; background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:22px 24px; margin-bottom:14px; flex-wrap:wrap; }}
.hero-count {{ display:flex; align-items:baseline; gap:12px; }}
.hero-num {{ font-family:"JetBrains Mono",monospace; font-size:64px; font-weight:500; line-height:1; color:var(--accent); letter-spacing:-0.03em; }}
.hero-label {{ font-size:13px; color:var(--muted); max-width:120px; }}
.nudge {{ margin-left:auto; display:flex; flex-direction:column; gap:2px; text-align:right; padding:10px 16px; border-radius:12px; background:var(--accent-soft); }}
.nudge.overdue {{ box-shadow:inset 0 0 0 1px var(--accent); }}
.nudge-head {{ font-size:14px; font-weight:600; color:var(--text); }}
.nudge.overdue .nudge-head {{ color:var(--accent); }}
.nudge-sub {{ font-size:12px; color:var(--muted); }}

.queue {{ background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:18px 20px 8px; margin-bottom:22px; }}
.queue-head {{ display:flex; align-items:baseline; gap:10px; margin-bottom:4px; }}
.queue-title {{ font-size:16px; font-weight:600; letter-spacing:-0.01em; margin:0; }}
.queue-count {{ font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); }}
.q-warn {{ font-size:12px; color:var(--muted); background:var(--accent-soft); border-radius:8px; padding:7px 10px; margin:8px 0; }}
.q-empty {{ font-size:13px; color:var(--muted); padding:16px 2px 20px; }}
.q-row {{ display:flex; align-items:flex-start; gap:12px; padding:14px 0; border-top:1px solid var(--border); }}
.q-row:first-of-type {{ border-top:none; }}
.q-kind {{ flex:0 0 auto; font-family:"JetBrains Mono",monospace; font-size:10px; letter-spacing:0.04em; text-transform:uppercase; padding:3px 8px; border-radius:999px; border:1px solid var(--border); color:var(--muted); margin-top:1px; }}
.q-kind.blog {{ color:var(--accent); border-color:var(--accent-soft); background:var(--accent-soft); }}
.q-kind.yt {{ color:var(--muted); }}
.q-main {{ flex:1 1 auto; min-width:0; }}
.q-title {{ display:inline-block; font-size:14.5px; font-weight:500; color:var(--text); text-decoration:none; line-height:1.35; }}
.q-title:hover {{ color:var(--accent); }}
.q-source {{ display:inline-block; margin-left:8px; font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); text-decoration:none; }}
.q-source:hover {{ color:var(--accent); }}
.q-meta {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:3px; font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); }}
.q-overdue {{ color:var(--accent); }}
.q-suggest {{ color:var(--muted); }}
.q-mark {{ flex:0 0 auto; }}
.q-mark button {{ width:32px; height:32px; border-radius:50%; border:1px solid var(--border); background:var(--surface); color:var(--muted); font-size:15px; line-height:1; cursor:pointer; transition:all .15s ease; }}
.q-mark button:hover {{ border-color:var(--accent); color:#fff; background:var(--accent); }}

.cal-error {{ background:var(--accent-soft); color:var(--accent); border:1px solid var(--accent); border-radius:12px; padding:12px 16px; margin-bottom:16px; font-size:13.5px; font-weight:500; }}

.input-cards {{ display:flex; gap:16px; align-items:stretch; margin-bottom:22px; flex-wrap:wrap; }}
.input-cards .trending {{ flex:1 1 340px; min-width:0; margin-bottom:0; display:flex; flex-direction:column; }}
.input-cards .trending .trending-saved {{ margin-top:auto; }}
.trending {{ background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:18px 20px 16px; margin-bottom:22px; }}
.notes-form {{ display:flex; flex-direction:column; gap:10px; }}
.notes-input {{ font-family:"Inter",sans-serif; font-size:13px; line-height:1.5; color:var(--text); background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:11px 14px; resize:vertical; min-height:96px; }}
.notes-input:focus {{ outline:none; border-color:var(--accent); }}
.notes-controls {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
.notes-controls .trending-btn {{ margin-left:auto; }}
.trending-head {{ display:flex; align-items:baseline; gap:10px; margin-bottom:12px; flex-wrap:wrap; }}
.trending-title {{ font-size:16px; font-weight:600; letter-spacing:-0.01em; margin:0; }}
.trending-sub {{ font-size:12px; color:var(--muted); }}
.trending-form {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
.trending-url {{ flex:1 1 260px; min-width:0; font-family:"Inter",sans-serif; font-size:13px; color:var(--text); background:var(--bg); border:1px solid var(--border); border-radius:999px; padding:9px 16px; }}
.trending-url:focus {{ outline:none; border-color:var(--accent); }}
.trending-voice {{ font-family:"Inter",sans-serif; font-size:13px; font-weight:500; color:var(--text); background:var(--surface); border:1px solid var(--border); border-radius:999px; padding:8px 32px 8px 14px; cursor:pointer; appearance:none; background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23837c72' stroke-width='2.5'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E"); background-repeat:no-repeat; background-position:right 12px center; }}
.trending-voice:hover {{ border-color:var(--accent); }}
.trending-btn {{ font-family:"Inter",sans-serif; font-size:13px; font-weight:600; color:#fff; background:var(--accent); border:1px solid var(--accent); border-radius:999px; padding:9px 20px; cursor:pointer; transition:opacity .15s ease; }}
.trending-btn:hover {{ opacity:.9; }}
.trending-saved {{ margin-top:16px; padding-top:14px; border-top:1px solid var(--border); }}
.trending-saved-title {{ font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted); margin:0 0 8px; }}
.t-empty {{ font-size:13px; color:var(--muted); padding:2px 0 4px; }}
.t-list {{ display:flex; flex-direction:column; }}
.t-row {{ display:flex; align-items:baseline; gap:10px; padding:9px 0; border-top:1px solid var(--border); }}
.t-row:first-child {{ border-top:none; }}
.t-title {{ flex:1 1 auto; min-width:0; font-size:14px; font-weight:500; color:var(--text); text-decoration:none; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.t-title:hover {{ color:var(--accent); }}
.t-source {{ flex:0 0 auto; font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); text-decoration:none; }}
.t-source:hover {{ color:var(--accent); }}
.t-added {{ flex:0 0 auto; font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); }}

.summary {{ font-size:13px; color:var(--muted); margin:0 2px 26px; display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
.summary a {{ color:var(--accent); text-decoration:none; }}
.summary a:hover {{ text-decoration:underline; }}
.legend {{ display:flex; gap:14px; margin-left:auto; }}
.legend span {{ display:inline-flex; align-items:center; gap:6px; font-family:"JetBrains Mono",monospace; font-size:11px; }}

.month {{ margin-bottom:30px; }}
.month-name {{ font-size:15px; font-weight:600; letter-spacing:-0.01em; margin:0 0 10px 2px; }}
.cal-grid {{ display:grid; grid-template-columns:repeat(7,1fr); gap:6px; }}
.dow {{ font-family:"JetBrains Mono",monospace; font-size:10px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted); text-align:center; padding:2px 0 4px; }}
.day {{ min-height:78px; background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:5px 6px; display:flex; flex-direction:column; gap:4px; overflow:hidden; }}
.day.empty {{ background:transparent; border-color:transparent; }}
.day.today {{ border-color:var(--accent); }}
.daynum {{ font-family:"JetBrains Mono",monospace; font-size:11px; color:var(--muted); }}
.day.today .daynum {{ color:var(--accent); font-weight:500; }}
.chips {{ display:flex; flex-direction:column; gap:4px; }}
.chip {{ display:flex; align-items:center; gap:5px; text-decoration:none; font-size:11px; line-height:1.25; color:var(--text); border:1px solid var(--border); border-radius:7px; padding:3px 6px; background:var(--bg); transition:border-color .15s ease; }}
.chip:hover {{ border-color:var(--accent); }}
.chip .dot {{ width:6px; height:6px; border-radius:50%; flex:0 0 auto; border:1px solid var(--accent); background:transparent; }}
.chip.threaded {{ background:var(--accent-soft); border-color:var(--accent-soft); }}
.chip.threaded .dot {{ background:var(--accent); border-color:var(--accent); }}
.chip-title {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.leg-dot {{ width:8px; height:8px; border-radius:50%; }}
.leg-dot.on {{ background:var(--accent); }}
.leg-dot.off {{ border:1px solid var(--accent); }}
:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}

@media (max-width:640px) {{
  .cal-grid {{ gap:3px; }}
  .day {{ min-height:60px; padding:3px 4px; }}
  .chip-title {{ display:none; }}
  .nudge {{ margin-left:0; text-align:left; }}
}}
</style>
</head>
<body>
  <main class="wrap">
    <div class="topbar">
      <h1>Schedule</h1>
    </div>

    {error_banner_html}
    <div class="input-cards">
      {trending_html}
      {notes_html}
    </div>

    {up_next_html}

    <div class="hero">
      <div class="hero-count">
        <span class="hero-num">{html.escape(big)}</span>
        <span class="hero-label">{html.escape(big_label)}</span>
      </div>
      {nudge_html}
    </div>

    <div class="summary">
      <span>{last_line}</span>
      <span class="legend">
        <span><span class="leg-dot on"></span>Threaded ({threaded_count})</span>
        <span><span class="leg-dot off"></span>Not yet ({total - threaded_count})</span>
      </span>
    </div>

    {months_html}
  </main>
</body>
</html>"""


@app.route("/calendar")
def calendar_view() -> Response:
    try:
        posts = _fetch_blog_posts()
    except httpx.HTTPError:
        return Response(
            "<!doctype html><body style='font-family:sans-serif;padding:40px'>"
            "<h1>Couldn't reach the blog</h1>"
            "<p>The Imbue blog list couldn't be loaded right now. Try again shortly.</p>"
            "</body>",
            mimetype="text/html",
        )
    threaded = _threaded_source_urls()
    state = _load_schedule_state()
    today = datetime.now()
    youtube_ok = True
    try:
        videos = _fetch_youtube_videos()
    except (httpx.HTTPError, YouTubeUnavailableError, ET.ParseError):
        videos = []
        youtube_ok = False
    items = _build_up_next(posts, videos, state, today)
    up_next_html = _render_up_next(items, youtube_ok, today)
    trending_html = _render_trending_block(_load_trending())
    notes_html = _render_notes_block(_load_drafts())
    return Response(
        _render_calendar_page(
            posts,
            threaded,
            today,
            up_next_html,
            trending_html=trending_html,
            notes_html=notes_html,
            error_message=request.args.get("error"),
        ),
        mimetype="text/html",
    )


@app.route("/schedule/mark", methods=["POST"])
def schedule_mark() -> WerkzeugResponse:
    """Record that the user posted/shared an item today; it leaves the queue."""
    item_id = (request.form.get("item_id") or "").strip()
    if item_id:
        state = _load_schedule_state()
        entry = state.get(item_id) or _blank_entry()
        entry["last_activated"] = datetime.now().date().isoformat()
        state[item_id] = entry
        _save_schedule_state(state)
    return redirect("../calendar")


def main() -> None:
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
