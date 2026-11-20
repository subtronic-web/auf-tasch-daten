#!/usr/bin/env python3
"""Auf Tasch – Push-Bot (läuft alle 5 Minuten auf GitHub).

Ablauf:
 1. Liest aus Firebase, welche Handys Push wollen (Sammlung push/).
 2. Öffnet die echte App (hansa-tasch.web.app) unsichtbar im Bot-Modus und rechnet
    für jedes Handy mit genau derselben Logik wie „Neu für dich“ aus, was neu ist.
 3. Schickt die Mitteilungen raus und merkt sich in pushstate/, was schon verschickt ist.

Geheimnis: FIREBASE_SA = Inhalt der Dienstkonto-Schlüsseldatei (JSON) aus der Firebase-Konsole.
Der Push-Schlüssel (VAPID) wird beim ersten Lauf selbst erzeugt und in Firebase abgelegt.
"""
import base64, json, os, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo

APP_URL = os.environ.get("APP_URL", "https://hansa-tasch.web.app/")
CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")
CONTACT = "https://hansa-tasch.web.app"
MAX_EINZELN = 3          # bis zu 3 einzelne Mitteilungen, sonst eine Sammel-Mitteilung
BERLIN = ZoneInfo("Europe/Berlin")


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Datenquelle
class FirestoreQuelle:
    def __init__(self):
        import firebase_admin
        from firebase_admin import credentials, firestore
        sa = os.environ.get("FIREBASE_SA", "").strip()
        if not sa:
            sys.exit("FIREBASE_SA fehlt (GitHub › Settings › Secrets › Actions).")
        firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
        self.db = firestore.client()

    def get(self, col, doc):
        sn = self.db.collection(col).document(doc).get()
        return sn.to_dict() if sn.exists else None

    def all(self, col):
        return {d.id: d.to_dict() for d in self.db.collection(col).stream()}

    def set(self, col, doc, data, merge=False):
        self.db.collection(col).document(doc).set(data, merge=merge)

    def delete(self, col, doc):
        self.db.collection(col).document(doc).delete()


