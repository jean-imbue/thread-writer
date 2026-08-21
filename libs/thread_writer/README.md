# thread-writer

Turn Imbue blog posts into X threads and read them in a Typefully-style view.

## Trending links

Besides Imbue's own blog posts, you can paste ANY link (a trending article, a
support doc, a competitor's announcement) into the input at the top of the
Schedule view and generate a thread that COMMENTS on it. Trending threads use a
different close than blog posts: they never invoke Imbue's mission or pitch
Imbue, they close with a plain link to the source and a short genuine take, and
they stay grounded only in the fetched text.

Pasted links are recorded in `runtime/thread-writer/trending.json`, keyed by a
stable, filesystem-safe slug derived from the URL, and are listed (newest first)
in the Schedule view's "Trending" section. Each saved link opens its own reader
at `/post/<slug>`, where the Voice/Format tabs and the Generate/Regenerate
buttons all work exactly as they do for blog posts (the slug is resolved through
the trending registry first, so the external commentary template is used).
