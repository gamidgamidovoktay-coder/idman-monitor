#!/usr/bin/env python3
from __future__ import annotations

import gc, hashlib, html, os, re, smtplib, sqlite3, ssl, warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import requests, yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dateutil import parser as dateparser
from rapidfuzz import fuzz
from zoneinfo import ZoneInfo

try:
    import pg8000.dbapi as pgdb
except Exception:
    pgdb = None

warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)

CONFIG_PATH = Path(os.getenv('CONFIG_PATH','sources.yaml'))
DB_PATH = Path(os.getenv('DB_PATH','idman_monitor.sqlite3'))
DATABASE_URL = os.getenv('DATABASE_URL','').strip()
SMTP_HOST = os.getenv('SMTP_HOST','smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT','587'))
SMTP_USER = os.getenv('SMTP_USER','')
SMTP_APP_PASSWORD = os.getenv('SMTP_APP_PASSWORD','')
EMAIL_FROM = os.getenv('EMAIL_FROM', SMTP_USER)
EMAIL_TO = [x.strip() for x in os.getenv('EMAIL_TO','').split(',') if x.strip()]

HEADERS = {'User-Agent':'Mozilla/5.0 (compatible; IdmanMonitor/5.0; +https://idman.biz)','Accept-Language':'az,ru,en;q=0.9'}
ARTICLE_PATH_HINTS = ['/news/','/xeber/','/idman_xeberleri/','/post/','/article/','/2025/','/2026/','/az/','/ru/','/a=','/futbol/','/bizim-futbol/']
SECTION_HINTS = ['sport','idman','futbol','football','basketbol','voleybol','mma','ufc','gules','güləş','judo','cüdo','chess','şahmat']
BAD_URL_PATTERNS = ['/category/','/categories/','/news/premyer-liqa','/misli','/azerbaycanf','/business/','/economy/','/weather','/army','/media','/science-and-education','/tag/','/author/','/page/','/search','/contact','/about','/reklam','/advert','/privacy']
BAD_TITLE_PATTERNS = ['günün son xəbərləri','hərbi xəbərlər','media xəbərləri','hava haqqında xəbərlər','biznes və iqtisadiyyat xəbərləri','misli premyer liqa |','azərbaycan futbolu »','sportnet.az']
HIGH = ['transfer','müqavilə','qadağa','danışıqları','qayıda bilər','gedir','ayrıldı','vidalaşdı','keçdi','satıldı','alındı','zədə','zədələn','travma','xəsarət','əməliyyat','millimiz','milli','yığma','sборная','avrokubok','çempionlar liqası','avropa liqası','konfrans liqası','uefa','rəqib','püşk','qalmaqal','skandal','qərar','cəza','fifa','affa','hakim','şikayət','apellyasiya','müsahibə','interview','açıqlama','dedi','bildirdi']
MEDIUM = ['qalib','məğlub','tur','nəticə','çempionat','kubok','medal','start','yekun','heyət','siyahı','hazırlıq']

@dataclass
class Source:
    name: str
    url: str
    group: str = 'C'

@dataclass
class NewsItem:
    source: Source
    url: str
    title: str
    description: str
    published_at: datetime
    first_seen_at: datetime
    sport_type: str
    topic: str
    priority: int
    raw_text: str

