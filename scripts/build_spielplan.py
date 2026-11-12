#!/usr/bin/env python3
"""Liest den calovo-Kalender (ICS) und schreibt spielplan.json (nur Hansa-Heimspiele ab Saison 2026/27)."""
import sys, re, json, datetime, urllib.request

SRC, OUT = sys.argv[1], sys.argv[2]
if SRC.startswith("http"):
    req = urllib.request.Request(SRC, headers={"User-Agent": "auf-tasch-spielplan/1.0"})
    text = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
else:
    text = open(SRC, encoding="utf-8", errors="ignore").read()
text = re.sub(r"\r?\n[ \t]", "", text)

def field(ev, key):
    m = re.search(r"^" + key + r"[^:\n]*:(.*)$", ev, re.M)
    return m.group(1).strip() if m else ""

def iso(d):
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"

games = []
for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
    full = field(ev, "SUMMARY")
    summ = full.split("|")[0].strip()
    liga = full.split("|")[1].strip() if "|" in full else ""
    unconfirmed = summ.startswith("*")
    summ = summ.lstrip("* ").strip()
    if " - " not in summ:
        continue
    home, away = [x.strip() for x in summ.split(" - ", 1)]
    if "Hansa Rostock" not in home:
        continue
    ds = re.search(r"^DTSTART([^:\n]*):(\d{8})(T(\d{4}))?", ev, re.M)
    if not ds:
        continue
    start = iso(ds.group(2))
    if start < "2026-07-01":
        continue
    time = None
    end = None
    if ds.group(4) and not unconfirmed:
        time = ds.group(4)[:2] + ":" + ds.group(4)[2:]
    else:
        de = re.search(r"^DTEND[^:\n]*:(\d{8})", ev, re.M)
        if de:
            e = datetime.date.fromisoformat(iso(de.group(1))) - datetime.timedelta(days=1)
            if e.isoformat() > start:
                end = e.isoformat()
    games.append({"opp": away, "comp": liga, "start": start, "end": end, "time": time, "confirmed": time is not None})

games.sort(key=lambda g: g["start"])
out = {"quelle": "calovo-Kalender", "stand": datetime.date.today().isoformat(), "spiele": games}
new = json.dumps(out, ensure_ascii=False, indent=1)
try:
    old = open(OUT, encoding="utf-8").read()
except FileNotFoundError:
    old = ""
if new != old:
    open(OUT, "w", encoding="utf-8").write(new)
    print("spielplan.json aktualisiert:", len(games), "Heimspiele")
else:
    print("keine Änderung")
