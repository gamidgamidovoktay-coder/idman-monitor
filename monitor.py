#!/usr/bin/env python3
"""
Idman Monitor
A lightweight news-monitoring script for Azerbaijani sports sources.

What it does:
- Scans configured sites.
- Performs a final rescan ~2-3 minutes before sending.
- Keeps SQLite memory so already sent news is not repeated.
- Deduplicates by URL and by similar meaning/title.
- Keeps original language in title and summary.
- Sends digest by email through Gmail SMTP.
"""

from __future__ import annotations

import gc
import hashlib
import html
import os
import re
import smtplib
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import yaml
import warnings
from bs4 import XMLParsedAsHTMLWarning
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from rapidfuzz import fuzz
from zoneinfo import ZoneInfo

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "sources.yaml"))
DB_PATH = Path(os.getenv("DB_PATH", "idman_monitor.sqlite3"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [x.strip() for x in os.getenv("EMAIL_TO", "").split(",") if x.strip()]

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; IdmanMonitor/1.0; +https://idman.biz)"
    ),
    "Accept-Language": "az,ru,en;q=0.9",
}

ARTICLE_PATH_HINTS = [
    "/news/", "/xeber/", "/idman/", "/sport/", "/futbol/", "/football/",
    "/az/", "/ru/", "/a/", "/post/", "/article/"
]

SECTION_HINTS = [
    "sport", "idman", "futbol", "football", "basketbol", "voleybol",
    "mma", "ufc", "gules", "güləş", "judo", "cüdo", "chess", "şahmat"
]

@dataclass
class Source:
    name: str
    url: str
    group: str = "C"

