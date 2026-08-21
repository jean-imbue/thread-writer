---
title: Thread Writer
description: A multi-voice, multi-format generator that turns blog posts, links, or bullet points into X threads and LinkedIn posts, with a schedule queue and a cached voice-corpus mechanism.
thumbnail: inspiration-thread-writer.svg
version: v1
format: v1
---

# Thread Writer

This file is the manifest for the **Thread Writer** inspiration (slug:
`thread-writer`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A multi-voice, multi-format generator that turns blog posts, links, or bullet points into X threads and LinkedIn posts, with a schedule queue and a cached voice-corpus mechanism.

Thread Writer solves the "we published something, now someone has to turn it
into social posts" problem. It is a small web app (a Typefully-style reader)
that takes a source -- one of your own blog posts, any trending link you paste
in, or a few bullet points you jot down -- and drafts ready-to-post social copy
from it. Every draft can be written in one of three **voices** (Normies,
Researchers, or Quotes) and one of three **formats** (a single Tweet, a Thread
of tweets, or a long-form LinkedIn post), and any voice-and-format combination
can be generated or regenerated on demand with one click. When it is running the
user opens a tab and sees two surfaces: a **reader** that renders a chosen
draft as a stack of tweet cards with live character counts, a voice dropdown, a
format tab toggle, and a Publish button; and a **Schedule** view with an "Up
next" queue of what is due to post (each with a recommended, staggered date), a
month calendar of published posts, and paste-a-link / paste-your-notes inputs.
Publishing never auto-posts -- it copies the text to the clipboard and opens X's
or LinkedIn's composer so the user reviews and sends it themselves. The voices
are not one-line style hints -- each is grounded in a large corpus of real
example writing that is sent to the model as a cached prompt prefix, so drafts
imitate a genuine register rather than a description of one.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `libs/thread_writer`
- `supervisord.conf`
- `pyproject.toml`

**`libs/thread_writer`** is the whole app: a single-file Flask service
(`src/thread_writer/runner.py`) plus its `README.md` and `pyproject.toml`. The
one module holds everything -- the HTML/CSS/JS for both the reader and the
Schedule pages (rendered as f-strings, no templates), the blog/link/YouTube
fetching and parsing, the on-demand generation pipeline, and the small JSON
registries that persist state. There is no separate frontend build; the pages
are server-rendered and the little client-side JS only switches voice/format
tabs and drives the copy/publish buttons.

**`supervisord.conf`** runs the app. The `[program:thread-writer]` stanza
launches `uv run thread-writer` (the `thread-writer` console script defined in
the lib's `pyproject.toml`, which calls `runner:main`). `main()` serves the
Flask app on `127.0.0.1:8080` with the threaded Werkzeug server -- threaded
because generation calls are synchronous and can take ~10-30s, so the server
must absorb the wait without blocking other requests. The same command line
first runs `python3 scripts/forward_port.py --url http://localhost:8080 --name
thread-writer`, which registers the service so it shows up as a tab in the
workspace UI, and wraps everything in `scripts/oom_tag_service.py` so the
OOM-prevention daemon treats it as a sheddable user service. Port and data
directory are overridable via `THREAD_WRITER_PORT` and `THREAD_WRITER_DATA_DIR`.

**`pyproject.toml`** (the workspace root) carries `thread-writer` as a workspace
member and dependency so `uv run thread-writer` resolves. It also pins the
runtime deps the app needs (flask, httpx, beautifulsoup4, litellm, openai,
loguru, werkzeug).

At runtime everything hangs off `DATA_DIR` (defaults to `runtime/thread-writer/`):
generated drafts live at `DATA_DIR/threads/<slug>/<voice>.<format>.json` (one
file per voice+format, all the same JSON shape, a LinkedIn post being a
single-element `tweets` array); pasted links are recorded in
`DATA_DIR/trending.json`; pasted notes in `DATA_DIR/drafts.json`;
mark-as-posted / scheduling state in `DATA_DIR/schedule_state.json`; and the
per-voice example corpora are read from `DATA_DIR/voices/*.md`. Generation
(`/generate`, `/trending`, `/draft`) fetches or reads the source text, builds a
voice- and format-specific prompt grounded only in that text, calls the model
through litellm, parses strict JSON back into tweet strings, and writes the
per-post file; the reader then renders it. The voice corpus for a voice is
parsed and assembled once at import into a byte-identical block and injected as
a `cache_control: ephemeral` prefix so Anthropic's prompt cache bills it once
and serves it from cache on later calls.

## Recipe

This inspiration is version `v1` (front-matter `version:`).
It is not a fork of the workspace it came from -- it is DERIVED from it by the
recipe below: include these paths, leave these out, apply these
published-version rules. An update re-runs the recipe against the current
workspace and publishes the result as the next version, so anything excluded
here stays excluded even though it still exists in the source workspace. This
block is the durable home of that recipe -- a later update reads it back from
here.

```yaml
version: v1
include:
  - libs/thread_writer
  - supervisord.conf
  - pyproject.toml
data_include: []
exclude:
  - libs/pigeon_post (the workspace's other, unrelated app; not part of this inspiration)
  - runtime/thread-writer/voices/ (the per-voice example corpora; runtime state, and contained the author's private writing samples)
  - runtime/thread-writer/threads/ (generated drafts; personal runtime output)
  - runtime/thread-writer/*.json (the trending, drafts, and schedule-state registries; personal runtime state)
modification_rules:
  - remove the [program:pigeon-post] stanza from supervisord.conf so only the thread-writer service ships
  - remove the three pigeon-post references from pyproject.toml (the [project].dependencies entry, the [tool.uv.workspace].members entry, and the [tool.uv.sources] line)
```

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by `requires_`):

- requires_llm: calls Claude for generation via the KEYED litellm path
  (`litellm.completion`, reading `ANTHROPIC_API_KEY` and routing through the
  Anthropic-compatible proxy at `ANTHROPIC_BASE_URL`), using model
  `anthropic/claude-fable-5` as the primary writer with `anthropic/claude-opus-4-8`
  as the fallback. An adopter on the keyless subscription path (`claude -p`) must
  switch the model calls in `_complete_generation` per the use-ai-integration
  skill. This is the only hard requirement to produce any draft.
- requires_permission: to rebuild the "Normies" voice corpus, the app expects
  scraped writing samples under `DATA_DIR/voices/`; gathering the original
  Imbue-style samples used slack-api / slack-read-all (user-approved; the
  adopting agent initiates this via a latchkey permission request during setup
  if the user wants Slack-sourced samples). This is optional -- any writing
  corpus works, and with no corpus files the voices fall back to built-in inline
  guidance -- so it is only required if the adopter wants richly grounded voices
  from their own Slack.

Blog/link fetching and the X/LinkedIn "publish" step need NO special auth:
fetching is a plain public HTTP GET, and publishing only copies text and opens a
composer in the browser (it never posts, so no X/LinkedIn credentials are used).

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the `use-inspiration` skill's template path; in short:

1. Read this entire file first, especially "Prerequisites" and "Holes"
   below -- Prerequisites are your SETUP agenda, Holes are your ADAPTATION
   agenda.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the Prerequisites).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the minds app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each hole interactively, one at a time. Translate each into
   plain language, ask for a decision only when you genuinely need one, and
   resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

