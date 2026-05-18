#!/usr/bin/env python3
"""
Idman Monitor v4

Редакционная версия агрегатора для азербайджанского спорта.

Что делает:
- Один проход без Final scan.
- Берёт только новости в окне 60 минут.
- Не повторяет уже отправленные новости между запусками.
- Отсекает страницы разделов/категорий/служебные страницы.
- Дедуплицирует одинаковые новости внутри письма.
- Разделяет письмо на "Футбол" и "Другие виды спорта".
- Даёт приоритет важным новостям: трансферы, травмы, сборная, еврокубки,
  скандалы/решения, интервью.
- Лимит: максимум 50 новостей в письме.
- Мелкие сайты не отключает, но ограничивает шум.
"""

from __future__ import annotations

import gc
import hashlib
import html
import os
import re
import smtplib
import sqlite3
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dateutil import parser as dateparser
from rapidfuzz import fuzz
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "sources.yaml"))
DB_PATH = Path(os.getenv("DB_PATH", "idman_monitor.sqlite3"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [x.strip() for x in os.getenv("EMAIL_TO", "").split(",") if x.strip()]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IdmanMonitor/4.0; +https://idman.biz)",
    "Accept-Language": "az,ru,en;q=0.9",
}

# Links that usually mean this is a real article path, not a section/listing.
ARTICLE_PATH_HINTS = [
    "/news/", "/xeber/", "/idman_xeberleri/", "/post/", "/article/",
    "/2025/", "/2026/", "/az/", "/ru/", "/a=", "/futbol/", "/bizim-futbol/",
]

SECTION_HINTS = [
    "sport", "idman", "futbol", "football", "basketbol", "voleybol",
    "mma", "ufc", "gules", "güləş", "judo", "cüdo", "chess", "şahmat"
]

BAD_URL_PATTERNS = [
    "/category/", "/categories/", "/news/premyer-liqa", "/misli",
    "/azerbaycanf", "/business/", "/economy/", "/weather", "/army",
    "/media", "/science-and-education", "/tag/", "/author/", "/page/",
    "/search", "/contact", "/about", "/reklam", "/advert", "/privacy",
]

BAD_TITLE_PATTERNS = [
    "günün son xəbərləri", "hərbi xəbərlər", "media xəbərləri",
    "hava haqqında xəbərlər", "biznes və iqtisadiyyat xəbərləri",
    "misli premyer liqa |", "azərbaycan futbolu »", "sportnet.az",
]

HIGH_PRIORITY_KEYWORDS = [
    # transfers / contracts
    "transfer", "müqavilə", "müqavilə imzaladı", "qadağa", "qadağası",
    "danışıqları", "qayıda bilər", "gedir", "ayrıldı", "vidalaşdı",
    "keçdi", "satıldı", "alındı",
    # injuries
    "zədə", "zədələn", "travma", "xəsarət", "əməliyyat",
    # national team
    "millimiz", "milli", "yığma", "sборная", "azerbaijan national",
    # European cups / UEFA
    "avrokubok", "çempionlar liqası", "avropa liqası", "konfrans liqası",
    "uefa", "rəqib", "püşk",
    # scandals / decisions
    "qalmaqal", "skandal", "qərar", "cəza", "fifa", "affa", "hakim",
    "şikayət", "apellyasiya",
    # interviews
    "müsahibə", "interview", "açıqlama", "dedi", "bildirdi",
]

MEDIUM_PRIORITY_KEYWORDS = [
    "qalib", "məğlub", "tur", "nəticə", "çempionat", "kubok",
    "medal", "start", "yekun", "heyət", "siyahı", "hazırlıq",
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
    topic: str
    priority: int
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
    replacements = {
        "qarabağ": "karabakh", "qarabag": "karabakh", "карабах": "karabakh",
        "neftçi": "neftchi", "нефтчи": "neftchi",
        "azərbaycan": "azerbaijan", "azerbaycan": "azerbaijan", "азербайджан": "azerbaijan",
        "güləş": "wrestling", "борьба": "wrestling",
        "cüdo": "judo", "дзюдо": "judo",
        "çempionlar liqası": "champions league",
        "avropa liqası": "europa league",
        "konfrans liqası": "conference league",
    }
    for a, b in replacements.items():
        t = t.replace(a, b)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    stop = {
        "və", "ve", "ilə", "üçün", "olan", "oldu", "deyib", "bildirib",
        "the", "and", "for", "from", "with", "и", "на", "по", "для", "что",
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
        ctype = r.headers.get("content-type", "").lower()
        if "image/" in ctype or "video/" in ctype or "application/pdf" in ctype:
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


def looks_like_bad_url(url: str) -> bool:
    low = url.lower()
    return any(p in low for p in BAD_URL_PATTERNS)


def looks_like_article_url(url: str) -> bool:
    low = url.lower()
    if looks_like_bad_url(url):
        return False
    return any(h in low for h in ARTICLE_PATH_HINTS)


def extract_links(source: Source, html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    links: List[str] = []

    for a in soup.find_all("a", href=True):
        url = clean_url(source.url, a.get("href", ""))
        if not url:
            continue
        if not same_domain(source.url, url):
            continue
        if looks_like_bad_url(url):
            continue

        txt = normalize_text(a.get_text(" "))
        low_url = url.lower()

        # Do not collect section/listing pages as articles.
        if looks_like_article_url(url) or len(txt) >= 25:
            links.append(url)

    seen = set()
    out = []
    for x in links:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out[:18]


def discover_section_pages(source: Source, html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    result = []
    seen = set()
    for a in soup.find_all("a", href=True):
        txt = normalize_text(a.get_text(" ")).lower()
        url = clean_url(source.url, a.get("href", ""))
        if not url or not same_domain(source.url, url):
            continue
        if looks_like_bad_url(url):
            continue
        low = url.lower()
        if any(h in txt or h in low for h in SECTION_HINTS):
            if url not in seen and url != source.url:
                result.append(url)
                seen.add(url)
    return result[:2]


def summarize_essence(text: str, max_sentences: int = 2) -> str:
    text = normalize_text(text)
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?։۔])\s+", text)
    parts = [p.strip() for p in parts if len(p.strip()) > 10]
    if not parts:
        return text[:350]
    return " ".join(parts[:max_sentences])[:600]


def parse_published_time(soup: BeautifulSoup, config: dict) -> Optional[datetime]:
    tz = ZoneInfo(config["settings"]["timezone"])
    candidates = []

    for attrs in [
        {"property": "article:published_time"},
        {"name": "pubdate"},
        {"name": "publishdate"},
        {"itemprop": "datePublished"},
        {"property": "og:updated_time"},
    ]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            candidates.append(tag.get("content"))

    for time_tag in soup.find_all("time"):
        if time_tag.get("datetime"):
            candidates.append(time_tag.get("datetime"))
        else:
            candidates.append(time_tag.get_text(" "))

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


def is_excluded(text: str, config: dict) -> bool:
    low = text.lower()
    for kw in config["settings"].get("exclude_keywords", []):
        if kw.lower() in low:
            return True
    return False


def is_bad_title(title: str) -> bool:
    low = title.lower()
    return any(p in low for p in BAD_TITLE_PATTERNS)


def is_azerbaijani_sport(text: str, config: dict) -> bool:
    low = text.lower()
    hints = config["settings"].get("az_sport_hints", [])
    return any(h.lower() in low for h in hints)


def detect_sport_type(text: str) -> str:
    low = text.lower()
    mapping = [
        ("football", ["futbol", "football", "футбол", "premyer liqa", "misli", "qarabağ", "neftçi", "zirə", "sabah", "qəbələ"]),
        ("futsal", ["futzal", "futsal", "мини-футбол"]),
        ("basketball", ["basketbol", "basketball", "баскетбол"]),
        ("volleyball", ["voleybol", "volleyball", "волейбол"]),
        ("mma", ["mma", "ufc", "bellator", "oktaqon", "октагон"]),
        ("judo", ["cüdo", "judo", "дзюдо"]),
        ("wrestling", ["güləş", "wrestling", "борьба", "güləşçi"]),
        ("chess", ["şahmat", "chess", "шахмат"]),
    ]
    for sport, keys in mapping:
        if any(k in low for k in keys):
            return sport
    return "other"


def topic_for_sport(sport_type: str) -> str:
    return "football" if sport_type in {"football", "futsal"} else "other"


def determine_priority(text: str) -> int:
    low = text.lower()
    if any(k in low for k in HIGH_PRIORITY_KEYWORDS):
        return 0
    if any(k in low for k in MEDIUM_PRIORITY_KEYWORDS):
        return 1
    return 2


def is_recent_enough(item: NewsItem, config: dict) -> bool:
    window_minutes = int(config["settings"].get("fresh_window_minutes", 60))
    tz = ZoneInfo(config["settings"]["timezone"])
    now = datetime.now(tz)
    dt = item.published_at or item.first_seen_at
    return dt >= now - timedelta(minutes=window_minutes)


def extract_article(source: Source, url: str, config: dict) -> Optional[NewsItem]:
    if not looks_like_article_url(url):
        return None

    html_text = fetch(url)
    if not html_text:
        return None

    soup = BeautifulSoup(html_text, "html.parser")

    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og.get("content", "")
    if not title and soup.find("h1"):
        title = soup.find("h1").get_text(" ")
    if not title and soup.title:
        title = soup.title.get_text(" ")

    title = normalize_text(title)
    if len(title) < 8 or is_bad_title(title):
        return None

    desc = ""
    for selector in [
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "description"}),
    ]:
        tag = soup.find(*selector)
        if tag and tag.get("content"):
            desc = tag.get("content", "")
            break

    if not desc:
        paragraphs = [normalize_text(p.get_text(" ")) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 25]
        desc = " ".join(paragraphs[:3])

    desc = summarize_essence(desc, 2)
    raw_text = normalize_text(f"{title} {desc}")

    if is_excluded(raw_text, config):
        return None
    if not is_azerbaijani_sport(raw_text, config):
        return None

    published_at = parse_published_time(soup, config)
    sport_type = detect_sport_type(raw_text)
    topic = topic_for_sport(sport_type)
    priority = determine_priority(raw_text)

    item = NewsItem(
        source=source,
        url=url,
        title=title,
        description=desc,
        published_at=published_at,
        first_seen_at=datetime.now(ZoneInfo(config["settings"]["timezone"])),
        sport_type=sport_type,
        topic=topic,
        priority=priority,
        raw_text=raw_text,
    )

    if not is_recent_enough(item, config):
        return None

    return item


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
        print("  opened", flush=True)

        pages = [source.url] + discover_section_pages(source, main_html)
        print(f"  pages to check: {len(pages)}", flush=True)

        candidate_urls: List[str] = []
        for page in pages:
            page_html = main_html if page == source.url else fetch(page)
            if not page_html:
                continue
            candidate_urls.extend(extract_links(source, page_html))

        # Keep order, unique.
        unique_candidates = []
        seen = set()
        for u in candidate_urls:
            if u not in seen:
                unique_candidates.append(u)
                seen.add(u)

        print(f"  candidate article links: {len(unique_candidates[:12])}", flush=True)

        source_found = 0
        for url in unique_candidates[:12]:
            item = extract_article(source, url, config)
            if not item:
                continue
            if not already_sent(conn, item, config):
                found.append(item)
                source_found += 1

        print(f"  new relevant items from {source.name}: {source_found}", flush=True)

        try:
            del main_html, pages, candidate_urls, unique_candidates, seen
        except Exception:
            pass
        gc.collect()

    print(f"Scan finished. Total relevant items found: {len(found)}", flush=True)
    return found, failed


def dedupe_batch(items: List[NewsItem]) -> List[Tuple[NewsItem, List[NewsItem]]]:
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

    fixed = []
    for primary, dupes in groups:
        all_items = [primary] + dupes
        all_items.sort(key=lambda x: x.published_at or x.first_seen_at)
        fixed.append((all_items[0], all_items[1:]))
    return fixed


def order_groups(groups: List[Tuple[NewsItem, List[NewsItem]]]) -> List[Tuple[NewsItem, List[NewsItem]]]:
    return sorted(
        groups,
        key=lambda g: (
            g[0].topic != "football",                 # football first
            g[0].priority,                            # important first
            -(g[0].published_at or g[0].first_seen_at).timestamp(),  # newest first
        )
    )


def trim_groups(groups: List[Tuple[NewsItem, List[NewsItem]]], max_items: int) -> List[Tuple[NewsItem, List[NewsItem]]]:
    return groups[:max_items]


def format_dt(dt: Optional[datetime], fallback: datetime, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    d = (dt or fallback).astimezone(tz)
    return d.strftime("%H:%M")


def priority_label(priority: int) -> str:
    if priority == 0:
        return "🔥 Важно"
    if priority == 1:
        return "🟡 Среднее"
    return "⚪ Низкое"


def build_email(config: dict, groups: List[Tuple[NewsItem, List[NewsItem]]], failed: List[str]) -> Tuple[str, str, str]:
    tz_name = config["settings"]["timezone"]
    now = datetime.now(ZoneInfo(tz_name))
    subject = f"{config['settings']['digest_name']} — {now.strftime('%H:%M')}"
    emoji_map = config["settings"].get("sport_emoji", {})

    lines_text = [subject, ""]
    lines_html = [f"<h2>{html.escape(subject)}</h2>"]

    sections = [
        ("football", "⚽ Футбол"),
        ("other", "🏅 Другие виды спорта"),
    ]

    for topic_key, section_title in sections:
        section_groups = [g for g in groups if g[0].topic == topic_key]
        if not section_groups:
            continue

        lines_text.extend([section_title, ""])
        lines_html.append(f"<h3>{html.escape(section_title)}</h3>")

        for primary, dupes in section_groups:
            emoji = emoji_map.get(primary.sport_type, emoji_map.get("other", "🏅"))
            time_s = format_dt(primary.published_at, primary.first_seen_at, tz_name)
            desc = summarize_essence(primary.description, 2)

            also_sources = []
            for d in dupes:
                if d.source.name != primary.source.name and d.source.name not in also_sources:
                    also_sources.append(d.source.name)

            also_text = ""
            if also_sources:
                shown = ", ".join(also_sources[:3])
                extra = len(also_sources) - 3
                also_text = f"Также: {shown}" + (f" (+{extra})" if extra > 0 else "")

            plabel = priority_label(primary.priority)
            lines_text.extend([
                f"{emoji} {time_s} — {plabel}",
                primary.title,
                desc,
                f"Источник: {primary.source.name}" + (f" (+{len(also_sources)})" if also_sources else ""),
            ])
            if also_text:
                lines_text.append(also_text)
            lines_text.append(primary.url)
            lines_text.append("")

            lines_html.append(
                f"<p><strong>{emoji} {time_s} — {html.escape(plabel)}</strong><br>"
                f"<strong>{html.escape(primary.title)}</strong><br>"
                f"{html.escape(desc)}<br>"
                f"Источник: {html.escape(primary.source.name)}"
                + (f" (+{len(also_sources)})" if also_sources else "")
                + "<br>"
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

    print(f"Sending email to: {', '.join(EMAIL_TO)}", flush=True)

    msg = MIMEMultipart("alternative")
    sender_name = config["settings"].get("sender_name", "Idman Monitor")
    msg["From"] = formataddr((sender_name, EMAIL_FROM or SMTP_USER))
    msg["To"] = ", ".join(EMAIL_TO)
    msg["Subject"] = subject

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_APP_PASSWORD)
        server.sendmail(EMAIL_FROM or SMTP_USER, EMAIL_TO, msg.as_string())


def main() -> int:
    config = load_config()
    conn = init_db()

    print("Idman Monitor v4 started", flush=True)
    items, failed = scan_once(config, conn)

    groups = dedupe_batch(items)
    groups = order_groups(groups)
    max_items = int(config["settings"].get("max_items_per_email", 50))
    groups = trim_groups(groups, max_items)

    if not groups:
        print("No new items. No email sent.", flush=True)
        if failed:
            print("Failed sources:", ", ".join(sorted(set(failed))), flush=True)
        return 0

    subject, text_body, html_body = build_email(config, groups, failed)
    send_email(config, subject, text_body, html_body)

    # Mark primary items as sent after email was sent.
    mark_sent(conn, [g[0] for g in groups], config)
    print(f"Sent digest with {len(groups)} news items.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
