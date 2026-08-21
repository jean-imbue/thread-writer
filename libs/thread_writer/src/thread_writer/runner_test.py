import json
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from thread_writer.runner import _build_generation_prompt
from thread_writer.runner import _build_up_next
from thread_writer.runner import _build_voice_corpus_block
from thread_writer.runner import _clean_corpus_line
from thread_writer.runner import _close_directive
from thread_writer.runner import _generation_message_content
from thread_writer.runner import _parse_corpus_examples
from thread_writer.runner import _voice_corpus_block
from thread_writer.runner import _derive_draft_slug
from thread_writer.runner import _derive_trending_slug
from thread_writer.runner import _draft_title_from_notes
from thread_writer.runner import _drafts_entries_newest_first
from thread_writer.runner import _extract_external_title
from thread_writer.runner import _is_iso_date
from thread_writer.runner import _load_drafts
from thread_writer.runner import _load_post_threads
from thread_writer.runner import _load_schedule_state
from thread_writer.runner import _load_trending
from thread_writer.runner import _overdue_label
from thread_writer.runner import _parse_model_thread_json
from thread_writer.runner import _parse_youtube_feed
from thread_writer.runner import _post_slug
from thread_writer.runner import _recommended_post_date
from thread_writer.runner import _redirect_after_generate
from thread_writer.runner import _resolve_source
from thread_writer.runner import _save_drafts
from thread_writer.runner import _save_schedule_state
from thread_writer.runner import _save_trending
from thread_writer.runner import _strip_code_fences
from thread_writer.runner import _trending_entries_newest_first
from thread_writer.runner import _url_host
from thread_writer.runner import BLOG_URL
from thread_writer.runner import FORMAT_PRESETS
from thread_writer.runner import FORMAT_PUBLISH
from thread_writer.runner import RECOMMEND_SPACING_DAYS
from thread_writer.runner import ThreadGenerationError

_TODAY = datetime(2026, 7, 20)

_SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <yt:videoId>abc123</yt:videoId>
    <title>First   video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
    <published>2026-06-01T10:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>def456</yt:videoId>
    <title>Second video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=def456"/>
    <published>2026-07-15T10:00:00+00:00</published>
  </entry>
