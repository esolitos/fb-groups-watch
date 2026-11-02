#!/usr/bin/env python3
"""Text digest of new items from a browser session."""
import argparse
import fcntl
import hashlib
import os
import re
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timezone
from urllib.parse import urlparse

GROUP_RE = re.compile(r"/groups/([^/?#]+)")
POST_RE = re.compile(r"/groups/([^/?#]+)/(?:posts|permalink)/(\d+)")
URL_RE = re.compile(r"(https?://\S+|www\.\S+)")
SCROLLS = 3

EXTRACT_JS = r"""
() => {
  const top = Array.from(document.querySelectorAll('[role="article"]'))
    .filter(a => !a.parentElement || !a.parentElement.closest('[role="article"]'));
  return top.map(a => {
    const link = a.querySelector('a[href*="/posts/"], a[href*="/permalink/"]');
    const head = a.querySelector('h2 a, h3 a, h4 a, strong a, h2, h3, h4, strong');
    const msg = a.querySelector('[data-ad-preview="message"], [data-ad-comet-preview="message"]');
    let text = msg ? msg.innerText : '';
    if (!text) {
      text = Array.from(a.querySelectorAll('div[dir="auto"]'))
        .filter(d => d.closest('[role="article"]') === a)
        .map(d => d.innerText).join(' ');
    }
    return {
      href: link ? link.href : '',
      time: link ? (link.getAttribute('aria-label') || link.innerText || '') : '',
      author: head ? head.innerText : '',
      text: text || '',
    };
  });
}
"""


def clean_excerpt(text, limit=140):
    text = URL_RE.sub("", text or "")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def dedupe_key(post_id, author, text):
    if post_id:
        return "id:" + post_id
    norm = " ".join((text or "").split())[:100]
    raw = f"{author}|{norm}"
    return "h:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_link(gid, pid):
    return f"https://www.facebook.com/groups/{gid}/posts/{pid}/"


def group_id(url):
    m = GROUP_RE.search(urlparse(url).path)
    return m.group(1) if m else None


def parse_post_href(href):
    m = POST_RE.search(urlparse(href or "").path)
    return (m.group(1), m.group(2)) if m else (None, None)


def is_allowed_url(url, gids):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not (host == "facebook.com" or host.endswith(".facebook.com")):
        return False
    gid = group_id(url)
    return gid is not None and gid in gids


def is_login_url(url):
    path = urlparse(url).path.lower()
    return any(path == p or path.startswith(p + "/") or path.startswith(p + ".")
               for p in ("/login", "/checkpoint"))


def feed_url(url):
    base = url.split("?")[0].split("#")[0].rstrip("/")
    return base + "/?sorting_setting=CHRONOLOGICAL"


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    groups = []
    for g in cfg.get("groups", []):
        gid = group_id(g["url"])
        if not gid:
            raise SystemExit(f"bad group url: {g['url']}")
        groups.append({"name": g["name"], "url": g["url"], "gid": gid})
    return groups, int(cfg.get("max_posts", 20))


def open_db(path):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        "post_key TEXT NOT NULL, grp TEXT NOT NULL, first_seen TEXT NOT NULL, "
        "PRIMARY KEY (grp, post_key))"
    )
    return db


class Abort(Exception):
    def __init__(self, line, code):
        super().__init__(line)
        self.line, self.code = line, code


def check_page(page, gids):
    url = page.url
    if is_login_url(url) or page.locator('input[name="email"], input[name="pass"]').count():
        raise Abort(f"ALERT needs_login: {urlparse(url).path}", 2)
    if not is_allowed_url(url, gids):
        raise Abort(f"ALERT unexpected_page: {urlparse(url).hostname}{urlparse(url).path}", 4)


def read_group(page, group, gids, max_posts):
    page.goto(feed_url(group["url"]), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    page.keyboard.press("Escape")
    check_page(page, gids)
    posts = {}
    for i in range(SCROLLS + 1):
        for item in page.evaluate(EXTRACT_JS):
            gid, pid = parse_post_href(item["href"])
            author = " ".join(item["author"].split())
            ttext = " ".join(item["time"].split())
            key = dedupe_key(pid, author, item["text"])
            if key not in posts:
                posts[key] = {
                    "author": author, "time": ttext, "text": item["text"],
                    "link": build_link(gid or group["gid"], pid) if pid else None,
                }
        if len(posts) >= max_posts or i == SCROLLS:
            break
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(2500)
        page.keyboard.press("Escape")
        check_page(page, gids)
    return dict(list(posts.items())[:max_posts])


def format_post(group_name, p):
    out = f"{group_name} · {p['author'] or '?'} · {p['time'] or '?'}\n{clean_excerpt(p['text'])}"
    if p["link"]:
        out += "\n" + p["link"]
    return out


def launch(pw, profile, headless=False):
    ctx = pw.chromium.launch_persistent_context(
        profile, headless=headless, timezone_id="Europe/Oslo"
    )
    ctx.set_default_timeout(60000)
    ctx.set_default_navigation_timeout(60000)
    return ctx


def run_login(profile):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = launch(pw, profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.facebook.com/")
            while ctx.pages:
                time.sleep(1)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def run_watch(cfg_path, profile, db_path):
    groups, max_posts = load_config(cfg_path)
    gids = {g["gid"] for g in groups}
    db = open_db(db_path)
    try:
        return _watch(groups, gids, max_posts, profile, db)
    finally:
        db.close()


def _watch(groups, gids, max_posts, profile, db):
    from playwright.sync_api import sync_playwright
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines, alerts = [], []
    with sync_playwright() as pw:
        ctx = launch(pw, profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(60000)
            page.set_default_navigation_timeout(60000)
            for g in groups:
                err = ""
                try:
                    posts = read_group(page, g, gids, max_posts)
                except Abort:
                    raise
                except Exception as e:
                    posts, err = {}, " " + type(e).__name__
                if not posts:
                    alerts.append(f"ALERT parse_error {g['name']}{err}")
                    continue
                baseline = db.execute(
                    "SELECT 1 FROM seen WHERE grp=? LIMIT 1", (g["gid"],)
                ).fetchone() is None
                for key, p in posts.items():
                    cur = db.execute(
                        "INSERT OR IGNORE INTO seen (post_key, grp, first_seen) VALUES (?,?,?)",
                        (key, g["gid"], now),
                    )
                    if cur.rowcount and not baseline:
                        lines.append(format_post(g["name"], p))
        except Abort as e:
            print(e.line)
            return e.code
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    db.commit()
    if lines:
        print("\n\n".join(lines))
    if alerts:
        print(("\n" if lines else "") + "\n".join(alerts))
    return 3 if alerts else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--login", action="store_true")
    a = ap.parse_args(argv)
    lock = open(a.state + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ALERT locked")
        return 5
    os.makedirs(a.profile, exist_ok=True)
    # we hold the lock, so no browser of ours has the profile open
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.unlink(os.path.join(a.profile, name))
        except FileNotFoundError:
            pass
    if a.login:
        return run_login(a.profile)
    return run_watch(a.config, a.profile, a.state)


if __name__ == "__main__":
    sys.exit(main())
