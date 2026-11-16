#!/usr/bin/env python3
"""Export the local WordPress site to static files ready for S3.

Crawls the site from its home page, follows same-origin links and assets
(href, src, srcset, CSS url()), rewrites the local origin to the public one,
and writes everything under OUT_DIR. Standard library only.

    scripts/export.py --source http://isaacinthecloud.test --site https://isaacinthecloud.com --out dist
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Never fetch or publish WordPress internals; the static site has no backend.
SKIP = re.compile(r"^/(wp-admin|wp-json|wp-login\.php|xmlrpc\.php|feed|comments|wp-sitemap|\?)")
ATTR_URL = re.compile(r"""(?:href|src|poster|data-src)\s*=\s*["']([^"']+)["']""", re.I)
SRCSET = re.compile(r"""srcset\s*=\s*["']([^"']+)["']""", re.I)
CSS_URL = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.I)
TEXT_TYPES = ("text/", "application/javascript", "application/json", "image/svg+xml", "application/xml")


def fetch(url: str) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "static-export/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get_content_type(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get_content_type(), e.read()


def local_path(out: Path, path: str, ctype: str) -> Path:
    """Map a URL path to a file. Pages become <dir>/index.html; query strings are dropped."""
    path = urllib.parse.unquote(path)
    if path.endswith("/") or (ctype == "text/html" and "." not in path.rsplit("/", 1)[-1]):
        path = path.rstrip("/") + "/index.html"
    dest = (out / path.lstrip("/")).resolve()
    if out.resolve() not in dest.parents:
        raise ValueError(f"refusing to write outside {out}: {path}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="local site origin, e.g. http://isaacinthecloud.test")
    ap.add_argument("--site", required=True, help="public origin, e.g. https://isaacinthecloud.com")
    ap.add_argument("--out", default="dist", help="output directory (replaced on each run)")
    a = ap.parse_args()

    src = a.source.rstrip("/")
    site = a.site.rstrip("/")
    src_host = urllib.parse.urlsplit(src).netloc
    out = Path(a.out)
    abs_url = re.compile(re.escape(src).replace("/", r"\\?/") + r"""[^"'\s<>)]*""")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    def rewrite(text: str) -> str:
        # Plain, JSON-escaped (http:\/\/host) and protocol-relative forms of the local origin.
        for old, new in (
            (src, site),
            (src.replace("/", "\\/"), site.replace("/", "\\/")),
            ("//" + src_host, "//" + urllib.parse.urlsplit(site).netloc),
        ):
            text = text.replace(old, new)
        return text

    def links(text: str, base: str, ctype: str) -> set[str]:
        found: set[str] = set()
        if ctype == "text/html":
            found.update(html.unescape(u) for u in ATTR_URL.findall(text))
            for group in SRCSET.findall(text):
                found.update(part.strip().split(" ")[0] for part in html.unescape(group).split(","))
        found.update(CSS_URL.findall(text))
        # Absolute local URLs anywhere (meta content, JSON-LD, inline scripts), plain or JSON-escaped.
        found.update(m.replace("\\/", "/") for m in abs_url.findall(text))
        result = set()
        for u in found:
            u = u.strip().replace("\\/", "/")
            if not u or u.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                continue
            absu = urllib.parse.urljoin(base, u)
            parts = urllib.parse.urlsplit(absu)
            if parts.netloc != src_host or SKIP.match(parts.path or "/"):
                continue
            result.add(urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", "")))
        return result

    queue, seen, written = [src + "/"], set(), 0
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        status, ctype, body = fetch(url)
        if status != 200:
            print(f"  ! {status} {url}", file=sys.stderr)
            if url == src + "/":
                return 1
            continue
        path = urllib.parse.urlsplit(url).path or "/"
        dest = local_path(out, path, ctype)
        if ctype.startswith(TEXT_TYPES):
            text = body.decode("utf-8")
            queue.extend(links(text, url, ctype) - seen)
            body = rewrite(text).encode("utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        written += 1
        print(f"  {dest.relative_to(out)}")

    # 404 page: WordPress renders it for any unknown path.
    status, _, body = fetch(f"{src}/__static-export-404__")
    if status == 404:
        (out / "404.html").write_text(rewrite(body.decode("utf-8")), encoding="utf-8")
        print("  404.html")

    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {site}/sitemap.xml\n", encoding="utf-8")
    today = dt.date.today().isoformat()
    rels = sorted(p.parent.relative_to(out).as_posix() for p in out.rglob("index.html"))
    pages = [site + "/" if r == "." else f"{site}/{r}/" for r in rels]
    urls = "\n".join(f"  <url><loc>{p}</loc><lastmod>{today}</lastmod></url>" for p in pages)
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n",
        encoding="utf-8",
    )

    leftovers = [str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() and p.suffix in {".html", ".css", ".js", ".xml", ".txt", ".svg", ".json"} and src_host in p.read_text("utf-8", "ignore")]
    if leftovers:
        print(f"error: local origin still referenced in: {', '.join(leftovers)}", file=sys.stderr)
        return 1
    print(f"exported {written} files + 404.html, robots.txt, sitemap.xml to {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
