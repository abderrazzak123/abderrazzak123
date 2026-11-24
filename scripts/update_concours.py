#!/usr/bin/env python3
"""Collect open state concours from emploi-public.ma and write concours.json.

Runs on GitHub Actions on a schedule. Read-only: only public listing pages.
Strategy (from the portal's structure): iterate the exact-deadline filter
(datePicker, JJ/MM/AAAA) day by day for each category, keep only "Annonce"
cards, dedupe by detail URL. Plain pagination ordering is unstable at equal
deadlines, so day-by-day is the reliable way to be exhaustive.
"""
import json
import re
import sys
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

BASE = "https://www.emploi-public.ma"
# --- results & news sources watched in every run (beyond emploi-public) ---
# Each source: (display name, url, keywords matched against link text)
RESULT_SOURCES = [
    ("وزارة التربية الوطنية", "https://www.men.gov.ma/Fr/Pages/Accueil.aspx",
     ["لوائح", "نتائج", "مباراة التعليم", "المراكز الجهوية", "CRMEF", "liste", "résultat"]),
    ("الجمارك — المالية", "https://e-recrutement.finances.gov.ma/",
     ["résultat", "liste", "نتائج", "لوائح", "ADII", "douane"]),
    ("الجامعات", "https://recrutement.enssup.gov.ma/",
     ["résultat", "liste", "نتائج", "لوائح", "avis"]),
    ("maroc-concour", "https://www.maroc-concour.com/",
     ["نتائج", "لوائح"]),
    ("recrutements.ma", "https://www.recrutements.ma/",
     ["نتائج", "لوائح"]),
]
RESULT_KEYWORDS = ["لوائح", "نتائج", "liste", "résultat", "resultat", "convocation"]
CATEGORIES = ["service_etat", "etab_publics", "collec"]
LIST_TMPL = (BASE + "/fr/concours-liste?key_word=&stat={stat}&corps=0&region=0"
             "&datePicker={day}&date_from=&date_to=&procedure=0&page={page}")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MyConcoursBot/1.0; +https://abderrazzak123.github.io/abderrazzak123/)",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})")
DAYS_AHEAD = 120
EMPTY_STREAK_STOP = 21
MAX_REQUESTS = 4000


def fr_date_to_iso(text):
    m = DATE_RE.search(text or "")
    if not m:
        return ""
    day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = MONTHS.get(mon)
    if not month:
        return ""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    results = []
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" "))
        if "Avis de concours" not in text:
            continue
        href = a["href"]
        url = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        card = a
        for _ in range(5):
            if card.parent is None:
                break
            card = card.parent
            if "Limite de dépôt" in card.get_text():
                break
        card_text = clean(card.get_text(" "))
        # Results publications become news too (not only announcements).
        if "Résultats" in card_text or "Resultats" in card_text:
            results.append({"title": re.split(r"\s+Annonce\s+", clean(text))[0][:120], "url": url})
            continue
        # Keep announcements only (skip Convocation / Résultats publications).
        if "Annonce" not in card_text:
            continue
        deadline = ""
        m = re.search(r"Limite de dépôt\s*:\s*([^|]+)", card_text)
        if m:
            deadline = fr_date_to_iso(m.group(1))
        exam = ""
        m = re.search(r"Date du concours\s*:\s*([^|]+)", card_text)
        if m:
            exam = fr_date_to_iso(m.group(1))
        posts = ""
        m = re.search(r"(\d+)\s*postes?", card_text)
        if m:
            posts = m.group(1)
        title = text
        org = ""
        parts = text.split("Avis de concours")
        if len(parts) > 2:
            title = "Avis de concours" + parts[1]
            rest = clean("Avis de concours".join(parts[2:]))
            if rest.startswith(clean(title)):
                org = clean(rest[len(clean(title)):])
            org = re.sub(r"\s*Annonce\s*$", "", org)
        # The card link text embeds badge + dates; cut them from the title.
        title = re.split(r"\s+Annonce\s+", clean(title))[0]
        title = re.split(r"\s+Limite de dépôt\s*:", title)[0]
        title = re.split(r"\s+Date du concours\s*:", title)[0]
        org = re.split(r"\s+Annonce\s+", clean(org))[0]
        items.append({
            "title": clean(title),
            "org": clean(org),
            "deadline": deadline,
            "exam": exam,
            "posts": posts,
            "url": url,
        })
    return items, results