class DateiQuelle:
    """Für Tests: alles liegt in einer JSON-Datei {sammlung: {doc: daten}}."""
    def __init__(self, pfad):
        self.pfad = pfad
        self.d = json.load(open(pfad, encoding="utf-8"))

    def get(self, col, doc):
        return self.d.get(col, {}).get(doc)

    def all(self, col):
        return dict(self.d.get(col, {}))

    def set(self, col, doc, data, merge=False):
        c = self.d.setdefault(col, {})
        c[doc] = {**c.get(doc, {}), **data} if merge else data
        self._save()

    def delete(self, col, doc):
        self.d.get(col, {}).pop(doc, None)
        self._save()

    def _save(self):
        json.dump(self.d, open(self.pfad, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- Schlüssel
def vapid(q):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    st = q.get("pushstate", "_vapid")
    if st and st.get("priv") and st.get("pub"):
        pub = st["pub"]
        if (q.get("config", "push") or {}).get("pub") != pub:
            q.set("config", "push", {"pub": pub})
        return st["priv"], pub
    k = ec.generate_private_key(ec.SECP256R1())
    priv = base64.urlsafe_b64encode(k.private_numbers().private_value.to_bytes(32, "big")).decode().rstrip("=")
    raw = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pub = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    q.set("pushstate", "_vapid", {"priv": priv, "pub": pub, "ts": int(time.time() * 1000)})
    q.set("config", "push", {"pub": pub})
    log("Neuer Push-Schlüssel erzeugt.")
    return priv, pub


# ---------------------------------------------------------------- Senden
def sende(sub, payload, priv):
    from pywebpush import webpush, WebPushException
    try:
        webpush(subscription_info=sub, data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=priv, vapid_claims={"sub": CONTACT}, ttl=6 * 3600,
                headers={"Urgency": "normal"}, timeout=15)
        return "ok"
    except WebPushException as e:
        code = getattr(e.response, "status_code", None)
        if code in (404, 410):
            return "weg"
        log("  Fehler beim Senden:", code, str(e)[:200])
        return "fehler"


def mitteilungen(rows, badge):
    def eine(r, tag):
        p = {"title": r["t"], "body": r["s"], "tag": tag, "badge": badge,
             "keys": r["keys"], "chat": r["chat"]}
        if r["url"]:
            p["url"] = r["url"]
        else:
            p.update(act=r["act"], a=r["a"], b=r["b"])
        return p
    if len(rows) <= MAX_EINZELN:
        return [eine(r, (r["chat"] and "chat-" + r["chat"]) or (r["keys"][0] if r["keys"] else r["t"])[:60])
                for r in rows]
    n = sum(r["n"] for r in rows)
    return [{"title": f"Auf Tasch: {n} Neuigkeiten", "tag": "sammel", "badge": badge,
             "body": " · ".join(r["t"] for r in rows[:4]) + (" …" if len(rows) > 4 else ""),
             "keys": [k for r in rows for k in r["keys"]], "act": "nav", "a": "start", "b": None}]


def nachtruhe(nset, jetzt):
    an = nset.get("nacht", True) if isinstance(nset, dict) else True
    return bool(an) and (jetzt.hour >= 23 or jetzt.hour < 7)


# ---------------------------------------------------------------- Hauptlauf
def lauf(q, sender=sende, jetzt=None):
    jetzt = jetzt or datetime.now(BERLIN)
    priv, pub = vapid(q)
    subs = q.all("push")
    if not subs:
        log("Noch kein Handy für Push angemeldet.")
        return {}
    users = q.all("users")
    daten = {"app": q.get("tasch", "app") or {}, "karten": q.all("karten"), "zusatz": q.all("zusatz")}
    from playwright.sync_api import sync_playwright
    bericht = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if os.path.exists(CHROME) else p.chromium.launch()
        pg = b.new_page()
        fehler = []
        pg.on("pageerror", lambda e: fehler.append(str(e)))
        # Keine echte Anmeldung bei Firebase, kein Service Worker
        pg.route("**/*", lambda r: r.abort() if ("firebasejs" in r.request.url or r.request.url.endswith("/sw.js")) else r.continue_())
        pg.add_init_script("window.__PUSHBOT=" + json.dumps({"data": daten}, ensure_ascii=False) + ";")
        pg.goto(APP_URL + ("&" if "?" in APP_URL else "?") + "pushbot=1", wait_until="domcontentloaded", timeout=60000)
        try:
            pg.wait_for_function("window.PUSHBOT_READY===true", timeout=60000)
        except Exception:
            ok = pg.evaluate("typeof window.pushBotRows")
            if ok != "function":
                log("Die Live-App hat den Push-Bot-Modus noch nicht (erst V2.4 hochladen). Ende.")
                b.close()
                return {}
            raise
        for sid, s in subs.items():
            u = users.get(s.get("uid", ""))
            if not u:
                log(f"{sid}: kein Zugang mehr → Abo gelöscht")
                q.delete("push", sid); q.delete("pushstate", sid)
                bericht[sid] = "gelöscht"
                continue
            st = q.get("pushstate", sid) or {}
            nseen = set(st.get("nseen") or []) | set(s.get("seenApp") or []) if st.get("nseen") is not None else None
            seen = dict(st.get("seen") or {})
            for g, ts in (s.get("chatApp") or {}).items():
                seen[g] = max(seen.get(g, 0) or 0, ts or 0)
            res = pg.evaluate("u=>window.pushBotRows(u)", {
                "uid": s["uid"], "name": u.get("name", ""), "role": u.get("role", ""), "owner": u.get("owner"),
                "nset": s.get("nset") or {}, "nseen": sorted(nseen) if nseen is not None else None, "seen": seen})
            name = u.get("name", "?")
            if res["init"]:
                q.set("pushstate", sid, {"nseen": res["nseen"], "seen": res["seen"], "last": int(time.time() * 1000)})
                log(f"{name}: neu angemeldet – aktueller Stand gemerkt, nix verschickt")
                bericht[sid] = "start"
                continue
            rows = res["rows"]
            if not rows:
                bericht[sid] = 0
                continue
            if nachtruhe(s.get("nset") or {}, jetzt):
                log(f"{name}: {len(rows)} neu, aber Nachtruhe – kommt ab 7 Uhr")
                bericht[sid] = "nacht"
                continue
            ergebnis = "ok"
            for m in mitteilungen(rows, res["badge"]):
                ergebnis = sender(s["sub"], m, priv)
                if ergebnis != "ok":
                    break
            if ergebnis == "weg":
                log(f"{name}: Push-Adresse gibt’s nicht mehr → Abo gelöscht")
                q.delete("push", sid); q.delete("pushstate", sid)
                bericht[sid] = "gelöscht"
                continue
            if ergebnis == "ok":
                ns = set(res["nseen"]) | {k for r in rows for k in r["keys"]}
                se = dict(res["seen"])
                for r in rows:
                    if r["chat"]:
                        se[r["chat"]] = max(se.get(r["chat"], 0) or 0, r["chatTs"])
                q.set("pushstate", sid, {"nseen": sorted(ns), "seen": se, "last": int(time.time() * 1000)})
                log(f"{name}: {len(rows)} neu → verschickt: " + " | ".join(r["t"] for r in rows))
                bericht[sid] = len(rows)
            else:
                bericht[sid] = "fehler"
        if fehler:
            log("JS-Fehler in der App:", fehler[:3])
        b.close()
    return bericht


def testpush(q, name, sender=sende):
    """Einmalige Test-Mitteilung an alle Handys der Person `name` (ignoriert Nachtruhe, ändert keinen Stand)."""
    priv, pub = vapid(q)
    users = q.all("users")
    n = 0
    for sid, s in q.all("push").items():
        u = users.get(s.get("uid", ""), {})
        if name.lower() not in (u.get("name", "") or "").lower():
            continue
        m = {"title": "Auf Tasch: Test", "body": "Wenn du das liest, klappt Push. Die Zahl am App-Symbol sollte 1 zeigen.",
             "tag": "test", "badge": 1, "keys": [], "chat": None, "act": "nav", "a": "start", "b": None}
        e = sender(s["sub"], m, priv)
        log(f"Test an {u.get('name', '?')} ({sid}): {e}")
        n += 1
    if not n:
        log(f"Kein angemeldetes Handy für „{name}“ gefunden.")


if __name__ == "__main__":
    quelle = DateiQuelle(sys.argv[1]) if len(sys.argv) > 1 else FirestoreQuelle()
    if os.environ.get("TESTPUSH", "").strip():
        testpush(quelle, os.environ["TESTPUSH"].strip())
    else:
        lauf(quelle)