class DB:
    def __init__(self):
        self.is_pg = bool(DATABASE_URL and pgdb)
        if self.is_pg:
            parsed = urlparse(DATABASE_URL)
            ssl_ctx = ssl._create_unverified_context()
            self.conn = pgdb.connect(
                user=parsed.username,
                password=parsed.password,
                host=parsed.hostname,
                port=parsed.port or 5432,
                database=parsed.path.lstrip("/"),
                ssl_context=ssl_ctx,
            )
            self.conn.autocommit = True
        else:
            if DATABASE_URL and not pgdb:
                print("DATABASE_URL set, but pg8000 is not installed. Falling back to SQLite.", flush=True)
            self.conn = sqlite3.connect(DB_PATH)

    def _convert_sql(self, sql: str) -> str:
        if not self.is_pg:
            return sql
        return sql.replace("?", "%s").replace("ON CONFLICT(url) DO NOTHING", "ON CONFLICT (url) DO NOTHING")

    def q(self, sql: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(self._convert_sql(sql), params)
        if not self.is_pg:
            self.conn.commit()
        return cur

    def rows(self, sql: str, params: tuple = ()):
        return self.q(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()):
        return self.q(sql, params)

    def fetchall(self, sql: str, params: tuple = ()):
        return self.rows(sql, params)

    def close(self):
        self.conn.close()


def load_config():
    with CONFIG_PATH.open('r', encoding='utf-8') as f: return yaml.safe_load(f)

def init_db():
    db=DB()
    db.q('CREATE TABLE IF NOT EXISTS sent_items (url TEXT PRIMARY KEY, title_hash TEXT, semantic_key TEXT, title TEXT, source TEXT, sent_at TEXT)')
    db.q('CREATE TABLE IF NOT EXISTS pending_items (url TEXT PRIMARY KEY, title TEXT, description TEXT, published_at TEXT, first_seen_at TEXT, source_name TEXT, source_url TEXT, sport_type TEXT, topic TEXT, priority INTEGER, raw_text TEXT, semantic_key TEXT, created_at TEXT)')
    db.q('CREATE TABLE IF NOT EXISTS failures (source TEXT PRIMARY KEY, last_failed_at TEXT, consecutive_days INTEGER DEFAULT 0, disabled INTEGER DEFAULT 0)')
    return db

def now_utc(): return datetime.now(timezone.utc).isoformat()
def norm(t): return html.unescape(t or '').replace('\xa0',' ').strip() if t else ''
def normalize_text(t): return re.sub(r'\s+',' ', norm(t)).strip()
def sha(t): return hashlib.sha256(t.encode('utf-8','ignore')).hexdigest()

def semantic_key(title):
    t=normalize_text(title).lower()
    for a,b in {'qarabağ':'karabakh','qarabag':'karabakh','neftçi':'neftchi','azərbaycan':'azerbaijan','azerbaycan':'azerbaijan','güləş':'wrestling','cüdo':'judo','çempionlar liqası':'champions league','avropa liqası':'europa league','konfrans liqası':'conference league'}.items(): t=t.replace(a,b)
    t=re.sub(r'[^\w\s]',' ',t, flags=re.UNICODE)
    stop={'və','ve','ilə','üçün','olan','oldu','deyib','bildirib','the','and','for','from','with','и','на','по','для','что'}
    return ' '.join([x for x in t.split() if len(x)>2 and x not in stop][:18])

def clean_url(base, href):
    if not href: return None
    href=href.strip()
    if href.startswith(('mailto:','tel:','javascript:','#')): return None
    u=urljoin(base,href); u,_=urldefrag(u)
    return u if urlparse(u).scheme.startswith('http') else None

def same_domain(a,b): return urlparse(a).netloc.lower().replace('www.','') == urlparse(b).netloc.lower().replace('www.','')
def bad_url(u): return any(p in u.lower() for p in BAD_URL_PATTERNS)
def article_url(u): return (not bad_url(u)) and any(h in u.lower() for h in ARTICLE_PATH_HINTS)

def fetch(url, timeout=5):
    try:
        r=requests.get(url,headers=HEADERS,timeout=timeout,allow_redirects=True,stream=True)
        if r.status_code>=400: return None
        ctype=r.headers.get('content-type','').lower()
        if 'image/' in ctype or 'video/' in ctype or 'application/pdf' in ctype: return None
        chunks=[]; total=0
        for c in r.iter_content(65536):
            if not c: continue
            total += len(c)
            if total > 1_500_000: return None
            chunks.append(c)
        return b''.join(chunks).decode(r.encoding or 'utf-8', errors='replace')
    except Exception: return None

def extract_links(source, html_text):
    soup=BeautifulSoup(html_text,'html.parser'); links=[]
    for a in soup.find_all('a', href=True):
        u=clean_url(source.url,a.get('href',''))
        if not u or not same_domain(source.url,u) or bad_url(u): continue
        txt=normalize_text(a.get_text(' '))
        if article_url(u) or len(txt)>=25: links.append(u)
    out=[]; seen=set()
    for u in links:
        if u not in seen: out.append(u); seen.add(u)
    return out[:18]

def discover_sections(source, html_text):
    soup=BeautifulSoup(html_text,'html.parser'); out=[]; seen=set()
    for a in soup.find_all('a', href=True):
        txt=normalize_text(a.get_text(' ')).lower(); u=clean_url(source.url,a.get('href',''))
        if not u or not same_domain(source.url,u) or bad_url(u): continue
        if any(h in txt or h in u.lower() for h in SECTION_HINTS) and u!=source.url and u not in seen:
            out.append(u); seen.add(u)
    return out[:2]

def summarize(text, n=2):
    text=normalize_text(text)
    parts=[p.strip() for p in re.split(r'(?<=[.!?։۔])\s+', text) if len(p.strip())>10]
    return (' '.join(parts[:n]) if parts else text[:350])[:600]

def parse_time(soup, config):
    tz=ZoneInfo(config['settings']['timezone']); cand=[]
    for attrs in [{'property':'article:published_time'},{'name':'pubdate'},{'name':'publishdate'},{'itemprop':'datePublished'},{'property':'og:updated_time'}]:
        tag=soup.find('meta', attrs=attrs)
        if tag and tag.get('content'): cand.append(tag.get('content'))
    for t in soup.find_all('time'):
        cand.append(t.get('datetime') or t.get_text(' '))
    for c in cand:
        try:
            dt=dateparser.parse(c, fuzzy=True, dayfirst=True)
            if dt:
                if dt.tzinfo is None: dt=dt.replace(tzinfo=tz)
                return dt.astimezone(tz)
        except Exception: pass
    return None

def excluded(text, config): return any(k.lower() in text.lower() for k in config['settings'].get('exclude_keywords',[]))
def bad_title(t): return any(p in t.lower() for p in BAD_TITLE_PATTERNS)
def az_sport(text, config): return any(h.lower() in text.lower() for h in config['settings'].get('az_sport_hints',[]))

def sport_type(text):
    low=text.lower(); mp=[('football',['futbol','football','premyer liqa','misli','qarabağ','neftçi','zirə','sabah','qəbələ']),('basketball',['basketbol','basketball']),('volleyball',['voleybol']),('mma',['mma','ufc']),('judo',['cüdo','judo']),('wrestling',['güləş','wrestling']),('chess',['şahmat','chess'])]
    for s,ks in mp:
        if any(k in low for k in ks): return s
    return 'other'
def topic(s): return 'football' if s in {'football','futsal'} else 'other'
def priority(text):
    low=text.lower()
    if any(k in low for k in HIGH): return 0
    if any(k in low for k in MEDIUM): return 1
    return 2

def recent(dt, config):
    tz=ZoneInfo(config['settings']['timezone']); window=int(config['settings'].get('fresh_window_minutes',60))
    return dt >= datetime.now(tz) - timedelta(minutes=window)

def extract_article(source,url,config):
    if not article_url(url): return None
    h=fetch(url)
    if not h: return None
    soup=BeautifulSoup(h,'html.parser')
    title=''
    og=soup.find('meta', property='og:title')
    if og and og.get('content'): title=og.get('content')
    if not title and soup.find('h1'): title=soup.find('h1').get_text(' ')
    if not title and soup.title: title=soup.title.get_text(' ')
    title=normalize_text(title)
    if len(title)<8 or bad_title(title): return None
    desc=''
    for selector in [('meta',{'property':'og:description'}),('meta',{'name':'description'})]:
        tag=soup.find(*selector)
        if tag and tag.get('content'): desc=tag.get('content'); break
    if not desc:
        ps=[normalize_text(p.get_text(' ')) for p in soup.find_all('p')]
        desc=' '.join([p for p in ps if len(p)>25][:3])
    desc=summarize(desc,2); raw=normalize_text(title+' '+desc)
    if excluded(raw,config) or not az_sport(raw,config): return None
    dt=parse_time(soup,config)
    first_seen=datetime.now(ZoneInfo(config['settings']['timezone']))

    # v5.8: softer freshness filter.
    # If date exists, keep freshness window.
    # If date is missing, allow once by first_seen; PostgreSQL memory prevents repeats.
    if dt:
        if not recent(dt,config): return None
    else:
        dt=first_seen

    st=sport_type(raw)
    return NewsItem(source,url,title,desc,dt,first_seen,st,topic(st),priority(raw),raw)

def sent_or_pending(db,item,config):
    key=semantic_key(item.title); cutoff=(datetime.now(timezone.utc)-timedelta(hours=int(config['settings'].get('sent_memory_hours',72)))).isoformat()
    for u,sem,title in db.rows('SELECT url, semantic_key, title FROM sent_items WHERE sent_at >= ?', (cutoff,)):
        if u==item.url or (sem and fuzz.token_set_ratio(sem,key)>=88) or (title and fuzz.token_set_ratio(title.lower(),item.title.lower())>=90): return True
    for u,sem,title in db.rows('SELECT url, semantic_key, title FROM pending_items',()):
        if u==item.url or (sem and fuzz.token_set_ratio(sem,key)>=88) or (title and fuzz.token_set_ratio(title.lower(),item.title.lower())>=90): return True
    return False

def add_pending(db,items):
    added=0
    for item in items:
        try:
            db.q('INSERT INTO pending_items (url,title,description,published_at,first_seen_at,source_name,source_url,sport_type,topic,priority,raw_text,semantic_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (item.url,item.title,item.description,item.published_at.isoformat(),item.first_seen_at.isoformat(),item.source.name,item.source.url,item.sport_type,item.topic,item.priority,item.raw_text,semantic_key(item.title),now_utc()))
            added+=1
        except Exception: pass
    return added

def row_item(r):
    u,t,d,pub,fs,sn,su,st,tp,pr,raw=r
    return NewsItem(Source(sn,su),u,t,d,dateparser.parse(pub),dateparser.parse(fs),st,tp,int(pr),raw)

def pending_items(db,config):
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=int(config['settings'].get('pending_backlog_minutes',180)))).isoformat()
    db.q('DELETE FROM pending_items WHERE created_at < ?', (cutoff,))
    return [row_item(r) for r in db.rows('SELECT url,title,description,published_at,first_seen_at,source_name,source_url,sport_type,topic,priority,raw_text FROM pending_items',())]

