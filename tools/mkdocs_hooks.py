"""Build-time link rewriting, so one set of sources serves GitHub and the site.

Every page under ``docs/`` is read in two places: on GitHub, where contributors
browse the repo, and on the published site. Most links work in both, because
most targets are other pages under ``docs/``. The ones that do not are the
links that leave the tree — ``../CONTRIBUTING.md``,
``../bench/results/latest.jsonl``, ``../examples/walkthrough.out``. Those are
correct on GitHub and 404s on the site, which has no such files.

The fix is a rule, not an edit: resolve every relative link against the page
that contains it, and if the result escapes ``docs/``, point it at the file on
GitHub instead. Sources stay written for the repo — nobody has to remember a
second convention when adding a page — and a link that leaves the tree cannot
silently become a dead one, because ``mkdocs build --strict`` fails on the
alternative.

``tests/test_docs_site.py`` pins the rule table.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

#: A markdown inline link's target, plus the optional title that may follow it:
#: ``](target)`` and ``](target "title")``. Reference definitions
#: (``[label]: target``) are matched by :data:`_REFERENCE` below.
_INLINE = re.compile(r'\]\(\s*(?P<target>[^)\s]+)(?P<title>\s+"[^"]*")?\s*\)')

#: ``[label]: target`` at the head of a line — the other way markdown names a
#: destination. Rewriting only inline links would leave these behind.
_REFERENCE = re.compile(r'^(?P<head>\[[^\]]+\]:\s+)(?P<target>\S+)', re.MULTILINE)

#: Left alone: already absolute, a bare fragment, or a protocol we do not resolve.
_ABSOLUTE = re.compile(r'^([a-z][a-z0-9+.-]*:|//|#|/)', re.IGNORECASE)


def _rewrite(target: str, page_dir: str, blob: str) -> str | None:
    """The site URL for ``target`` as written on a page in ``page_dir``, or None.

    None means "leave it exactly as it is" — the link is absolute, or it
    resolves to something that is still inside ``docs/`` and therefore still
    part of the site.
    """
    if _ABSOLUTE.match(target):
        return None
    path, sep, fragment = target.partition('#')
    if not path:
        return None
    # posixpath, not pathlib: markdown link targets are URLs, whose separator is
    # `/` on every platform this may build on.
    resolved = posixpath.normpath(posixpath.join('docs', page_dir, path))
    if resolved == 'docs' or resolved.startswith('docs/'):
        return None
    return f'{blob}/{resolved}{sep}{fragment}'


def on_page_markdown(markdown: str, *, page: Any, config: Any, **_: Any) -> str:
    """mkdocs hook: rewrite repo-relative links on every page of the site."""
    repo_url = str(config['repo_url']).rstrip('/')
    blob = f'{repo_url}/blob/main'
    page_dir = posixpath.dirname(str(page.file.src_uri))

    def inline(match: re.Match[str]) -> str:
        url = _rewrite(match['target'], page_dir, blob)
        title = match['title'] or ''
        return match[0] if url is None else f']({url}{title})'

    def reference(match: re.Match[str]) -> str:
        url = _rewrite(match['target'], page_dir, blob)
        return match[0] if url is None else f'{match["head"]}{url}'

    return _REFERENCE.sub(reference, _INLINE.sub(inline, markdown))