@dataclass
class NewsItem:
    source: Source
    url: str
    title: str
    description: str
    published_at: Optional[datetime]
    first_seen_at: datetime
    sport_type: str
    raw_text: str


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title_hash TEXT,
            semantic_key TEXT,
            title TEXT,
            source TEXT,
            sent_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failures (
            source TEXT PRIMARY KEY,
            last_failed_at TEXT,
            consecutive_days INTEGER DEFAULT 0,
            disabled INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def semantic_key(title: str) -> str:
    t = normalize_text(title).lower()
    # Normalize common spellings.
    replacements = {
        "qarabağ": "karabakh",
        "qarabag": "karabakh",
        "карабах": "karabakh",
        "neftçi": "neftchi",
        "нефтчи": "neftchi",
        "azərbaycan": "azerbaijan",
        "azerbaycan": "azerbaijan",
        "азербайджан": "azerbaijan",
        "güləş": "wrestling",
        "борьба": "wrestling",
        "cüdo": "judo",
        "дзюдо": "judo",
    }
    for a, b in replacements.items():
        t = t.replace(a, b)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    stop = {
        "и", "в", "на", "о", "об", "the", "a", "an", "of", "to", "az", "ru",
        "ve", "və", "bu", "ilə", "üçün", "sonra", "deyib", "bildirib"
    }
    tokens = [x for x in t.split() if len(x) > 2 and x not in stop]
    return " ".join(tokens[:18])


def clean_url(base_url: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    full = urljoin(base_url, href)
    full, _frag = urldefrag(full)
    parsed = urlparse(full)
    if not parsed.scheme.startswith("http"):
        return None
    return full


def same_domain(url1: str, url2: str) -> bool:
    d1 = urlparse(url1).netloc.lower().replace("www.", "")
    d2 = urlparse(url2).netloc.lower().replace("www.", "")
    return d1 == d2


def fetch(url: str, timeout: int = 5) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True, stream=True)
        if r.status_code >= 400:
            return None
        content_type = r.headers.get("content-type", "").lower()
        if "image/" in content_type or "video/" in content_type or "application/pdf" in content_type:
            return None
        max_bytes = 1_500_000
        chunks = []
        total = 0
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        enc = r.encoding or "utf-8"
        return raw.decode(enc, errors="replace")
    except Exception:
        return None


def extract_links(source: Source, html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    links: List[str] = []

    for a in soup.find_all("a", href=True):
        url = clean_url(source.url, a.get("href", ""))
        if not url:
            continue
        if not same_domain(source.url, url):
            continue
        low = url.lower()
        txt = normalize_text(a.get_text(" ")).lower()
        # Keep likely articles and section links. Avoid obvious assets/admin links.
        if any(x in low for x in ["/wp-admin", "/login", ".jpg", ".png", ".pdf", "#"]):
            continue
        if any(h in low for h in ARTICLE_PATH_HINTS) or len(txt) >= 20:
            links.append(url)

    # Preserve order, unique.
    seen = set()
    out = []
    for x in links:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out[:18]


def discover_section_pages(source: Source, html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    sections = []
    for a in soup.find_all("a", href=True):
        txt = normalize_text(a.get_text(" ")).lower()
        url = clean_url(source.url, a.get("href", ""))
        if not url or not same_domain(source.url, url):
            continue
        low = url.lower()
        if any(h in txt or h in low for h in SECTION_HINTS):
            sections.append(url)
    # main + first page of each section only, capped
    result = []
    seen = set()
    for u in sections:
        if u not in seen and u != source.url:
            result.append(u)
            seen.add(u)
    return result[:2]


def extract_article(source: Source, url: str, config: dict) -> Optional[NewsItem]:
    html_text = fetch(url)
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")

    title = ""
    if soup.find("meta", property="og:title"):
        title = soup.find("meta", property="og:title").get("content", "")
    if not title and soup.find("h1"):
        title = soup.find("h1").get_text(" ")
    if not title and soup.title:
        title = soup.title.get_text(" ")

    title = normalize_text(title)
    if len(title) < 8:
        return None

    desc = ""
    for selector in [
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "description"}),
    ]:
        tag = soup.find(*selector)
        if tag:
            desc = tag.get("content", "")
            break

    if not desc:
        paragraphs = [normalize_text(p.get_text(" ")) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 25]
        desc = " ".join(paragraphs[:3])

    desc = summarize_essence(desc, config["settings"].get("normal_summary_sentences", 3))
    raw_text = normalize_text(f"{title} {desc}")

    if is_excluded(raw_text, config):
        return None
    if not is_azerbaijani_sport(raw_text, config):
        return None

    published_at = parse_published_time(soup, config)
    sport_type = detect_sport_type(raw_text)

    return NewsItem(
        source=source,
        url=url,
        title=title,
        description=desc,
        published_at=published_at,
        first_seen_at=datetime.now(ZoneInfo(config["settings"]["timezone"])),
        sport_type=sport_type,
        raw_text=raw_text,
    )


def parse_published_time(soup: BeautifulSoup, config: dict) -> Optional[datetime]:
    candidates = []
    for attrs in [
        {"property": "article:published_time"},
        {"name": "pubdate"},
        {"name": "publishdate"},
        {"itemprop": "datePublished"},
    ]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            candidates.append(tag.get("content"))

    for time_tag in soup.find_all("time"):
        if time_tag.get("datetime"):
            candidates.append(time_tag.get("datetime"))
        else:
            candidates.append(time_tag.get_text(" "))

    tz = ZoneInfo(config["settings"]["timezone"])
    for c in candidates:
        try:
            dt = dateparser.parse(c, fuzzy=True)
            if not dt:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(tz)
        except Exception:
            continue
    return None


def summarize_essence(text: str, max_sentences: int) -> str:
    text = normalize_text(text)
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?։۔])\s+", text)
    parts = [p.strip() for p in parts if len(p.strip()) > 10]
    if not parts:
        return text[:350]
    summary = " ".join(parts[:max_sentences])
    return summary[:700]


def is_excluded(text: str, config: dict) -> bool:
    low = text.lower()
    for kw in config["settings"].get("exclude_keywords", []):
        if kw.lower() in low:
            return True
    return False


