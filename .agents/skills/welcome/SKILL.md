---
name: welcome
description: Greet the user when a new project starts. This mind was created from the "Thread Writer" inspiration, so the welcome introduces that inspiration and immediately starts the adaptation conversation.
---

# Welcome the user (inspiration: Thread Writer)

This mind was created from an inspiration -- a published snapshot of apps
another mind built:

- Title: Thread Writer
- Slug: `thread-writer`
- Description: A multi-voice, multi-format generator that turns blog posts, links, or bullet points into X threads and LinkedIn posts, with a schedule queue and a cached voice-corpus mechanism.
- Manifest: `inspiration-thread-writer.md` (at the repo root)

Do ALL of the following in your FIRST response, in the same turn, without
waiting to be asked:

1. Open with a short CUSTOM welcome that names **Thread Writer** and gives the
   one-line description above. Do NOT use a generic "Welcome to Minds"
   greeting and do NOT offer a generic suggestions list.
2. Immediately read `inspiration-thread-writer.md` at the repo root (reading the
   manifest in the first turn is required).
3. In plain, non-technical language, present what the inspiration is and
   what it needs from the user -- name the manifest's "Prerequisites" (the
   connectors/permissions it runs on). Then ask whether they want to hook it
   up to their own accounts now (e.g. "Want me to connect this to your own
   Slack?"). End your first response on THAT question. This is the
   `use-inspiration` skill's template path; the manifest's "How to adapt
   it" section is the full script: if they say yes, ACTIVATE FIRST -- initiate
   each `requires_permission` via a latchkey permission request, get the
   app showing THEIR OWN DATA (that is the definition of working; a running
   service is not), invite them to take a look -- and only then ask how they
   want to adapt it.

If this repo has accumulated several `inspiration-*.md` manifests, the one
named above is the latest; treat the others as reference (they were likely
already adapted upstream).
