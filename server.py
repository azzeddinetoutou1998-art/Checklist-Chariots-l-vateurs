#!/usr/bin/env python3
"""
Checklist de prise de poste - Caristes
Serveur autonome : Python 3.9+ uniquement, aucune dépendance, base SQLite locale.

  Formulaire caristes (sans compte) : http://<serveur>:8080/
  Tableau de bord (mot de passe)    : http://<serveur>:8080/tableau
  Affiches QR-code (mot de passe)   : http://<serveur>:8080/qr

Variables d'environnement :
  ADMIN_PASSWORD   mot de passe du tableau de bord (obligatoire en production)
  PORT             port d'écoute (défaut 8080)
  DATA_DIR         dossier de la base de données (défaut ./data)
"""
import base64, csv, hmac, io, json, os, re, secrets, sqlite3, threading, time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
# Pages dans le dossier static/, ou à la racine si les fichiers ont été déposés à plat (dépôt GitHub par glisser-déposer)
STATIC = os.path.join(ROOT, "static") if os.path.isdir(os.path.join(ROOT, "static")) else ROOT
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
DB_PATH = os.path.join(DATA_DIR, "checklists.db")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
PORT = int(os.environ.get("PORT", "8080"))
PASSWORD = os.environ.get("ADMIN_PASSWORD") or ""
GENERATED_PASSWORD = False
if not PASSWORD:
    PASSWORD = secrets.token_urlsafe(9)
    GENERATED_PASSWORD = True

CHOIX = ("C", "NC", "NA")          # Conforme / Non conforme / Non applicable
STATUTS = ("a_traiter", "pris_en_charge", "resolu")
MAX_BODY = 32 * 1024
MAX_BODY_PHOTOS = 10 * 1024 * 1024   # checklist avec photos
MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 2 * 1024 * 1024

def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