def is_azerbaijani_sport(text: str, config: dict) -> bool:
    low = text.lower()
    hints = config["settings"].get("az_sport_hints", [])
    return any(h.lower() in low for h in hints)


def detect_sport_type(text: str) -> str:
    low = text.lower()
    mapping = [
        ("football", ["futbol", "football", "футбол", "premyer liqa", "misli", "qarabağ", "neftçi", "zirə"]),
        ("futsal", ["futzal", "futsal", "мини-футбол"]),
        ("basketball", ["basketbol", "basketball", "баскетбол"]),
        ("volleyball", ["voleybol", "volleyball", "волейбол"]),
        ("mma", ["mma", "ufc", "bellator", "октагон"]),
        ("judo", ["cüdo", "judo", "дзюдо"]),
        ("wrestling", ["güləş", "wrestling", "борьба", "güləşçi"]),
        ("chess", ["şahmat", "chess", "шахмат"]),
    ]
    for sport, keys in mapping:
        if any(k in low for k in keys):
            return sport
    return "other"


def already_sent(conn: sqlite3.Connection, item: NewsItem, config: dict) -> bool:
    key = semantic_key(item.title)
    memory_hours = int(config["settings"].get("sent_memory_hours", 72))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=memory_hours)
    rows = conn.execute(
        "SELECT url, semantic_key, title FROM sent_items WHERE sent_at >= ?",
        (cutoff.isoformat(),)
    ).fetchall()
    if any(row[0] == item.url for row in rows):
        return True
    for _url, sem, title in rows:
        # Meaning-level duplicate.
        if sem and key and fuzz.token_set_ratio(sem, key) >= 88:
            return True
        if title and fuzz.token_set_ratio(title.lower(), item.title.lower()) >= 90:
            return True
    return False