</feed>"""


def _blog_post(url: str, days_ago: int, title: str = "A post") -> dict:
    when = datetime(2026, 7, 20)
    return {"title": title, "url": url, "date": when - timedelta(days=days_ago), "category": ""}


def test_parse_youtube_feed_extracts_fields() -> None:
    videos = _parse_youtube_feed(_SAMPLE_FEED)
    assert [v["video_id"] for v in videos] == ["abc123", "def456"]
    assert videos[0]["title"] == "First video"
    assert videos[0]["url"] == "https://www.youtube.com/watch?v=abc123"
    assert videos[0]["published"].year == 2026


def test_parse_youtube_feed_ignores_malformed_entries() -> None:
    broken = _SAMPLE_FEED.replace("<published>2026-06-01T10:00:00+00:00</published>", "")
    videos = _parse_youtube_feed(broken)
    assert [v["video_id"] for v in videos] == ["def456"]


def test_blog_never_posted_is_due() -> None:
    post = _blog_post("https://imbue.com/blog/new", days_ago=30)
    items = _build_up_next([post], [], {}, _TODAY)
    assert len(items) == 1
    assert items[0]["kind"] == "Blog"
    assert items[0]["last_display"] is None
    assert items[0]["overdue_days"] == 30 - 7
    # Blog rows carry the slug + source URL used to link into the post's thread.
    assert items[0]["slug"] == "new"
    assert items[0]["source_url"] == "https://imbue.com/blog/new"


def test_blog_older_than_queue_window_is_excluded() -> None:
    # Never posted, but published beyond the ~4 month queue window -> not queued.
    post = _blog_post("https://imbue.com/blog/ancient", days_ago=200)
    assert _build_up_next([post], [], {}, _TODAY) == []


def test_blog_draft_without_state_is_still_due() -> None:
    # Pre-generating a draft thread must NOT remove a post from the queue: the
    # queue tracks what still needs to be POSTED, not what has been drafted.
    url = "https://imbue.com/blog/drafted"
    post = _blog_post(url, days_ago=30)
    items = _build_up_next([post], [], {}, _TODAY)
    assert len(items) == 1
    assert items[0]["id"] == url


def test_blog_recently_marked_leaves_queue() -> None:
    url = "https://imbue.com/blog/x"
    post = _blog_post(url, days_ago=100)
    state = {url: {"last_activated": "2026-07-18", "scheduled_date": None, "note": ""}}
    assert _build_up_next([post], [], state, _TODAY) == []


def test_blog_stale_last_activated_is_due_again() -> None:
    url = "https://imbue.com/blog/x"
    post = _blog_post(url, days_ago=100)
    state = {url: {"last_activated": "2026-07-01", "scheduled_date": None, "note": "hi"}}
    items = _build_up_next([post], [], state, _TODAY)
    assert len(items) == 1
    assert items[0]["overdue_days"] == 19 - 7
    assert items[0]["note"] == "hi"
    assert items[0]["last_display"] == "01 Jul 2026"


def test_youtube_due_and_within_cadence() -> None:
    videos = _parse_youtube_feed(_SAMPLE_FEED)
    items = _build_up_next([], videos, {}, _TODAY)
    # abc123 (2026-06-01) is 49 days old -> due; def456 (2026-07-15) is 5 days -> not.
    assert [i["id"] for i in items] == ["abc123"]
    assert items[0]["kind"] == "YouTube"
    assert items[0]["overdue_days"] == 49 - 30


def test_youtube_recent_revive_leaves_queue() -> None:
    videos = _parse_youtube_feed(_SAMPLE_FEED)
    state = {"abc123": {"last_activated": "2026-07-10", "scheduled_date": None, "note": ""}}
    assert _build_up_next([], videos, state, _TODAY) == []


def test_blog_items_come_before_videos() -> None:
    post = _blog_post("https://imbue.com/blog/new", days_ago=30)
    videos = _parse_youtube_feed(_SAMPLE_FEED)
    items = _build_up_next([post], videos, {}, _TODAY)
    assert [i["kind"] for i in items] == ["Blog", "YouTube"]


def test_post_slug_takes_last_path_segment() -> None:
    assert _post_slug("https://imbue.com/blog/foo-bar") == "foo-bar"
    assert _post_slug("https://imbue.com/blog/foo-bar/") == "foo-bar"
    assert _post_slug("https://imbue.com/blog/foo-bar?utm=1#top") == "foo-bar"


def test_load_post_threads_reads_per_post_layout(tmp_path: Path) -> None:
    post_dir = tmp_path / "threads" / "my-post"
    post_dir.mkdir(parents=True)
    (post_dir / "normies.twitter.json").write_text(json.dumps({"tweets": ["hi"]}))
    (post_dir / "quotes.linkedin.json").write_text(json.dumps({"tweets": ["yo"]}))
    got = _load_post_threads("my-post", tmp_path)
    assert got == {
        "normies": {"twitter": {"tweets": ["hi"]}},
        "quotes": {"linkedin": {"tweets": ["yo"]}},
    }


def test_load_post_threads_missing_dir_is_empty(tmp_path: Path) -> None:
    assert _load_post_threads("nope", tmp_path) == {}


def test_schedule_state_roundtrip(tmp_path: Path) -> None:
    assert _load_schedule_state(tmp_path) == {}
    state = {"id1": {"last_activated": "2026-07-20", "scheduled_date": None, "note": "n"}}
    _save_schedule_state(state, tmp_path)
    assert _load_schedule_state(tmp_path) == state


def test_load_schedule_state_tolerates_garbage(tmp_path: Path) -> None:
    (tmp_path / "schedule_state.json").write_text("{not json")
    assert _load_schedule_state(tmp_path) == {}


def test_is_iso_date() -> None:
    assert _is_iso_date("2026-07-20")
    assert not _is_iso_date("not-a-date")
    assert not _is_iso_date("")


def test_overdue_label() -> None:
    assert _overdue_label(0) == "due today"
    assert _overdue_label(1) == "1 day overdue"
    assert _overdue_label(5) == "5 days overdue"


def test_recommended_post_date_starts_tomorrow_and_staggers() -> None:
    # Item 0 -> tomorrow; each later item is pushed RECOMMEND_SPACING_DAYS out.
    assert _recommended_post_date(0, _TODAY) == datetime(2026, 7, 21).date()
    assert _recommended_post_date(1, _TODAY) == datetime(
        2026, 7, 21 + RECOMMEND_SPACING_DAYS
    ).date()
    assert _recommended_post_date(2, _TODAY) == datetime(
        2026, 7, 21 + 2 * RECOMMEND_SPACING_DAYS
    ).date()


# --- Thread generation: parsing helpers -------------------------------------

_SAMPLE_POST = {
    "title": "A grounded title",
    "text": "The system asks questions before it writes any code.",
    "links": ["https://github.com/imbue-ai/blueprint", "https://discord.gg/example"],
}


def test_strip_code_fences_removes_json_fence() -> None:
    fenced = '```json\n{"tweets": ["a"]}\n```'
    assert _strip_code_fences(fenced) == '{"tweets": ["a"]}'


def test_strip_code_fences_leaves_plain_text() -> None:
    assert _strip_code_fences('  {"tweets": ["a"]}  ') == '{"tweets": ["a"]}'


def test_parse_model_thread_json_twitter_from_dict() -> None:
    raw = '{"tweets": ["one", "two", "three"]}'
    assert _parse_model_thread_json(raw, "twitter") == ["one", "two", "three"]


def test_parse_model_thread_json_tolerates_fences_and_whitespace() -> None:
    raw = '```json\n{"tweets": ["  hook  ", "body"]}\n```'
    assert _parse_model_thread_json(raw, "twitter") == ["hook", "body"]


def test_parse_model_thread_json_accepts_bare_list() -> None:
    assert _parse_model_thread_json('["only", "two"]', "twitter") == ["only", "two"]


def test_parse_model_thread_json_linkedin_collapses_to_single_post() -> None:
    raw = '{"tweets": ["the whole post", "stray extra"]}'
    assert _parse_model_thread_json(raw, "linkedin") == ["the whole post"]


def test_parse_model_thread_json_tweet_collapses_to_single() -> None:
    # A single tweet is a one-element format; drop any over-produced extras.
    raw = '{"tweets": ["the one tweet", "stray extra", "another"]}'
    assert _parse_model_thread_json(raw, "tweet") == ["the one tweet"]


def test_parse_model_thread_json_rejects_invalid_json() -> None:
    with pytest.raises(ThreadGenerationError):
        _parse_model_thread_json("not json at all", "twitter")


def test_parse_model_thread_json_rejects_non_list_tweets() -> None:
    with pytest.raises(ThreadGenerationError):
        _parse_model_thread_json('{"tweets": "just a string"}', "twitter")


def test_parse_model_thread_json_rejects_all_empty_strings() -> None:
    with pytest.raises(ThreadGenerationError):
        _parse_model_thread_json('{"tweets": ["", "   "]}', "twitter")


def test_parse_model_thread_json_drops_non_string_and_blank_items() -> None:
    raw = '{"tweets": ["keep", "", 5, "also keep"]}'
    assert _parse_model_thread_json(raw, "twitter") == ["keep", "also keep"]


# --- Thread generation: prompt building -------------------------------------


def test_build_generation_prompt_grounds_in_post_and_carries_rules() -> None:
    prompt = _build_generation_prompt(
        "researchers", "twitter", "https://imbue.com/blog/blueprint", _SAMPLE_POST
    )
    # Grounded in the post's own title, text, links, and source URL.
    assert _SAMPLE_POST["title"] in prompt
    assert _SAMPLE_POST["text"] in prompt
    assert "https://github.com/imbue-ai/blueprint" in prompt
    assert "https://imbue.com/blog/blueprint" in prompt
    # Carries the mandatory rules and the mission close.
    assert "sentence case" in prompt.lower()
    assert "open source" in prompt
    # Every banned word is listed for the model to avoid.
    for banned in ("leverage", "delve", "seamless", "furthermore"):
        assert banned in prompt


def test_build_generation_prompt_twitter_asks_for_three() -> None:
    prompt = _build_generation_prompt("normies", "twitter", "https://imbue.com/blog/x", _SAMPLE_POST)
    assert "exactly 3 tweets" in prompt


def test_build_generation_prompt_linkedin_asks_for_one_and_scheme_less_links() -> None:
    prompt = _build_generation_prompt("normies", "linkedin", "https://imbue.com/blog/x", _SAMPLE_POST)
    assert "exactly 1 post" in prompt
    assert "scheme-less" in prompt


def test_build_generation_prompt_tweet_asks_for_one_tweet() -> None:
    prompt = _build_generation_prompt("normies", "tweet", "https://imbue.com/blog/x", _SAMPLE_POST)
    assert "exactly 1 tweet" in prompt
    # A single tweet opens with the announcement and ends with the link.
    assert "Produce ONE tweet" in prompt


def test_build_generation_prompt_carries_headline_first_rule() -> None:
    # The headline-first opening applies to every format.
    for format_id in ("tweet", "twitter", "linkedin"):
        prompt = _build_generation_prompt(
            "normies", format_id, "https://imbue.com/blog/x", _SAMPLE_POST
        )
        assert "announcement-style headline" in prompt


def test_format_presets_order_and_labels() -> None:
    # Tabs are ordered Tweet, Thread, LinkedIn; the twitter id keeps its files
    # but is displayed as "Thread".
    assert FORMAT_PRESETS == [
        ("tweet", "Tweet"),
        ("twitter", "Thread"),
        ("linkedin", "LinkedIn"),
    ]


def test_format_publish_tweet_opens_x_composer() -> None:
    # A single tweet publishes through X's composer, same as a twitter thread.
    assert FORMAT_PUBLISH["tweet"]["compose_url"] == "https://x.com/compose/post"
    assert FORMAT_PUBLISH["tweet"]["site"] == "X"


def test_build_generation_prompt_handles_post_without_links() -> None:
    post = {"title": "T", "text": "Body.", "links": []}
    prompt = _build_generation_prompt("quotes", "twitter", "https://imbue.com/blog/x", post)
    assert "(none found)" in prompt


# --- Thread generation: redirect targets ------------------------------------


def test_redirect_after_generate_is_relative_and_carries_error() -> None:
    # Relative "post/<slug>" so it resolves under the /service/thread-writer/
    # proxy, which does not rewrite redirect Location headers.
    response = _redirect_after_generate("blueprint", "the model was unavailable")
    location = response.headers["Location"]
    assert response.status_code == 302
    assert location.startswith("post/blueprint")
    assert "error=the%20model%20was%20unavailable" in location


def test_redirect_after_generate_without_error_has_no_query() -> None:
    response = _redirect_after_generate("blueprint", None)
    assert response.headers["Location"] == "post/blueprint"


# --- Trending links: slug derivation ----------------------------------------

_SUPPORT_URL = (
    "https://support.claude.com/en/articles/16266773-how-claude-marks-ai-generated-content"
)


def test_url_host_strips_scheme_port_and_www() -> None:
    assert _url_host("https://www.Example.com:443/a/b") == "example.com"
    assert _url_host(_SUPPORT_URL) == "support.claude.com"


def test_derive_trending_slug_combines_host_and_last_segment() -> None:
    slug = _derive_trending_slug(_SUPPORT_URL, {})
    # Sanitized host + last meaningful path segment, lowercased, dashed, truncated.
    assert slug.startswith("support-claude-com-16266773-how-claude-marks")
    assert "/" not in slug and "." not in slug
    assert len(slug) <= 60


def test_derive_trending_slug_is_filesystem_safe_and_truncated() -> None:
    # Only the host and the LAST path segment feed the slug (middle segments drop).
    slug = _derive_trending_slug("https://Foo.Bar.com/Path/With Spaces & Symbols!!!", {})
    assert slug == "foo-bar-com-with-spaces-symbols"
    long_url = "https://x.com/" + "a" * 200
    assert len(_derive_trending_slug(long_url, {})) <= 60


def test_derive_trending_slug_falls_back_when_no_path() -> None:
    assert _derive_trending_slug("https://example.com", {}) == "example-com"


def test_derive_trending_slug_reuses_slug_for_same_url() -> None:
    existing = {"example-com-post": {"url": "https://example.com/post"}}
    assert _derive_trending_slug("https://example.com/post", existing) == "example-com-post"


def test_derive_trending_slug_appends_suffix_for_different_url() -> None:
    existing = {"example-com-post": {"url": "https://example.com/post"}}
    # A different URL that would collapse to the same slug gets a numeric suffix.
    assert (
        _derive_trending_slug("https://example.com/post/", existing) == "example-com-post-2"
    )
    existing["example-com-post-2"] = {"url": "https://example.com/post/"}
    assert (
        _derive_trending_slug("https://example.com/post#frag", existing)
        == "example-com-post-3"
    )


# --- Trending links: registry persistence & ordering ------------------------


def test_trending_registry_roundtrip(tmp_path: Path) -> None:
    assert _load_trending(tmp_path) == {}
    registry = {"a-slug": {"url": "https://a.com/x", "title": "A", "added": "2026-08-10"}}
    _save_trending(registry, tmp_path)
    assert _load_trending(tmp_path) == registry


def test_load_trending_tolerates_garbage(tmp_path: Path) -> None:
    (tmp_path / "trending.json").write_text("{not json")
    assert _load_trending(tmp_path) == {}


def test_trending_entries_newest_first_orders_by_added_date() -> None:
    registry = {
        "old": {"url": "u1", "title": "old", "added": "2026-08-01"},
        "new": {"url": "u2", "title": "new", "added": "2026-08-11"},
        "mid": {"url": "u3", "title": "mid", "added": "2026-08-05"},
    }
    assert [slug for slug, _ in _trending_entries_newest_first(registry)] == ["new", "mid", "old"]


# --- External vs imbue: template selection & close --------------------------


def test_close_directive_imbue_invokes_mission_and_cta() -> None:
    close = _close_directive(False, "https://imbue.com/blog/x")
    assert "Imbue's mission" in close
    assert "open source" in close
    assert "https://imbue.com/blog/x" in close


def test_close_directive_external_is_commentary_with_no_pitch() -> None:
    close = _close_directive(True, "https://support.claude.com/a")
    # External commentary must not invoke the mission or pitch Imbue products.
    assert "mission" not in close.lower() or "do not" in close.lower()
    assert "open source" not in close
    assert "Bouncer" in close  # explicitly names what NOT to pitch
    assert "https://support.claude.com/a" in close


def test_build_generation_prompt_external_uses_commentary_template() -> None:
    prompt = _build_generation_prompt(
        "normies", "twitter", "https://support.claude.com/a", _SAMPLE_POST, is_external=True
    )
    assert "COMMENTS on the linked content" in prompt
    assert "do not pitch imbue" in prompt.lower()
    assert "open source" not in prompt  # no mission close for external content
    assert "https://support.claude.com/a" in prompt


def test_build_generation_prompt_imbue_still_uses_mission_close() -> None:
    prompt = _build_generation_prompt(
        "normies", "twitter", "https://imbue.com/blog/x", _SAMPLE_POST, is_external=False
    )
    assert "social copy for Imbue" in prompt
    assert "open source" in prompt


def test_resolve_source_trending_slug_is_external(tmp_path: Path) -> None:
    _save_trending({"ext-slug": {"url": "https://support.claude.com/a"}}, tmp_path)
    assert _resolve_source("ext-slug", tmp_path) == ("https://support.claude.com/a", "external")


def test_resolve_source_notes_slug_is_notes(tmp_path: Path) -> None:
    # A slug present in the drafts registry (but not trending) resolves to a
    # notes draft, which has no source URL and uses the notes template.
    _save_drafts({"my-draft": {"title": "My draft", "notes": "- a point"}}, tmp_path)
    assert _resolve_source("my-draft", tmp_path) == ("", "notes")


def test_resolve_source_unknown_slug_is_imbue_blog(tmp_path: Path) -> None:
    assert _resolve_source("blueprint", tmp_path) == (f"{BLOG_URL}/blueprint", "imbue")


# --- External content: title extraction -------------------------------------


def test_extract_external_title_prefers_og_title() -> None:
    html_text = (
        '<html><head><meta property="og:title" content="OG   Title">'
        "<title>Doc Title</title></head><body><h1>H1 Title</h1></body></html>"
    )
    assert _extract_external_title(html_text) == "OG Title"


def test_extract_external_title_falls_back_to_title_then_h1() -> None:
    assert (
        _extract_external_title("<html><head><title>Doc Title</title></head></html>")
        == "Doc Title"
    )
    assert _extract_external_title("<html><body><h1>Just an H1</h1></body></html>") == "Just an H1"


def test_extract_external_title_empty_when_nothing_present() -> None:
    assert _extract_external_title("<html><body><p>text</p></body></html>") == ""


# --- Notes drafts: title & slug derivation ----------------------------------


def test_draft_title_from_notes_uses_first_non_empty_line() -> None:
    notes = "\n\n  Launching Widget 3.0 today  \nsecond line\n"
    assert _draft_title_from_notes(notes) == "Launching Widget 3.0 today"


def test_draft_title_from_notes_strips_leading_bullet_markers() -> None:
    assert _draft_title_from_notes("- We shipped the thing\n- and more") == "We shipped the thing"
    assert _draft_title_from_notes("* first\n* second") == "first"
    assert _draft_title_from_notes("• bulleted point") == "bulleted point"


def test_draft_title_from_notes_truncates_to_max() -> None:
    long_line = "x" * 200
    assert len(_draft_title_from_notes(long_line)) <= 60


def test_draft_title_from_notes_empty_when_blank() -> None:
    assert _draft_title_from_notes("\n\n   \n") == ""


def test_derive_draft_slug_is_filesystem_safe_and_truncated() -> None:
    slug = _derive_draft_slug("Launching Widget 3.0 today!!!", {})
    assert slug == "launching-widget-3-0-today"
    assert "/" not in slug and "." not in slug
    assert len(_derive_draft_slug("z" * 200, {})) <= 60


def test_derive_draft_slug_falls_back_when_title_has_no_alphanumerics() -> None:
    assert _derive_draft_slug("!!! ???", {}) == "draft"


def test_derive_draft_slug_appends_suffix_on_collision() -> None:
    # Every "Draft it" is a fresh entry, so a title that collapses to an existing
    # slug gets a numeric suffix rather than clobbering the earlier draft.
    existing = {"my-note": {"title": "My note"}}
    assert _derive_draft_slug("My note", existing) == "my-note-2"
    existing["my-note-2"] = {"title": "My note"}
    assert _derive_draft_slug("My note", existing) == "my-note-3"


# --- Notes drafts: registry persistence & ordering --------------------------


def test_drafts_registry_roundtrip(tmp_path: Path) -> None:
    assert _load_drafts(tmp_path) == {}
    registry = {"a-draft": {"title": "A", "notes": "- one\n- two", "added": "2026-08-19"}}
    _save_drafts(registry, tmp_path)
    assert _load_drafts(tmp_path) == registry


def test_load_drafts_tolerates_garbage(tmp_path: Path) -> None:
    (tmp_path / "drafts.json").write_text("{not json")
    assert _load_drafts(tmp_path) == {}


def test_drafts_entries_newest_first_orders_by_added_date() -> None:
    registry = {
        "old": {"title": "old", "notes": "n", "added": "2026-08-01"},
        "new": {"title": "new", "notes": "n", "added": "2026-08-11"},
        "mid": {"title": "mid", "notes": "n", "added": "2026-08-05"},
    }
    assert [slug for slug, _ in _drafts_entries_newest_first(registry)] == ["new", "mid", "old"]


# --- Notes drafts: template selection ---------------------------------------

_SAMPLE_NOTES = {
    "title": "We shipped offline mode",
    "text": "- offline mode works now\n- syncs when you reconnect\n- no account needed",
    "links": [],
}


def test_close_directive_notes_has_no_mission_cta_or_source_link() -> None:
    close = _close_directive(False, "", is_notes=True)
    # A notes draft ends naturally with no mission, no pitch, and no source link.
    assert "open source" not in close
    assert "source link" in close.lower() and "do not append" in close.lower()


def test_build_generation_prompt_notes_uses_notes_template() -> None:
    prompt = _build_generation_prompt("normies", "twitter", "", _SAMPLE_NOTES, is_notes=True)
    # Notes are the source; the bullets are carried in and no mission close is added.
    assert "notes ARE the source" in prompt
    assert _SAMPLE_NOTES["text"] in prompt
    assert "open source" not in prompt
    assert "do not append a source link" in prompt.lower()
    # The shared voice rules still apply.
    assert "sentence case" in prompt.lower()
    assert "announcement-style headline" in prompt


def test_build_generation_prompt_notes_respects_format_counts() -> None:
    for format_id, expected in (("tweet", "exactly 1 tweet"), ("twitter", "exactly 3 tweets"),
                                ("linkedin", "exactly 1 post")):
        prompt = _build_generation_prompt("normies", format_id, "", _SAMPLE_NOTES, is_notes=True)
        assert expected in prompt


# --- Per-voice example corpus loading + prompt caching -----------------------

# A tiny synthetic corpus that exercises every decoration the parser strips: a
# ``[#channel]`` Slack line, a ``[url]`` HN line, a quoted snippet with a
# trailing `` -- <url>`` attribution, and a quoted snippet with a trailing
# ``(date)``. It also includes a ``- **metadata**`` bullet and source bullets
# before the first section (both must be ignored) plus a trailing
# ``## Voice profile`` section whose bullets are style notes, not examples.
_SYNTHETIC_CORPUS = """# Some voice — verbatim corpus

