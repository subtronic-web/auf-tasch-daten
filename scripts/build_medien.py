#!/usr/bin/env python3
"""Auf Tasch: sammelt alles rund um die Heimspiele und schreibt medien.json + news.json.

Quellen (alle ohne Zugangsschlüssel):
  - fch.de News: Hansa-News, Vorbericht, Nachbericht (Aufstellung, Zuschauer, Schiri, Torfolge), Vorverkauf
  - YouTube-Feeds: Hansa-TV (PK vor/nach dem Spiel), MAGENTA SPORT und Sky Sport 3. Liga (Highlights)
  - ARD-Mediathek: Nordmagazin, Sportschau u. a. (Highlights)
  - fch.de Kogge-Seite: Stadionmagazin als PDF
  - Ticketshop: direkter Link pro Heimspiel

Aufruf: python3 scripts/build_medien.py spielplan.json medien.json news.json
Gefundenes bleibt gespeichert, auch wenn es später aus einem Feed herausfällt.
Jede Quelle läuft für sich: fällt eine aus, laufen die anderen weiter.
"""
import datetime as dt
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
FCH = "https://www.fch.de"
YT_CHANNELS = {
    "hansatv": "UCj2W-sOPG-ryou81NOvgKPg",
    "MAGENTA SPORT": "UChr7ZXDFiPOEl543HM4Aj8g",
    "Sky Sport": "UC6jFiP9XyTyzvobcQOHtcKg",
}
ARD_SHOWS = ("nordmagazin", "sportschau", "ndr", "br24sport", "sport im osten")
MONATE = {m: i + 1 for i, m in enumerate(["januar", "februar", "maerz", "april", "mai", "juni", "juli",
                                           "august", "september", "oktober", "november", "dezember"])}
STOP = {"fc", "sc", "sg", "sv", "tsv", "ssv", "msv", "tsg", "vfb", "vfl", "ii", "rot", "weiss", "1", "04", "1899",
        "sonnenhof", "fortuna", "preussen", "viktoria", "kickers", "alemannia"}
FEHLER = []


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def norm(t):
    t = html.unescape(str(t or "")).lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    return t


def keywords(opp):
    """Erkennungswörter für den Gegner, z. B. 'TSV Havelse' -> ['havelse']."""
    toks = [w for w in re.split(r"[^a-z0-9]+", norm(opp)) if len(w) >= 4 and w not in STOP]
    if not toks:  # z. B. 'SC Preußen Münster' hat nach STOP noch 'muenster'; Sicherheitsnetz für Sonderfälle
        toks = [w for w in re.split(r"[^a-z0-9]+", norm(opp)) if len(w) >= 4]
    return toks


def mentions(text, opp):
    t = norm(text)
    return any(k in t for k in keywords(opp))


def day(s):
    return dt.date.fromisoformat(s[:10])


def strip_tags(h):
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def step(name, fn):
    try:
        fn()
    except Exception as e:  # eine kaputte Quelle darf die anderen nicht stoppen
        FEHLER.append(f"{name}: {type(e).__name__}: {e}"[:300])


# ---------------------------------------------------------------- Daten laden
spielplan_path, medien_path, news_path = sys.argv[1], sys.argv[2], sys.argv[3]
plan = json.load(open(spielplan_path, encoding="utf-8"))
HEIM = sorted([g for g in plan.get("spiele", []) if g.get("start")], key=lambda g: g["start"])
try:
    MED = json.load(open(medien_path, encoding="utf-8"))
except Exception:
    MED = {}
MED.setdefault("spiele", {})
import os
HEUTE = dt.date.fromisoformat(os.environ["AUFTASCH_HEUTE"]) if os.environ.get("AUFTASCH_HEUTE") else dt.datetime.now(dt.timezone(dt.timedelta(hours=1))).date()


def slot(g):
    s = MED["spiele"].setdefault(g["start"], {})
    s["opp"] = g["opp"]
    return s


def window(g, before, after):
    d = day(g["start"])
    return d - dt.timedelta(days=before), d + dt.timedelta(days=after)


def relevant(g, before=10, after=6):
    a, b = window(g, before, after)
    return a <= HEUTE <= b