def check_sources(session, seen_urls):
    """Fetch each watched source page; return news items for new result/list links."""
    found = []
    for name, url, keywords in RESULT_SOURCES:
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"source failed {name} ({url}): {e}", file=sys.stderr)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        n = 0
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text(" "))
            if len(text) < 8:
                continue
            low = text.lower()
            if not any(k.lower() in low for k in keywords):
                continue
            href = a["href"]
            if href.startswith("#") or href.startswith("javascript"):
                continue
            link = href if href.startswith("http") else url.rstrip("/") + "/" + href.lstrip("/")
            if link in seen_urls or link in {f["link"] for f in found}:
                continue
            found.append({
                "date": date.today().isoformat(),
                "title": "نتائج ولوائح: " + text[:120],
                "body": name,
                "link": link,
            })
            n += 1
            if n >= 5:
                break
        print(f"source {name}: {n} new links")
    return found


def main():
    today = date.today()
    seen = {}
    ep_results = []
    session = requests.Session()
    session.headers.update(HEADERS)
    requests_made = 0
    for stat in CATEGORIES:
        empty_streak = 0
        for offset in range(DAYS_AHEAD + 1):
            if requests_made >= MAX_REQUESTS:
                break
            day = today + timedelta(days=offset)
            day_str = day.strftime("%d/%m/%Y")
            day_items = 0
            for page in range(1, 40):
                if requests_made >= MAX_REQUESTS:
                    break
                url = LIST_TMPL.format(stat=stat, day=day_str, page=page)
                try:
                    r = session.get(url, timeout=30)
                    r.raise_for_status()
                except requests.RequestException as e:
                    print(f"fetch failed {url}: {e}", file=sys.stderr)
                    break
                requests_made += 1
                items, page_results = parse_page(r.text)
                for res in page_results:
                    if res["url"] not in {x["url"] for x in ep_results}:
                        ep_results.append(res)
                if not items:
                    break
                new = 0
                for it in items:
                    if it["url"] not in seen:
                        seen[it["url"]] = it
                        new += 1
                day_items += len(items)
                if new == 0:
                    break
                time.sleep(0.6)
            if day_items == 0:
                empty_streak += 1
                if offset > 30 and empty_streak >= EMPTY_STREAK_STOP:
                    break
            else:
                empty_streak = 0

    today_iso = today.isoformat()
    concours = []
    for it in seen.values():
        if it["deadline"] and it["deadline"] < today_iso:
            continue
        note = f"{it['posts']} منصب" if it["posts"] else ""
        slug = re.sub(r"[^a-z0-9]+", "-", it["url"].rsplit("/", 1)[-1].lower())[:60]
        concours.append({
            "id": "ep-" + slug,
            "title": it["title"],
            "org": it["org"],
            "deadline": it["deadline"],
            "exam": it["exam"],
            "note": note,
            "apply": it["url"],
        })
    concours.sort(key=lambda c: (c["deadline"] or "9999-12-31", c["title"]))

    # News: concours that were not in the previous run become news items.
    prev_urls = set()
    prev_news = []
    prev_seen_sources = set()
    try:
        with open("concours.json", encoding="utf-8") as f:
            prev = json.load(f)
        prev_urls = {c.get("apply", "") for c in prev.get("concours", [])}
        prev_news = prev.get("news", []) or []
        prev_seen_sources = set(prev.get("sources_seen", []) or [])
    except Exception:
        pass
    news = []
    if prev_urls:
        for c in concours:
            if c["apply"] not in prev_urls:
                body_parts = [p for p in [c["org"], c["note"]] if p]
                if c["deadline"]:
                    body_parts.append("آخر أجل: " + c["deadline"])
                news.append({
                    "date": today_iso,
                    "title": "مباراة جديدة: " + c["title"],
                    "body": " — ".join(body_parts),
                    "link": c["apply"],
                })
    # Results/lists from the watched sources (CRMEF, ADII, universities, aggregators).
    session = requests.Session()
    session.headers.update(HEADERS)
    source_news = check_sources(session, prev_seen_sources)
    for res in ep_results:
        if res["url"] not in prev_seen_sources and res["url"] not in {n["link"] for n in source_news}:
            source_news.append({
                "date": today_iso,
                "title": "نتائج: " + res["title"],
                "body": "emploi-public",
                "link": res["url"],
            })
    news = source_news + news
    seen_sources = prev_seen_sources | {n["link"] for n in source_news}
    news = (news + prev_news)[:40]

    out = {"concours": concours, "news": news, "updated": today_iso,
           "sources_seen": sorted(seen_sources)}
    with open("concours.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(concours)} open concours, {len(news)} news items from {requests_made} requests")


if __name__ == "__main__":
    main()