def mark_sent(db,items,config):
    now=now_utc()
    for item in items:
        try: db.q('INSERT INTO sent_items (url,title_hash,semantic_key,title,source,sent_at) VALUES (?,?,?,?,?,?)', (item.url,sha(item.title),semantic_key(item.title),item.title,item.source.name,now))
        except Exception: pass
        db.q('DELETE FROM pending_items WHERE url=?',(item.url,))

def record_failure(db,name):
    rows=db.rows('SELECT consecutive_days FROM failures WHERE source=?',(name,))
    if rows: db.q('UPDATE failures SET last_failed_at=?, consecutive_days=consecutive_days+1 WHERE source=?',(now_utc(),name))
    else: db.q('INSERT INTO failures(source,last_failed_at,consecutive_days,disabled) VALUES (?,?,1,0)',(name,now_utc()))
def clear_failure(db,name): db.q('DELETE FROM failures WHERE source=?',(name,))
def disabled(db): return {r[0] for r in db.rows('SELECT source FROM failures WHERE consecutive_days >= 10',())}

def scan(config,db):
    sources=[Source(**s) for s in config['sources']]; dis=disabled(db); found=[]; failed=[]
    for idx,source in enumerate(sources,1):
        print(f'[{idx}/{len(sources)}] Scanning {source.name}: {source.url}', flush=True)
        if source.name in dis: failed.append(source.name+' (отключён после 10 дней ошибок)'); continue
        main=fetch(source.url)
        if not main:
            print(f'  FAILED: cannot open {source.name}', flush=True); failed.append(source.name); record_failure(db,source.name); continue
        clear_failure(db,source.name); print('  opened', flush=True)
        pages=[source.url]+discover_sections(source,main); print(f'  pages to check: {len(pages)}', flush=True)
        cand=[]
        for p in pages:
            ph=main if p==source.url else fetch(p)
            if ph: cand.extend(extract_links(source,ph))
        uniq=[]; seen=set()
        for u in cand:
            if u not in seen: uniq.append(u); seen.add(u)
        print(f'  candidate article links: {len(uniq[:12])}', flush=True)
        sf=0
        for u in uniq[:12]:
            item=extract_article(source,u,config)
            if item and not sent_or_pending(db,item,config): found.append(item); sf+=1
        print(f'  new relevant items from {source.name}: {sf}', flush=True)
        try: del main,pages,cand,uniq,seen
        except Exception: pass
        gc.collect()
    print(f'Scan finished. Total new relevant items found: {len(found)}', flush=True)
    return found,failed