# ---------------------------------------------------------------- fch.de News
NEWS = []


def fch_news():
    for page in ("news.html", "news.html?page_n7=2"):
        h = get(f"{FCH}/{page}")
        for blk in re.findall(r'<div class="newsholder.*?</p>', h, flags=re.S):
            m_url = re.search(r'<h3><a href="([^"]+)">(.*?)</a></h3>', blk, flags=re.S)
            m_dt = re.search(r'<span class="date">(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})', blk)
            if not (m_url and m_dt):
                continue
            d, mo, y, hh, mi = m_dt.groups()
            teaser = strip_tags(blk.split("</h3>", 1)[-1])
            url = urllib.parse.urljoin(FCH + "/", m_url.group(1))
            if any(n["url"] == url for n in NEWS):
                continue
            NEWS.append({"titel": strip_tags(m_url.group(2)), "url": url, "datum": f"{y}-{mo}-{d}",
                         "zeit": f"{hh}:{mi}", "teaser": teaser})


ARTIKEL = {}


def artikel(url):
    """Text des Artikels ab der Überschrift bis 'zurück' (ohne Menüs)."""
    if url not in ARTIKEL:
        h = get(url)
        m = re.search(r'og:title" content="([^"]+)"', h) or re.search(r"<h2[^>]*>(.*?)</h2>", h, flags=re.S)
        titel = strip_tags(m.group(1)) if m else ""
        text = strip_tags(h)
        i = text.find(titel) if titel else -1
        j = text.find("‹ zurück", i if i >= 0 else 0)
        ARTIKEL[url] = {"titel": titel, "text": text[i if i >= 0 else 0: j if j > 0 else None]}
    return ARTIKEL[url]["text"]


def titel_von(url, fallback):
    try:
        artikel(url)
        return ARTIKEL[url]["titel"] or fallback
    except Exception:
        return fallback


def kategorie(n):
    t = norm(n["titel"])
    if re.search(r"frauen|u1\d|u2\d|nachwuchs|historisch|weihnacht|sticker|laternen|training", t):
        return ""
    if "ticket-info" in t or "vorverkauf" in t:
        return "Tickets"
    if "fan-infos" in t or "fan-info" in t:
        return "Fan-Infos"
    if re.search(r"\b\d{1,2}:\d{1,2}\b", n["titel"]) or "heimspiel" in t or "gegen" in t or "punkt" in t or "sieg" in t:
        return "Spiel"
    # seit 20.11.2026: alle Profi-News in die App (Wunsch David), nur Frauen/Nachwuchs/Historie/Aktionen raus
    if re.search(r"gewinnspiel|verlosung|fanshop|rabatt|gutschein", t):
        return ""
    if re.search(r"ansetzung|terminier|spielplan|anstosszeit", t):
        return "Spielplan"
    return "Verein"


def berichte():
    for g in HEIM:
        if not relevant(g, 5, 3):
            continue
        s = slot(g)
        d = day(g["start"])
        for n in NEWS:
            nd = day(n["datum"])
            text_kurz = n["titel"] + " " + n["teaser"]
            # Nachbericht: am Spieltag oder in der Nacht danach, mit Torfolge im Text
            if "nachbericht" not in s and d <= nd <= d + dt.timedelta(days=1):
                body = artikel(n["url"])
                if "Torfolge" in body and mentions(body, g["opp"]):
                    s["nachbericht"] = {"titel": n["titel"], "url": n["url"]}
                    m = re.search(r"FCH:\s*(.+?)\s+Zuschauer:\s*([\d.]+)", body)
                    if m:
                        s["xi"] = m.group(1).strip()
                        s["zus"] = m.group(2).strip(".")
                    m = re.search(r"Schiedsrichter(?:in)?:\s*(.+?)\s+Torfolge:", body)
                    if m:
                        s["schiri"] = m.group(1).strip()
                    m = re.search(r"Torfolge:\s*(.+?)(?:\s+‹|\s+zurück|$)", body)
                    if m:
                        s["torfolge"] = m.group(1).strip()[:300]
            # Vorbericht: bis zu 4 Tage vorher, nennt den Gegner, ist kein Ticket- oder Fan-Info-Artikel
            if "vorbericht" not in s and d - dt.timedelta(days=4) <= nd <= d and kategorie(n) not in ("Tickets", "Fan-Infos"):
                if mentions(text_kurz, g["opp"]) and "Torfolge" not in artikel(n["url"]):
                    s["vorbericht"] = {"titel": n["titel"], "url": n["url"]}