def mark_sent(conn: sqlite3.Connection, items: Iterable[NewsItem], config: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        conn.execute(
            "INSERT OR IGNORE INTO sent_items(url, title_hash, semantic_key, title, source, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item.url, sha(item.title), semantic_key(item.title), item.title, item.source.name, now),
        )
    conn.commit()


def record_failure(conn: sqlite3.Connection, source_name: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute("SELECT consecutive_days FROM failures WHERE source=?", (source_name,)).fetchone()
    if row:
        conn.execute(
            "UPDATE failures SET last_failed_at=?, consecutive_days=consecutive_days+1 WHERE source=?",
            (now, source_name),
        )
    else:
        conn.execute(
            "INSERT INTO failures(source, last_failed_at, consecutive_days, disabled) VALUES (?, ?, 1, 0)",
            (source_name, now),
        )
    conn.commit()


def clear_failure(conn: sqlite3.Connection, source_name: str) -> None:
    conn.execute("DELETE FROM failures WHERE source=?", (source_name,))
    conn.commit()


def disabled_sources(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT source FROM failures WHERE consecutive_days >= 10").fetchall()
    return {r[0] for r in rows}


def scan_once(config: dict, conn: sqlite3.Connection) -> Tuple[List[NewsItem], List[str]]:
    sources = [Source(**s) for s in config["sources"]]
    disabled = disabled_sources(conn)
    found: List[NewsItem] = []
    failed: List[str] = []

    for idx, source in enumerate(sources, start=1):
        print(f"[{idx}/{len(sources)}] Scanning {source.name}: {source.url}", flush=True)
        if source.name in disabled:
            failed.append(f"{source.name} (отключён после 10 дней ошибок)")
            continue

        main_html = fetch(source.url)
        if not main_html:
            print(f"  FAILED: cannot open {source.name}", flush=True)
            failed.append(source.name)
            record_failure(conn, source.name)
            continue
        clear_failure(conn, source.name)
        print(f"  opened", flush=True)

        pages = [source.url] + discover_section_pages(source, main_html)
        candidate_urls: List[str] = []
        print(f"  pages to check: {len(pages)}", flush=True)
        for page in pages:
            page_html = main_html if page == source.url else fetch(page)
            if not page_html:
                continue
            candidate_urls.extend(extract_links(source, page_html))

        # First page/main only, capped per source to avoid overload.
        seen_urls = set()
        print(f"  candidate article links: {len(candidate_urls[:12])}", flush=True)
        source_found = 0
        for url in candidate_urls[:12]:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            item = extract_article(source, url, config)
            if not item:
                continue
            if not already_sent(conn, item, config):
                found.append(item)
                source_found += 1
        print(f"  new relevant items from {source.name}: {source_found}", flush=True)
        try:
            del main_html, pages, candidate_urls, seen_urls
        except Exception:
            pass
        gc.collect()

    print(f"Scan finished. Total relevant items found: {len(found)}", flush=True)
    return found, failed


def dedupe_batch(items: List[NewsItem]) -> List[Tuple[NewsItem, List[NewsItem]]]:
    """Return primary item + duplicate/same-meaning items from this batch."""
    groups: List[Tuple[NewsItem, List[NewsItem]]] = []
    for item in items:
        placed = False
        key = semantic_key(item.title)
        for primary, dupes in groups:
            pkey = semantic_key(primary.title)
            if fuzz.token_set_ratio(key, pkey) >= 88 or fuzz.token_set_ratio(item.title, primary.title) >= 90:
                dupes.append(item)
                placed = True
                break
        if not placed:
            groups.append((item, []))

    # Primary source = the one where same news appeared earliest by publication time, fallback first seen.
    fixed_groups = []
    for primary, dupes in groups:
        all_items = [primary] + dupes
        all_items.sort(key=lambda x: x.published_at or x.first_seen_at)
        fixed_groups.append((all_items[0], all_items[1:]))
    return fixed_groups


def order_groups(groups: List[Tuple[NewsItem, List[NewsItem]]]) -> List[Tuple[NewsItem, List[NewsItem]]]:
    priority = {
        "football": 0, "futsal": 1, "basketball": 2, "volleyball": 3,
        "mma": 4, "judo": 5, "wrestling": 6, "chess": 7, "other": 8
    }
    return sorted(
        groups,
        key=lambda g: (
            -(g[0].published_at or g[0].first_seen_at).timestamp(),
            priority.get(g[0].sport_type, 9)
        )
    )


def apply_consecutive_source_limit(groups: List[Tuple[NewsItem, List[NewsItem]]], limit: int) -> List[Tuple[NewsItem, List[NewsItem]]]:
    # Soft reshuffle to avoid 7+ consecutive items from same source.
    result: List[Tuple[NewsItem, List[NewsItem]]] = []
    pool = groups[:]
    while pool:
        candidate_idx = 0
        if len(result) >= limit:
            last_sources = [g[0].source.name for g in result[-limit:]]
            if len(set(last_sources)) == 1:
                for i, g in enumerate(pool):
                    if g[0].source.name != last_sources[0]:
                        candidate_idx = i
                        break
        result.append(pool.pop(candidate_idx))
    return result


def format_dt(dt: Optional[datetime], fallback: datetime, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    d = (dt or fallback).astimezone(tz)
    return d.strftime("%H:%M")


def build_email(config: dict, groups: List[Tuple[NewsItem, List[NewsItem]]], failed: List[str]) -> Tuple[str, str, str]:
    tz_name = config["settings"]["timezone"]
    now = datetime.now(ZoneInfo(tz_name))
    subject = f"{config['settings']['digest_name']} — {now.strftime('%H:%M')}"
    emoji_map = config["settings"].get("sport_emoji", {})

    news_count = len(groups)
    max_sentences = (
        config["settings"].get("if_news_count_over_40_summary_sentences", 1)
        if news_count > 40 else
        config["settings"].get("normal_summary_sentences", 3)
    )

    lines_text = [subject, ""]
    lines_html = [f"<h2>{html.escape(subject)}</h2>"]

    for primary, dupes in groups:
        emoji = emoji_map.get(primary.sport_type, emoji_map.get("other", "🏅"))
        time_s = format_dt(primary.published_at, primary.first_seen_at, tz_name)
        desc = summarize_essence(primary.description, max_sentences)
        also_sources = [d.source.name for d in dupes if d.source.name != primary.source.name]
        also_unique = []
        for s in also_sources:
            if s not in also_unique:
                also_unique.append(s)

        exclusive = " — пока только один источник" if not also_unique else ""
        also_text = ""
        if also_unique:
            shown = ", ".join(also_unique[:3])
            extra = len(also_unique) - 3
            also_text = f"Также: {shown}" + (f" (+{extra})" if extra > 0 else "")

        lines_text.extend([
            f"{emoji} {time_s}{exclusive}",
            primary.title,
            desc,
            f"Источник: {primary.source.name}",
        ])
        if also_text:
            lines_text.append(also_text)
        lines_text.append(primary.url)
        lines_text.append("")

        lines_html.append(
            f"<p><strong>{emoji} {time_s}{html.escape(exclusive)}</strong><br>"
            f"<strong>{html.escape(primary.title)}</strong><br>"
            f"{html.escape(desc)}<br>"
            f"Источник: {html.escape(primary.source.name)}<br>"
            + (f"{html.escape(also_text)}<br>" if also_text else "")
            + f'<a href="{html.escape(primary.url)}">{html.escape(primary.url)}</a></p>'
        )

    if failed:
        lines_text.append("⚠️ Не удалось открыть:")
        lines_html.append("<h3>⚠️ Не удалось открыть:</h3><ul>")
        for f in sorted(set(failed)):
            lines_text.append(f"- {f}")
            lines_html.append(f"<li>{html.escape(f)}</li>")
        lines_html.append("</ul>")

    return subject, "\n".join(lines_text), "\n".join(lines_html)


def send_email(config: dict, subject: str, text_body: str, html_body: str) -> None:
    if not SMTP_USER or not SMTP_APP_PASSWORD or not EMAIL_TO:
        print("Email env vars are missing. Set SMTP_USER, SMTP_APP_PASSWORD, EMAIL_TO.", flush=True)
        return

    msg = MIMEMultipart("alternative")
    sender_name = config["settings"].get("sender_name", "Idman Monitor")
    msg["From"] = formataddr((sender_name, EMAIL_FROM or SMTP_USER))
    msg["To"] = ", ".join(EMAIL_TO)
    msg["Subject"] = subject

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    print(f"Sending email to: {', '.join(EMAIL_TO)}", flush=True)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_APP_PASSWORD)
        server.sendmail(EMAIL_FROM or SMTP_USER, EMAIL_TO, msg.as_string())


def main() -> int:
    config = load_config()
    conn = init_db()

    print("First scan...", flush=True)
    items1, failed1 = scan_once(config, conn)

    wait = int(config["settings"].get("final_rescan_seconds_before_send", 150))
    if wait > 0:
        print(f"Waiting {wait} seconds before final rescan...", flush=True)
        time.sleep(wait)

    print("Final scan...", flush=True)
    items2, failed2 = scan_once(config, conn)

    all_items = items1 + items2
    groups = dedupe_batch(all_items)
    groups = order_groups(groups)
    groups = apply_consecutive_source_limit(
        groups, int(config["settings"].get("max_consecutive_from_one_source", 7))
    )

    failed = failed1 + failed2

    if not groups:
        # If no news: normally send nothing. But if no news for 2-3 hours can be added later.
        print("No new items. No email sent.", flush=True)
        if failed:
            print("Failed sources:", ", ".join(sorted(set(failed))), flush=True)
        return 0

    subject, text_body, html_body = build_email(config, groups, failed)
    send_email(config, subject, text_body, html_body)

    # Mark only after successful send attempt.
    mark_sent(conn, [g[0] for g in groups], config)
    print(f"Sent digest with {len(groups)} news items.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