def dedupe(items):
    groups=[]
    for item in items:
        key=semantic_key(item.title); placed=False
        for primary,dupes in groups:
            if fuzz.token_set_ratio(key,semantic_key(primary.title))>=88 or fuzz.token_set_ratio(item.title,primary.title)>=90:
                dupes.append(item); placed=True; break
        if not placed: groups.append((item,[]))
    return groups

def order(items): return sorted(items, key=lambda x:(x.topic!='football',x.priority,-x.published_at.timestamp()))
def fmt_dt(dt,tz): return dt.astimezone(ZoneInfo(tz)).strftime('%H:%M')
def plabel(p): return '🔥 Важно' if p==0 else ('🟡 Среднее' if p==1 else '⚪ Низкое')

def build_email(config,groups,failed):
    tz=config['settings']['timezone']; now=datetime.now(ZoneInfo(tz)); subject=f"{config['settings']['digest_name']} — {now.strftime('%H:%M')}"
    emojis=config['settings'].get('sport_emoji',{}); text=[subject,'']; html_lines=[f'<h2>{html.escape(subject)}</h2>']
    for key,title in [('football','⚽ Футбол'),('other','🏅 Другие виды спорта')]:
        sec=[g for g in groups if g[0].topic==key]
        if not sec: continue
        text += [title,'']; html_lines.append(f'<h3>{html.escape(title)}</h3>')
        for primary,dupes in sec:
            emoji=emojis.get(primary.sport_type,emojis.get('other','🏅')); ts=fmt_dt(primary.published_at,tz); desc=summarize(primary.description,2)
            also=[]
            for d in dupes:
                if d.source.name!=primary.source.name and d.source.name not in also: also.append(d.source.name)
            text += [f'{emoji} {ts} — {plabel(primary.priority)}', primary.title, desc, f'Источник: {primary.source.name}'+(f' (+{len(also)})' if also else '')]
            if also: text.append('Также: '+', '.join(also[:3])+(f' (+{len(also)-3})' if len(also)>3 else ''))
            text += [primary.url,'']
            html_lines.append(f"<p><strong>{emoji} {ts} — {html.escape(plabel(primary.priority))}</strong><br><strong>{html.escape(primary.title)}</strong><br>{html.escape(desc)}<br>Источник: {html.escape(primary.source.name)}{(' (+'+str(len(also))+')') if also else ''}<br><a href=\"{html.escape(primary.url)}\">{html.escape(primary.url)}</a></p>")
    if failed:
        text.append('⚠️ Не удалось открыть:'); html_lines.append('<h3>⚠️ Не удалось открыть:</h3><ul>')
        for f in sorted(set(failed)): text.append('- '+f); html_lines.append(f'<li>{html.escape(f)}</li>')
        html_lines.append('</ul>')
    return subject,'\n'.join(text),'\n'.join(html_lines)