def datum_aus(text, ref_year, ref_month):
    """'Mittwoch (11. November) um 15 Uhr' oder '13.11.' -> ISO-Datum."""
    t = norm(text)
    if "startet" in t:
        t = t[t.find("startet"):]
    m = re.search(r"(\d{1,2})\.\s*(" + "|".join(MONATE) + r")", t)
    if m:
        d, mo = int(m.group(1)), MONATE[m.group(2)]
    else:
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.", t)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
    y = ref_year + (1 if mo < ref_month - 6 else 0)
    return f"{y:04d}-{mo:02d}-{d:02d}"


def vorverkauf():
    for n in NEWS:
        if kategorie(n) != "Tickets" or "auswaerts" in norm(n["titel"]):
            continue
        body = artikel(n["url"])
        y, mo = int(n["datum"][:4]), int(n["datum"][5:7])
        mem = free = None
        m = re.search(r"Vorverkauf für Mitglieder\W+.{0,140}", body) or re.search(r"Mitglied.{0,140}?(?:ab|startet).{0,60}", body)
        if m:
            mem = datum_aus(m.group(0), y, mo)
        m = re.search(r"[Ff]reie(?:r|n)? Verkauf\W+.{0,140}", body)
        if m:
            free = datum_aus(m.group(0), y, mo)
        if not (mem or free):
            continue
        for g in HEIM:
            if day(g["start"]) < day(n["datum"]):
                continue
            if mentions(n["titel"], g["opp"]) or mentions(body[:800], g["opp"]):
                s = slot(g)
                s["vvk"] = {k: v for k, v in (("mem", mem), ("free", free), ("url", n["url"])) if v}


# ---------------------------------------------------------------- YouTube
def yt_feed(cid):
    x = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(x).findall("a:entry", ns):
        out.append({"id": e.find("yt:videoId", ns).text, "titel": e.find("a:title", ns).text or "",
                    "datum": (e.find("a:published", ns).text or "")[:10]})
    return out


def youtube():
    hansa = yt_feed(YT_CHANNELS["hansatv"])
    for g in HEIM:
        if not relevant(g):
            continue
        s = slot(g)
        d = day(g["start"])
        for v in hansa:
            t = norm(v["titel"])
            if "pk" not in t or not mentions(v["titel"], g["opp"]) or "rostock vs" not in t.replace("f.c. hansa ", "hansa "):
                continue
            vd = day(v["datum"])
            if "pk vor" in t and d - dt.timedelta(days=6) <= vd <= d:
                s.setdefault("pkVor", v["id"])
            if "pk nach" in t and d <= vd <= d + dt.timedelta(days=1):
                s.setdefault("pkNach", v["id"])
    for name in ("MAGENTA SPORT", "Sky Sport"):
        try:
            feed = yt_feed(YT_CHANNELS[name])
        except Exception as e:
            FEHLER.append(f"YouTube {name}: {e}"[:200])
            continue
        for g in HEIM:
            s = slot(g) if relevant(g, 0, 5) else None
            if s is None:
                continue
            d = day(g["start"])
            for v in feed:
                t = norm(v["titel"])
                if "highlights" in t and "rostock" in t and mentions(v["titel"], g["opp"]) and d <= day(v["datum"]) <= d + dt.timedelta(days=4):
                    hl = s.setdefault("highlights", [])
                    if not any(h["name"] == name for h in hl):
                        hl.append({"name": name, "url": f"https://www.youtube.com/watch?v={v['id']}"})


