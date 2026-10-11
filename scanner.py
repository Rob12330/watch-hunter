#!/usr/bin/env python3
"""Watch Hunter V3. Search public Bing RSS index; does not access marketplace pages.
Results and prices are provisional search excerpts, NOT verified ads or valuations.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'public' / 'data.json'
MAX_PRICE = 15.0
MAX_ITEMS = 300
MAX_DAYS = 14
# Rotation: a few queries each hour to avoid hammering a public RSS feed.
SEARCH_TERMS = [
    'montre vintage', 'montre ancienne', 'montre mécanique', 'montre automatique',
    'lot montres', 'montre à réparer', 'seiko montre', 'citizen montre',
    'tissot montre', 'omega montre', 'longines montre', 'lip montre',
    'yema montre', 'orient montre', 'timex montre', 'casio montre',
    'montre héritage', 'montre homme', 'montre femme', 'montre en panne',
]
SOURCES = {
    'Vinted': {'domain': 'vinted.fr', 'site': 'site:vinted.fr/items', 'search': 'https://www.vinted.fr/catalog?search_text=montre&price_to=15'},
    'Leboncoin': {'domain': 'leboncoin.fr', 'site': 'site:leboncoin.fr/ad', 'search': 'https://www.leboncoin.fr/recherche?text=montre&price_max=15'},
}
USER_AGENT = 'WatchHunterV3/1.0 (public indexed search; no marketplace crawling)'
EURO_RE = re.compile(r'(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|euros?\b)', re.I)
PREFIX_RE = re.compile(r'(?:€)\s*(\d{1,4}(?:[.,]\d{1,2})?)', re.I)
WATCH_RE = re.compile(r'\b(montre|montres|watch|horlogerie|seiko|omega|tissot|citizen|longines|lip|yema|orient|casio|timex)\b', re.I)
BRANDS = {'omega': 28, 'longines': 28, 'tissot': 23, 'seiko': 22, 'citizen': 18,
          'lip': 22, 'yema': 22, 'orient': 18, 'timex': 14, 'casio': 11,
          'festina': 11, 'swatch': 11, 'certina': 24, 'zenith': 28, 'bulova': 20,
          'hamilton': 22, 'eterna': 24, 'dugèna': 12, 'dugena': 12, 'rotary': 12,
          'kelton': 13, 'pulsar': 11, 'luch': 15}
FLAGS = [('mécanique', 13), ('automatique', 13), ('vintage', 5),
         ('ancienne', 5), ('lot', 10), ('réparer', 9), ('en panne', 9),
         ('ne fonctionne pas', 9), ('remonter', 8)]
DANGER = ('réplique', 'replica', 'copie', 'fake', 'faux', 'inspiré', 'inspirée', 'contrefaçon')

def utcnow():
    return dt.datetime.now(dt.timezone.utc)

def clean_text(s):
    s = re.sub('<[^>]*>', ' ', s or '')
    return re.sub(r'\s+', ' ', html.unescape(s)).strip()[:600]

def clean_url(raw):
    try:
        parsed = urllib.parse.urlsplit(html.unescape(raw))
        domain = (parsed.hostname or '').lower()
        domain = domain[4:] if domain.startswith('www.') else domain
        if parsed.scheme not in ('http','https'):
            return None, None
        if domain == 'vinted.fr' and re.search(r'^/items/\d+', parsed.path):
            match = re.search(r'^/items/\d+[^?#]*', parsed.path)
            return 'https://www.vinted.fr' + match.group(0).rstrip('/'), 'Vinted'
        if domain == 'leboncoin.fr' and re.search(r'^/ad/[^/]+/\d+', parsed.path):
            match = re.search(r'^/ad/[^/]+/\d+', parsed.path)
            return 'https://www.leboncoin.fr' + match.group(0), 'Leboncoin'
        # Legacy leboncoin direct links to an ad ending in .htm
        if domain == 'leboncoin.fr' and re.search(r'/\d+\.htm$', parsed.path):
            return 'https://www.leboncoin.fr' + parsed.path, 'Leboncoin'
    except (ValueError, TypeError):
        pass
    return None, None

def extract_price(title, desc):
    for source, text in [('titre', title), ('extrait', desc)]:
        match = EURO_RE.search(text) or PREFIX_RE.search(text)
        if match:
            try:
                p = float(match.group(1).replace(',', '.'))
                if 0 < p < 10000:
                    return round(p, 2), source
            except ValueError:
                pass
    return None, 'inconnu'

def score_listing(title, description, price):
    txt = (title + ' ' + description).lower()
    score = 15
    found = []
    for name, points in BRANDS.items():
        if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', txt):
            score += points
            found.append(name.capitalize())
            break
    for term, points in FLAGS:
        if term in txt:
            score += points
            if len(found) < 3: found.append(term)
    if price is not None:
        score += round((MAX_PRICE - min(price, MAX_PRICE)) / MAX_PRICE * 16)
        if price <= 5: found.append('Petit prix')
    if any(d in txt for d in DANGER):
        score -= 40
        found.append('Authenticité suspecte')
    return max(0, min(score, 99)), found[:4]

def rss_items(payload):
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as e:
        raise ValueError('Flux RSS illisible') from e
    if root.tag != 'rss' and not root.findall('.//item'):
        raise ValueError('La réponse n’est pas un flux RSS de recherche')
    for item in root.findall('.//item'):
        yield {'title': clean_text(item.findtext('title')),
               'description': clean_text(item.findtext('description')),
               'url': (item.findtext('link') or '').strip()}

def fetch(term, source):
    query = f'{source["site"]} {term}'
    url = 'https://www.bing.com/search?' + urllib.parse.urlencode({
        'q': query, 'format': 'rss', 'mkt':'fr-FR', 'setlang': 'fr', 'count': '15'})
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/xml, text/xml;q=0.9'})
    with urllib.request.urlopen(req, timeout=18) as resp:
        data = resp.read(700000)
    return list(rss_items(data))

def parse_result(row, timestamp):
    link, source = clean_url(row.get('url'))
    if not link: return None
    title = clean_text(row.get('title', ''))
    desc = clean_text(row.get('description', ''))
    if not WATCH_RE.search(title + ' ' + desc): return None
    price, price_where = extract_price(title, desc)
    if price is not None and price > MAX_PRICE:
        return None
    score, tags = score_listing(title, desc, price)
    # Terms merely mentioned in a snippet do not prove the item condition or price.
    key = hashlib.sha256(link.encode('utf-8')).hexdigest()[:20]
    return {'id': key, 'source': source, 'title': title[:160], 'url': link,
            'description': desc[:360], 'price': price, 'price_origin': price_where,
            'score': score, 'tags': tags, 'first_seen': timestamp, 'last_seen': timestamp,
            'is_verified': False}

def read_previous():
    try:
        p = json.loads(OUT.read_text('utf-8'))
        return p if isinstance(p, dict) and isinstance(p.get('items'), list) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}

def run():
    previous = read_previous()
    old = {x['id']:x for x in previous.get('items', []) if isinstance(x, dict) and 'id' in x}
    now = utcnow()
    stamp = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
    # 4 searches for each marketplace per run; all 20 terms cycle every 5 hours.
    slot = (int(now.timestamp()) // 3600) % 5
    search_terms = SEARCH_TERMS[slot*4:slot*4+4]
    status = {}
    found = {}
    for name, source in SOURCES.items():
        ok = 0; errors = []; raw_count = 0
        for term in search_terms:
            try:
                results = fetch(term, source)
                ok += 1
                raw_count += len(results)
                for row in results:
                    item = parse_result(row, stamp)
                    if item and item['source'] == name:
                        found[item['id']] = item
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                errors.append(f'{term}: {type(e).__name__}')
                print(f'[WARN] {name}, {term}: {e}', file=sys.stderr)
            time.sleep(0.4)
        status[name] = {'queries_done': ok, 'queries_total': len(search_terms),
                        'raw_results': raw_count, 'new_links': len([x for x in found.values() if x['source']==name]),
                        'state': 'ok' if ok == len(search_terms) else 'partial' if ok else 'error',
                        'errors': errors[:4]}
    merged = old.copy()
    for key, item in found.items():
        if key in merged:
            old_item = merged[key]
            item['first_seen'] = old_item.get('first_seen', stamp)
            # If search index drops displayed price, preserve earlier discovered price but flag its source.
            if item['price'] is None and old_item.get('price') is not None:
                item['price'] = old_item['price']
                item['price_origin'] = 'ancien extrait'
        merged[key] = item
    cutoff = now - dt.timedelta(days=MAX_DAYS)
    retained = []
    for item in merged.values():
        try:
            last = dt.datetime.fromisoformat(item['last_seen'].replace('Z', '+00:00'))
            if last < cutoff: continue
        except (ValueError, KeyError, TypeError):
            continue
        if item.get('price') is not None and float(item['price']) > MAX_PRICE:
            continue
        retained.append(item)
    retained.sort(key=lambda x: (x['score'], x['last_seen']), reverse=True)
    retained = retained[:MAX_ITEMS]
    data = {'version': 3, 'generated_at': stamp, 'max_price': MAX_PRICE,
            'method': 'Flux RSS du moteur de recherche (pas de collecte directe des plateformes)',
            'notice': 'Résultats d’index publics. Les prix, la disponibilité et l’authenticité ne sont pas vérifiés.',
            'keywords_checked': search_terms, 'sources': status, 'new_this_scan':len(found),
            'items': retained}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'[OK] {stamp}; nouveaux liens (non dédupliqués par exécution précédente): {len(found)}; conservés: {len(retained)}')
    for name, state in status.items(): print(f'  {name}: {state["state"]} ({state["queries_done"]}/{state["queries_total"]} flux)')

if __name__ == '__main__':
    run()