def send_email(config,subject,text_body,html_body):
    if not SMTP_USER or not SMTP_APP_PASSWORD or not EMAIL_TO:
        print('Email env vars are missing. Set SMTP_USER, SMTP_APP_PASSWORD, EMAIL_TO.', flush=True); return
    print(f"Sending email to: {', '.join(EMAIL_TO)}", flush=True)
    msg=MIMEMultipart('alternative'); msg['From']=formataddr((config['settings'].get('sender_name','Idman Monitor'), EMAIL_FROM or SMTP_USER)); msg['To']=', '.join(EMAIL_TO); msg['Subject']=subject
    msg.attach(MIMEText(text_body,'plain','utf-8')); msg.attach(MIMEText(html_body,'html','utf-8'))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(); server.login(SMTP_USER, SMTP_APP_PASSWORD); server.sendmail(EMAIL_FROM or SMTP_USER, EMAIL_TO, msg.as_string())

def main():
    config=load_config(); db=init_db()
    print('Idman Monitor v5.8 started with '+('persistent PostgreSQL memory' if db.is_pg else 'SQLite fallback memory'), flush=True)
    if not db.is_pg: print('WARNING: set DATABASE_URL for reliable memory on Render Cron.', flush=True)
    found,failed=scan(config,db); print(f'Added to pending queue: {add_pending(db,found)}', flush=True)
    pend=pending_items(db,config); print(f'Pending queue size: {len(pend)}', flush=True)
    max_items=int(config['settings'].get('max_items_per_email',50)); selected=order(pend)[:max_items]; print(f'Selected for this email: {len(selected)}', flush=True)
    if not selected:
        print('No new items. No email sent.', flush=True); return 0
    groups=dedupe(selected); subject,text_body,html_body=build_email(config,groups,failed); send_email(config,subject,text_body,html_body)
    # Mark every selected pending item as processed, including near-duplicates hidden inside groups.
    # Items over the 50-limit stay in pending_items and will be considered in the next email.
    mark_sent(db,selected,config); print(f'Sent digest with {len(groups)} news items.', flush=True); return 0

if __name__=='__main__': raise SystemExit(main())
