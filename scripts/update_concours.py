#!/usr/bin/env python3
"""Collect open state concours from emploi-public.ma and write concours.json.

Runs on GitHub Actions on a schedule. Read-only: only public listing pages.
"""
import json
import re
import sys
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE = "https://www.emploi-public.ma"
LIST_URLS = [
    BASE + "/fr/concours-liste?page={page}",
    BASE + "/fr/concours-liste?stat=etab_publics&page={page}",
]
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
MAX_PAGES = 60


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
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" "))
        if "Avis de concours" not in text:
            continue
        href = a["href"]
        url = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        # The card around the link usually carries org, posts and dates.
        card = a
        for _ in range(5):
            if card.parent is None:
                break
            card = card.parent
            if "Limite de dépôt" in card.get_text():
                break
        card_text = clean(card.get_text(" "))
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
        # Title = link text up to the org name; org = text after the title.
        title = text
        org = ""
        # The link text typically repeats the title then the organisation.
        parts = text.split("Avis de concours")
        if len(parts) > 2:
            title = "Avis de concours" + parts[1]
            rest = clean("Avis de concours".join(parts[2:]))
            # rest = "<title again> <org> Annonce" -> strip the repeated title
            if rest.startswith(clean(title)):
                org = clean(rest[len(clean(title)):])
            org = re.sub(r"\s*Annonce\s*$", "", org)
        items.append({
            "title": clean(title),
            "org": clean(org),
            "deadline": deadline,
            "exam": exam,
            "posts": posts,
            "url": url,
        })
    return items


def main():
    today = date.today().isoformat()
    seen = {}
    session = requests.Session()
    session.headers.update(HEADERS)
    for template in LIST_URLS:
        for page in range(1, MAX_PAGES + 1):
            url = template.format(page=page)
            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"fetch failed {url}: {e}", file=sys.stderr)
                break
            items = parse_page(r.text)
            if not items:
                break
            new = 0
            for it in items:
                if it["url"] not in seen:
                    seen[it["url"]] = it
                    new += 1
            if new == 0:
                break
            # Lists are newest-first: stop once every dated item is past.
            dated = [i["deadline"] for i in items if i["deadline"]]
            if dated and all(d < today for d in dated):
                break
            time.sleep(1)

    concours = []
    for it in seen.values():
        if it["deadline"] and it["deadline"] < today:
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
    out = {"concours": concours, "updated": today}
    with open("concours.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(concours)} open concours")


if __name__ == "__main__":
    main()