This app is **Imbue-specific by design** and was intentionally NOT generalized:
the original author explicitly wanted the shipped behavior kept as-is rather than
abstracted into config, so adaptation means editing the code, not flipping
switches. The holes below are the concrete places an adopter rewires it for
their own project.

- **Blog source is hardcoded to imbue.com.** `BLOG_URL` is
  `https://imbue.com/blog` and `_parse_blog_posts` selects on imbue.com's exact
  markup (`div.font-mono` dates, `div.font-display` titles, `a[href*='/blog/']`).
  An adopter points it at their own blog and rewrites the parser to match their
  listing's HTML (or, if they only ever use the paste-a-link and paste-notes
  inputs, they can ignore the blog listing entirely -- those two paths work with
  no blog at all).
- **Threads close on Imbue's mission.** In `_close_directive`, an own-blog post
  closes on "Imbue builds software that is open source, runs on your own device,
  and that you own" plus a CTA to the writeup/repo. An adopter edits that closing
  sentence to their own mission/CTA (the external-link and notes closes are
  already neutral and need no change).
- **Voice guidance and voices are tuned to Imbue.** `_VOICE_GUIDANCE` names swyx
  and the Imbue Slack (Normies), Andrew Ng and Yann LeCun (Researchers); the
  voice dropdown labels say "swyx + imbumans" and "Ng + LeCun". An adopter edits
  `VOICE_PRESETS` and `_VOICE_GUIDANCE` to describe their own target registers.
- **The voice example corpora are NOT shipped.** The large per-voice sample
  files under `DATA_DIR/voices/` (`normies_examples.md`, `swyx.md`,
  `researchers_examples.md`, `researchers.md`) lived in runtime state and
  contained the author's private writing samples, so they were deliberately
  excluded. Out of the box the corpus loader finds no files and every voice
  falls back to its built-in inline `_VOICE_GUIDANCE` on the uncached path --
  generation still works, just without the "1000-shot" grounding. To restore the
  richer voices, the adopter drops their own markdown corpus files under
  `DATA_DIR/voices/` matching the `_VOICE_CORPUS_FILES` names (verbatim example
  bullets under `## ` sections; a trailing `## Voice profile` section is ignored).
- **The LLM path is the keyed litellm path.** `_complete_generation` /
  `_GENERATION_MODELS` call `litellm.completion` against `ANTHROPIC_API_KEY` +
  `ANTHROPIC_BASE_URL` with `anthropic/claude-fable-5` (primary) and
  `anthropic/claude-opus-4-8` (fallback). An adopter whose mind uses the keyless
  subscription path (`claude -p`) must switch these calls per the
  use-ai-integration skill -- this is a code change, not config.
- **The Schedule "revive" queue is hardcoded to Imbue's YouTube channel.** The
  "Up next" queue also surfaces Imbue videos due for a re-share, resolved from
  `YOUTUBE_HANDLE_URL = https://www.youtube.com/@imbue_ai`. An adopter points
  this at their own channel handle or removes the YouTube half of the queue if
  they do not want it (the blog half of the queue is independent).
- **`uv.lock` is the clean-base lock.** The snapshot's lockfile is the template
  base's, not the source mind's, so after adoption run `uv sync --all-packages`
  to resolve the thread-writer dependency tree before first launch.

## Publication history

This inspiration's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-08-21) -- Initial publish of the Thread Writer app: multi-voice (Normies / Researchers / Quotes) x multi-format (Tweet / Thread / LinkedIn) generation from blog posts, pasted links, or notes, with a Schedule queue, calendar, on-demand Generate/Regenerate, copy-and-open Publish, and the cached per-voice example-corpus mechanism.

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