# ---------------------------------------------------------------- ARD-Mediathek
def ard():
    for g in HEIM:
        if not relevant(g, 0, 5):
            continue
        s = slot(g)
        d = day(g["start"])
        q = urllib.parse.quote(f"Hansa Rostock {keywords(g['opp'])[0] if keywords(g['opp']) else g['opp']}")
        data = json.loads(get(f"https://api.ardmediathek.de/page-gateway/widgets/ard/search/vod?searchString={q}&pageSize=20"))
        for t in data.get("teasers", []):
            show = ((t.get("show") or {}).get("title") or "")
            title = t.get("longTitle") or t.get("shortTitle") or ""
            when = (t.get("broadcastedOn") or "")[:10]
            if not when or not (d <= day(when) <= d + dt.timedelta(days=3)):
                continue
            if not any(k in norm(show) for k in ARD_SHOWS) or not mentions(title, g["opp"]) or "rostock" not in norm(title):
                continue
            uid = (t.get("links", {}).get("target", {}) or {}).get("urlId") or t.get("id")
            if not uid:
                continue
            name = "NDR / Nordmagazin" if "nordmagazin" in norm(show) else "Sportschau / ARD" if "sportschau" in norm(show) else show
            hl = s.setdefault("highlights", [])
            if not any(h["name"] == name for h in hl):
                hl.insert(0, {"name": name, "url": f"https://www.ardmediathek.de/video/{uid}"})


# ---------------------------------------------------------------- Kogge (Stadionmagazin)
def kogge():
    h = get(f"{FCH}/news-media/kogge.html")
    saison_heim = [g for g in HEIM if g["start"] >= "2026-07-01"]
    for nr, pdf in re.findall(r'Kogge (\d+)\s*(?:&nbsp;|\s)*2026/2027</div>.*?pdfUrl:"([^"]+)"', h, flags=re.S):
        n = int(nr)
        if 1 <= n <= len(saison_heim):
            s = slot(saison_heim[n - 1])
            s["kogge"] = {"nr": n, "url": urllib.parse.urljoin(FCH + "/", pdf)}


# ---------------------------------------------------------------- Ticketshop
def shop():
    h = get("https://tickets.fc-hansa.de/shop?shopid=103&wes=empty_session_103&language=1&nextstate=2")
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, flags=re.S):
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        if ev.get("@type") != "Event" or "ostseestadion" not in norm((ev.get("location") or {}).get("name", "")):
            continue
        for g in HEIM:
            if mentions(ev.get("name", ""), g["opp"]) and abs((day(ev.get("startDate", g["start"])) - day(g["start"])).days) <= 10:
                slot(g)["shop"] = ev.get("url")


# ---------------------------------------------------------------- FotMob: Karten, Schiri, Zuschauer, Aufstellung
FOTMOB_LIGEN = (208, 209)  # 3. Liga, DFB-Pokal


def fotmob_min(e):
    t = e.get("time")
    o = e.get("overloadTime")
    return f"{t}.+{o}" if o else f"{t}."


def fotmob_aufstellung(md):
    team = (md.get("content", {}).get("lineup") or {}).get("homeTeam") or {}
    st = team.get("starters") or []
    if not st:
        return ""
    raus = {}
    for e in (md["content"].get("matchFacts", {}).get("events", {}) or {}).get("events", []):
        if e.get("type") == "Substitution" and e.get("isHome") and len(e.get("swap") or []) == 2:
            rein, aus = e["swap"][0].get("name", ""), e["swap"][1].get("name", "")
            raus[aus] = f"{e.get('time')}. {rein.split()[-1] if rein else ''}".strip()
    reihen = {}
    for p in st:
        y = round(((p.get("verticalLayout") or {}).get("y") or 0), 2)
        reihen.setdefault(y, []).append(p)
    teile = []
    for y in sorted(reihen):
        namen = []
        for p in sorted(reihen[y], key=lambda p: p.get("positionId") or 0):
            n = p.get("lastName") or (p.get("name") or "").split()[-1]
            if p.get("name") in raus:
                n += f" ({raus[p['name']]})"
            namen.append(n)
        teile.append(", ".join(namen))
    return " – ".join(teile)