**Sources:**
- Source list bullet that must be skipped: https://example.com/x

## Slack (verbatim)
### #channel-one
- [#channel-one] first real example message here
- [https://blog.example.com/post] a passage copied verbatim from a blog

## Snippets (verbatim)
- "a quoted snippet with attribution" — https://x.com/someone/status/1
- "a quoted snippet with a date" (Jan 1, 2025)
- **Not an example, this is metadata** — https://example.com

## Voice profile
- This is a style note and must NOT be collected as an example.
"""


def test_parse_corpus_examples_strips_decorations_and_skips_non_examples(tmp_path: Path) -> None:
    path = tmp_path / "voice.md"
    path.write_text(_SYNTHETIC_CORPUS)
    examples = _parse_corpus_examples(path)
    assert examples == [
        "first real example message here",
        "a passage copied verbatim from a blog",
        "a quoted snippet with attribution",
        "a quoted snippet with a date",
    ]
    # The source list, the ``- **metadata**`` bullet, and every Voice profile
    # style note are excluded.
    joined = "\n".join(examples)
    assert "Source list bullet" not in joined
    assert "metadata" not in joined
    assert "style note" not in joined


def test_parse_corpus_examples_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _parse_corpus_examples(tmp_path / "does-not-exist.md") == []


def test_clean_corpus_line_handles_each_decoration() -> None:
    assert _clean_corpus_line("- [#capabilities] can I get a review?") == "can I get a review?"
    assert _clean_corpus_line("- [https://ex.com/a] a verbatim passage") == "a verbatim passage"
    assert _clean_corpus_line('- "a quote" — https://x.com/1') == "a quote"
    assert _clean_corpus_line('- "a dated quote" (Apr 14, 2025)') == "a dated quote"
    assert _clean_corpus_line("- a plain slack line with no decoration") == (
        "a plain slack line with no decoration"
    )


def test_build_voice_corpus_block_builds_non_empty_for_normies_and_researchers(
    tmp_path: Path,
) -> None:
    # The real corpora live under gitignored runtime/, so build against synthetic
    # files placed at the expected per-voice filenames instead.
    (tmp_path / "normies_examples.md").write_text(_SYNTHETIC_CORPUS)
    (tmp_path / "swyx.md").write_text(_SYNTHETIC_CORPUS)
    (tmp_path / "researchers_examples.md").write_text(_SYNTHETIC_CORPUS)
    (tmp_path / "researchers.md").write_text(_SYNTHETIC_CORPUS)
    for voice_id in ("normies", "researchers"):
        block = _build_voice_corpus_block(voice_id, corpus_dir=tmp_path)
        assert block
        # The block is framed as a style reference and carries the parsed examples.
        assert block.startswith("Below is a large sample of real writing")
        assert "first real example message here" in block


def test_build_voice_corpus_block_empty_when_no_files(tmp_path: Path) -> None:
    assert _build_voice_corpus_block("normies", corpus_dir=tmp_path) == ""


def test_quotes_voice_has_no_corpus_and_stays_uncached() -> None:
    # The quotes voice is verbatim-from-the-post: it must never carry a cached
    # corpus and always runs on the plain, uncached completion path.
    assert _voice_corpus_block("quotes") is None
    content = _generation_message_content("the task prompt", _voice_corpus_block("quotes"))
    assert content == "the task prompt"


def test_generation_message_content_marks_corpus_block_as_cached_prefix() -> None:
    content = _generation_message_content("the task prompt", "the big voice corpus")
    # The corpus is the FIRST content block and is marked cache_control ephemeral;
    # the small task prompt follows as an uncached second block.
    assert isinstance(content, list)
    assert content[0]["text"] == "the big voice corpus"
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[1]["text"] == "the task prompt"
    assert "cache_control" not in content[1]
