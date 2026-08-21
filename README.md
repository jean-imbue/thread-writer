# Thread Writer

A multi-voice, multi-format generator that turns blog posts, links, or bullet points into X threads and LinkedIn posts, with a schedule queue and a cached voice-corpus mechanism.

Thread Writer is a small Flask web app -- a Typefully-style reader -- that turns
a source into ready-to-post social copy. Feed it one of your own blog posts, any
trending link you paste in, or a few bullet points, and it drafts the copy in
your choice of three voices (Normies, Researchers, Quotes) and three formats (a
single Tweet, a Thread of tweets, or a long-form LinkedIn post), with one-click
Generate and Regenerate for any voice-and-format combination. A Schedule view
tracks what is due to post next on a recommended cadence and shows a calendar of
what has already gone out. Publishing never auto-posts: it copies the text and
opens X's or LinkedIn's composer so you review and send it yourself. Each voice
is grounded in a large corpus of real example writing, sent to the model as a
cached prompt prefix so drafts imitate a genuine register rather than a
one-line description of one.

This repository is a published **minds inspiration**: a clean, bootable
snapshot of the apps and features a mind built, ready to adapt into your own.
It is NOT the generic workspace template -- it is this specific project.

## Use it

- **Create a new mind from it:** point a new minds workspace at this repo's
  URL. On first boot the mind reads the inspiration and helps you connect your
  own accounts and adapt it.
- **Bring it into an existing mind:** run `/use-inspiration <this repo's URL>`.

## What's inside

- **Thread Writer** -- [`inspiration-thread-writer.md`](inspiration-thread-writer.md) (published now)

Each `inspiration-<slug>.md` is the full manifest for that inspiration: what
it is, how it works, the prerequisites it needs, and how to adapt it.