# --------------------------------------------------------------------------- base
_lock = threading.Lock()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS checklists(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            horodateur TEXT NOT NULL,
            date TEXT NOT NULL,
            chariot TEXT NOT NULL,
            cariste TEXT NOT NULL,
            horametre REAL,
            site TEXT, fournisseur TEXT, activite TEXT,
            controles TEXT NOT NULL,
            nb_non_conformes INTEGER NOT NULL,
            commentaire TEXT,
            securite TEXT NOT NULL,
            statut TEXT NOT NULL,
            note_responsable TEXT,
            maj TEXT NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_h ON checklists(horodateur)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_m ON checklists(maj)")
        cols = [r[1] for r in c.execute("PRAGMA table_info(checklists)")]
        if "photos" not in cols:
            c.execute("ALTER TABLE checklists ADD COLUMN photos TEXT NOT NULL DEFAULT '[]'")
    os.makedirs(PHOTOS_DIR, exist_ok=True)

def clean(v, n):
    return (str(v).strip()[:n]) if v is not None else ""

def validate(p, cfg):
    err = []
    chariot = clean(p.get("chariot"), 20)
    if not chariot.isdigit(): err.append("Numéro du chariot : chiffres uniquement, numéro complet.")
    cariste = clean(p.get("cariste"), 80)
    if len(cariste) < 3: err.append("Nom et prénom du cariste obligatoires.")
    date = clean(p.get("date"), 10)
    try: datetime.strptime(date, "%Y-%m-%d")
    except ValueError: err.append("Date invalide.")
    try:
        horametre = float(str(p.get("horametre", "")).replace(",", "."))
        if horametre < 0 or horametre > 1e6: raise ValueError
    except ValueError:
        horametre = None; err.append("Horamètre : nombre attendu.")
    site, fournisseur, activite = clean(p.get("site"), 80), clean(p.get("fournisseur"), 80), clean(p.get("activite"), 80)
    for nom, val in (("Site", site), ("Fournisseur", fournisseur), ("Activité", activite)):
        if not val: err.append(f"{nom} obligatoire.")
    ctrl_in = p.get("controles") or {}
    controles = {}
    for point in cfg["points"]:
        v = ctrl_in.get(point)
        if v not in CHOIX: err.append(f"Point non renseigné : {point}")
        else: controles[point] = v
    nb_nc = sum(1 for v in controles.values() if v == "NC")
    commentaire = clean(p.get("commentaire"), 2000)
    securite = p.get("securite")
    if securite not in ("OUI", "NON"): err.append("Indiquez si le chariot peut rouler en sécurité.")
    if (nb_nc or securite == "NON") and len(commentaire) < 3:
        err.append("Décrivez la ou les anomalies dans le commentaire.")
    row = dict(date=date, chariot=chariot, cariste=cariste, horametre=horametre, site=site,
               fournisseur=fournisseur, activite=activite, controles=json.dumps(controles, ensure_ascii=False),
               nb_non_conformes=nb_nc, commentaire=commentaire, securite=securite or "",
               statut="a_traiter" if (nb_nc or securite == "NON" or commentaire) else "resolu")
    photos = []
    raw = p.get("photos") or []
    if not isinstance(raw, list) or len(raw) > MAX_PHOTOS:
        err.append(f"{MAX_PHOTOS} photos maximum."); raw = []
    for item in raw:
        try:
            data = base64.b64decode(str(item).split(",", 1)[-1], validate=True)
        except Exception:
            err.append("Photo illisible, reprenez-la."); continue
        if not data.startswith(b"\xff\xd8\xff") or len(data) > MAX_PHOTO_BYTES:
            err.append("Photo refusée (format ou taille)."); continue
        photos.append(data)
    return row, err, photos

def row_out(r):
    d = dict(r); d["controles"] = json.loads(d["controles"])
    d["photos"] = ["/photos/" + f for f in json.loads(d.get("photos") or "[]")]
    return d

# --------------------------------------------------------------------------- anti-abus
_hits = {}
def rate_ok(ip, limit=15, window=60):
    t = time.time()
    with _lock:
        lst = [x for x in _hits.get(ip, []) if t - x < window]
        ok = len(lst) < limit
        if ok: lst.append(t)
        _hits[ip] = lst
    return ok

# --------------------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "ChecklistCaristes/1.0"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)): body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name):
        with open(os.path.join(STATIC, name), "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def _ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def _authed(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                if hmac.compare_digest(pw.encode(), PASSWORD.encode()): return True
            except Exception: pass
        self._send(401, "Mot de passe requis", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="Tableau de bord caristes", charset="UTF-8"'})
        return False

    def _json_body(self, limit=MAX_BODY):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit: return None
        try: return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception: return None

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); path = u.path.rstrip("/") or "/"
        try:
            if path == "/": return self._file("formulaire.html")
            if path == "/config":
                cfg = load_config()
                return self._send(200, {k: cfg.get(k) for k in ("nom_entreprise", "sites", "activites", "fournisseurs", "points")})
            if path == "/sante": return self._send(200, {"ok": True})
            if path.startswith("/photos/"):
                if not self._authed(): return
                name = path[len("/photos/"):]
                if not re.fullmatch(r"\d+_\d\.jpg", name): return self._send(404, "Introuvable", "text/plain")
                fp = os.path.join(PHOTOS_DIR, name)
                if not os.path.isfile(fp): return self._send(404, "Introuvable", "text/plain")
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg", {"Cache-Control": "private, max-age=86400"})
            if path.startswith("/horametre/"):
                # dernier relevé connu d'un chariot (contrôle de saisie dans le formulaire)
                ch = path.rsplit("/", 1)[-1]
                if not ch.isdigit(): return self._send(404, {"erreur": "Introuvable"})
                with db() as c:
                    r = c.execute("SELECT date, horametre FROM checklists WHERE chariot=? AND horametre IS NOT NULL "
                                  "ORDER BY date DESC, horodateur DESC LIMIT 1", (ch,)).fetchone()
                return self._send(200, {"date": r["date"], "horametre": r["horametre"]} if r else {})
            if path in ("/tableau", "/qr", "/horametres", "/api/checklists", "/api/releves", "/export.csv") and not self._authed(): return
            if path == "/horametres": return self._file("horametres.html")
            if path == "/api/releves":
                days = max(1, min(3660, int((q.get("jours") or ["90"])[0] or 90)))
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
                with db() as c:
                    rows = c.execute("SELECT id, horodateur, date, chariot, cariste, horametre, site, fournisseur, activite "
                                     "FROM checklists WHERE date >= ? AND horametre IS NOT NULL ORDER BY chariot, date, horodateur",
                                     (cutoff,)).fetchall()
                return self._send(200, {"releves": [dict(r) for r in rows]})
            if path == "/tableau": return self._file("tableau.html")
            if path == "/qr": return self._file("qr.html")
            if path == "/api/checklists": return self._list(q)
            if path == "/export.csv": return self._csv(q)
            self._send(404, "Page introuvable", "text/plain; charset=utf-8")
        except Exception as e:
            self._send(500, {"erreur": "Erreur serveur"}); print("ERREUR", e)

    def _list(self, q):
        days = max(1, min(366, int((q.get("jours") or ["7"])[0] or 7)))
        since = (q.get("depuis") or [""])[0]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        # jeton de reprise avec 5 s de recouvrement (le client fusionne par id)
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with db() as c:
            if since:
                rows = c.execute("SELECT * FROM checklists WHERE maj > ? ORDER BY id", (since,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM checklists WHERE horodateur >= ? ORDER BY id", (cutoff,)).fetchall()
        self._send(200, {"maintenant": stamp, "lignes": [row_out(r) for r in rows], "points": load_config()["points"]})

    def _csv(self, q):
        days = max(1, min(3660, int((q.get("jours") or ["30"])[0] or 30)))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        points = load_config()["points"]
        lib = {"C": "Conforme", "NC": "Non conforme", "NA": "Non applicable"}
        out = io.StringIO(); w = csv.writer(out, delimiter=";")
        w.writerow(["Horodateur (UTC)", "Date", "Numéro du chariot", "Nom et prénom du cariste", "Horamètre",
                    "Site", "Fournisseur", "Activité"] + points +
                   ["Commentaire", "Mon chariot peut rouler en toute sécurité ?", "Suivi", "Note responsable", "Photos"])
        with db() as c:
            for r in c.execute("SELECT * FROM checklists WHERE horodateur >= ? ORDER BY id", (cutoff,)):
                ctrl = json.loads(r["controles"])
                w.writerow([r["horodateur"][:19].replace("T", " "), r["date"], r["chariot"], r["cariste"],
                            str(r["horametre"]).replace(".", ","), r["site"], r["fournisseur"], r["activite"]] +
                           [lib.get(ctrl.get(p), "") for p in points] +
                           [r["commentaire"], r["securite"], r["statut"], r["note_responsable"] or "",
                            len(json.loads(r["photos"] or "[]"))])
        body = "﻿" + out.getvalue()   # BOM pour qu'Excel lise les accents
        self._send(200, body, "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="checklists-{datetime.now():%Y-%m-%d}.csv"'})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path == "/api/checklist":
                if not rate_ok(self._ip()): return self._send(429, {"erreur": "Trop d'envois, patientez une minute."})
                p = self._json_body(MAX_BODY_PHOTOS)
                if not isinstance(p, dict): return self._send(400, {"erreur": "Données illisibles ou photos trop lourdes."})
                row, err, photos = validate(p, load_config())
                if err: return self._send(422, {"erreur": err[0], "erreurs": err})
                t = now_iso(); row.update(horodateur=t, maj=t)
                cols = ",".join(row); qs = ",".join("?" * len(row))
                with _lock, db() as c:
                    cur = c.execute(f"INSERT INTO checklists({cols}) VALUES({qs})", list(row.values()))
                    rid = cur.lastrowid
                    names = []
                    for i, data in enumerate(photos):
                        name = f"{rid}_{i}.jpg"
                        with open(os.path.join(PHOTOS_DIR, name), "wb") as f: f.write(data)
                        names.append(name)
                    if names:
                        c.execute("UPDATE checklists SET photos=?, statut=CASE WHEN statut='resolu' THEN 'a_traiter' ELSE statut END WHERE id=?",
                                  (json.dumps(names), rid))
                return self._send(201, {"ok": True, "id": rid})
            if path.startswith("/api/checklists/") and path.endswith("/suivi"):
                if not self._authed(): return
                try: rid = int(path.split("/")[3])
                except ValueError: return self._send(404, {"erreur": "Introuvable"})
                p = self._json_body() or {}
                statut = p.get("statut")
                if statut not in STATUTS: return self._send(400, {"erreur": "Statut invalide."})
                note = clean(p.get("note"), 500)
                with _lock, db() as c:
                    n = c.execute("UPDATE checklists SET statut=?, note_responsable=?, maj=? WHERE id=?",
                                  (statut, note, now_iso(), rid)).rowcount
                return self._send(200 if n else 404, {"ok": bool(n)})
            self._send(404, {"erreur": "Introuvable"})
        except Exception as e:
            self._send(500, {"erreur": "Erreur serveur"}); print("ERREUR", e)


if __name__ == "__main__":
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"Checklist caristes démarrée sur le port {PORT}")
    print(f"  Formulaire caristes : http://localhost:{PORT}/")
    print(f"  Tableau de bord     : http://localhost:{PORT}/tableau")
    if GENERATED_PASSWORD:
        print(f"  ATTENTION : aucun ADMIN_PASSWORD défini. Mot de passe temporaire : {PASSWORD}")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