def fotmob():
    spiele = {}
    for lid in FOTMOB_LIGEN:
        L = json.loads(get(f"https://www.fotmob.com/api/data/leagues?id={lid}"))
        for m in (L.get("fixtures", {}) or {}).get("allMatches", []) or []:
            if "rostock" in norm(m.get("home", {}).get("name", "")):
                utc = (m.get("status") or {}).get("utcTime") or ""
                if utc:
                    d = dt.datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=1))).date().isoformat()
                    spiele[d] = m
    for g in HEIM:
        m = spiele.get(g["start"])
        if not m or not (m.get("status") or {}).get("finished"):
            continue
        s = slot(g)
        if s.get("fotmob") == m["id"] and not relevant(g, 0, 3):
            continue  # schon fertig eingelesen; in den Tagen nach dem Spiel trotzdem nachschauen
        md = json.loads(get(f"https://www.fotmob.com/api/data/matchDetails?matchId={m['id']}"))
        mf = md.get("content", {}).get("matchFacts", {}) or {}
        karten = []
        for e in (mf.get("events", {}) or {}).get("events", []):
            if e.get("type") == "Card":
                art = {"Yellow": None, "YellowRed": "gr", "Red": "r"}.get(e.get("card"), None)
                name = e.get("lastName") or e.get("nameStr") or ((e.get("player") or {}).get("name") or "").split()[-1]
                k = ["H" if e.get("isHome") else "A", name, fotmob_min(e)]
                if art:
                    k.append(art)
                karten.append(k)
        s["karten"] = karten
        ib = mf.get("infoBox", {}) or {}
        ref = (ib.get("Referee") or {}).get("text")
        if ref:
            s["schiri"] = ref
        att = ib.get("Attendance")
        if att and not s.get("zus"):
            s["zus"] = f"{int(att):,}".replace(",", ".")
        xi = fotmob_aufstellung(md)
        if xi and not s.get("xi"):
            s["xi"] = xi
        s["fotmob"] = m["id"]


# ---------------------------------------------------------------- Ablauf
step("fch.de News", fch_news)
step("Berichte", berichte)
step("Vorverkauf", vorverkauf)
step("YouTube", youtube)
step("ARD", ard)
step("Kogge", kogge)
step("Ticketshop", shop)
step("FotMob", fotmob)

MED["fehler"] = FEHLER
neu = json.dumps(MED, ensure_ascii=False, indent=1, sort_keys=True)
alt = None
try:
    alt = json.dumps({**json.load(open(medien_path, encoding="utf-8")), "stand": None}, ensure_ascii=False, indent=1, sort_keys=True)
except Exception:
    pass
if alt != json.dumps({**MED, "stand": None}, ensure_ascii=False, indent=1, sort_keys=True):
    MED["stand"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    json.dump(MED, open(medien_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)

# news.json (App: Wisch-Leiste „Hansa News“ mit den letzten 10, Ticket-News separat)
if NEWS:
    wahl = []
    for n in NEWS:
        k = kategorie(n)
        if not k:
            continue
        e = {"titel": titel_von(n["url"], n["titel"]), "datum": n["datum"], "kategorie": k, "url": n["url"]}
        if k == "Tickets":
            v = next((x["vvk"] for x in MED["spiele"].values() if (x.get("vvk") or {}).get("url") == n["url"]), None)
            wd = lambda iso: ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][day(iso).weekday()] + " " + iso[8:10] + "." + iso[5:7] + "."
            if v:
                e["kurz"] = " · ".join(x for x in [("Mitglieder ab " + wd(v["mem"])) if v.get("mem") else "", ("freier Verkauf ab " + wd(v["free"])) if v.get("free") else ""] if x)
            else:
                e["kurz"] = re.sub(r"\s*\.*\s*News lesen.*$", "", n["teaser"])[:160]
        wahl.append(e)
        if len(wahl) >= 14:  # App zeigt die letzten 10 (ohne Ticket-News, die stehen bei Tickets & Vorverkauf)
            break
    try:
        alt_news = json.load(open(news_path, encoding="utf-8")).get("news")
    except Exception:
        alt_news = None
    if wahl and wahl != alt_news:
        json.dump({"aktualisiert": HEUTE.isoformat(), "news": wahl}, open(news_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

print("Fehler:", FEHLER or "keine")
