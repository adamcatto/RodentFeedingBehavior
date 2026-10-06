"""The user manual: Markdown chapters in ``docs/manual`` rendered for the in-app Help page.

Chapters are ``NN-slug.md`` files (``NN`` sets the order); images live in
``docs/manual/images``. The same files read well on GitHub and are combined
into the Word manual by ``scripts/build_manual.py``. In an installed package
the folder is shipped as ``feeding/_docs/manual``.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

from .config import PACKAGE_ROOT, PROJECT_ROOT

_CHAPTER = re.compile(r"^(\d+)-([a-z0-9-]+)\.md$")


def docs_dir() -> Path:
    for d in (PROJECT_ROOT / "docs" / "manual", PACKAGE_ROOT / "_docs" / "manual"):
        if d.is_dir():
            return d
    return PROJECT_ROOT / "docs" / "manual"


def chapters() -> list[dict]:
    """[{slug, file, title}] in reading order."""
    out = []
    d = docs_dir()
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        m = _CHAPTER.match(p.name)
        if not m:
            continue
        first = next((ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), f"# {m[2]}")
        out.append({"slug": m[2], "file": p.name, "title": first[2:].strip()})
    return out


def slugify(value: str, separator: str = "-") -> str:
    """GitHub-style heading ids, so in-page links work on GitHub and in the app alike."""
    value = re.sub(r"<[^>]+>", "", value).strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return value.replace(" ", separator)


@lru_cache(maxsize=64)
def _render(path: str, mtime: float) -> dict:
    import markdown

    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "attr_list", "sane_lists", "md_in_html"],
                           extension_configs={"toc": {"slugify": slugify, "toc_depth": "2-3"}})
    text = Path(path).read_text(encoding="utf-8")
    body = md.convert(text)
    by_file = {c["file"]: c["slug"] for c in chapters()}

    def link(m):
        attr, url = m[1], m[2]
        if re.match(r"^[a-z]+:|^#|^/", url):
            return m[0]
        file, _, anchor = url.partition("#")
        if file in by_file:  # another chapter: an in-app route
            return f'{attr}="#help/{by_file[file]}{"/" + anchor if anchor else ""}"'
        return f'{attr}="/docs/manual/{url}"'  # an image or other file next to the chapters

    body = re.sub(r'(href|src)="([^"]+)"', link, body)
    toc = [{"id": t["id"], "text": html.unescape(t["name"]), "level": t["level"]} for t in _flatten(md.toc_tokens)]
    plain = re.sub(r"<[^>]+>", " ", body)
    return {"html": body, "toc": toc, "text": html.unescape(re.sub(r"\s+", " ", plain))}


def _flatten(tokens: list[dict]) -> list[dict]:
    out = []
    for t in tokens:
        out.append(t)
        out += _flatten(t.get("children", []))
    return out


def chapter(slug: str) -> dict | None:
    for c in chapters():
        if c["slug"] == slug:
            p = docs_dir() / c["file"]
            return {**c, **_render(str(p), p.stat().st_mtime)}
    return None


def search(query: str, limit: int = 30) -> list[dict]:
    """Chapters and sections containing every word of ``query``, with a snippet."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    hits = []
    for c in chapters():
        d = chapter(c["slug"])
        # split the chapter into sections at h2/h3 headings
        parts = re.split(r'<h([23]) id="([^"]+)">(.*?)</h\1>', d["html"])
        # re.split with 3 groups: [text, level, id, title, text, level, id, title, text, ...]
        sections = [("", c["title"], parts[0])] + [(parts[i + 2], re.sub(r"<[^>]+>", "", parts[i + 3]), parts[i + 4])
                                                   for i in range(0, len(parts) - 1, 4)]
        for anchor, title, chunk in sections:
            text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk)))
            low = (title + " " + text).lower()
            if all(w in low for w in words):
                i = max(0, text.lower().find(words[0]) - 60)
                hits.append({"slug": c["slug"], "chapter": c["title"], "anchor": anchor, "section": html.unescape(title),
                             "snippet": ("…" if i else "") + text[i:i + 180].strip() + "…",
                             "score": sum(low.count(w) for w in words) + 5 * sum(w in title.lower() for w in words)})
    return sorted(hits, key=lambda h: -h["score"])[:limit]
