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
import base64, csv, hmac, io, json, os, re, secrets, shutil, smtplib, sqlite3, ssl, threading, time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
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
PASSWORD = (os.environ.get("ADMIN_PASSWORD") or "").strip()   # espaces ou retour à la ligne collés par erreur
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

# --------------------------------------------------------------------------- alertes e-mail
# Réglages dans Render (onglet Environment) :
#   ALERTE_EMAILS   destinataires, séparés par des virgules
#   SMTP_HOST, SMTP_PORT (587 par défaut), SMTP_USER, SMTP_PASSWORD, SMTP_FROM (adresse d'expédition)
SMTP = {k: (os.environ.get(k) or "").strip() for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")}
ALERTE_EMAILS = [a.strip() for a in (os.environ.get("ALERTE_EMAILS") or "").replace(";", ",").split(",") if a.strip()]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

def alertes_actives():
    return bool(ALERTE_EMAILS and SMTP["SMTP_HOST"] and (SMTP["SMTP_FROM"] or SMTP["SMTP_USER"]))

def heure_paris(iso):
    """Heure de Paris sans dépendance : UTC+2 de fin mars à fin octobre, sinon UTC+1."""
    t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    def dernier_dimanche(m):
        d = datetime(t.year, m, 31 if m in (3, 10) else 30, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    off = 2 if dernier_dimanche(3) <= t < dernier_dimanche(10) else 1
    return (t + timedelta(hours=off)).strftime("%d/%m/%Y à %H:%M")

def points_critiques_nok(controles, cfg):
    crit = (cfg.get("alertes") or {}).get("points_critiques") or []
    return [p for p in crit if controles.get(p) == "NC"]

def construire_alerte(row, rid, photos, cfg):
    controles = json.loads(row["controles"]) if isinstance(row["controles"], str) else row["controles"]
    crit = points_critiques_nok(controles, cfg)
    autres = [p for p, v in controles.items() if v == "NC" and p not in crit]
    motif = ", ".join(crit) if crit else "chariot déclaré NON roulant"
    noms = [re.sub(r"^(Fonctionnement (du|de la|de l'|des) |Essais de )", "", p.split("(")[0]).strip().capitalize() for p in crit]
    court = (", ".join(noms[:3]) + (f" +{len(noms) - 3}" if len(noms) > 3 else "") + " NOK") if crit else "NE PEUT PAS ROULER"
    sujet = f"[ALERTE SÉCURITÉ] Chariot {row['chariot']} – {court} – {row['site']}"
    quand = heure_paris(row["horodateur"])
    infos = [("Chariot", row["chariot"]), ("Site", row["site"]), ("Activité", row["activite"]),
             ("Fournisseur", row["fournisseur"]), ("Cariste", row["cariste"]), ("Date et heure", quand),
             ("Horamètre", f"{row['horametre']:g} h".replace(".", ",") if row["horametre"] is not None else ""),
             ("Peut rouler en sécurité", row["securite"])]
    lien = f"{PUBLIC_URL}/tableau" if PUBLIC_URL else ""
    texte = [f"Alerte sécurité : {motif}", ""] + [f"{k} : {v}" for k, v in infos]
    texte += ["", "Points de sécurité NON CONFORMES :"] + [f"  - {p}" for p in crit] if crit else []
    if autres: texte += ["", "Autres points non conformes :"] + [f"  - {p}" for p in autres]
    texte += ["", "Commentaire du cariste :", row["commentaire"] or "(aucun)"]
    if photos: texte += ["", f"{len(photos)} photo(s) en pièce jointe."]
    if lien: texte += ["", f"Tableau de bord : {lien}"]
    li = lambda xs, col: "".join(f"<li style='margin:2px 0;color:{col}'><b>{escape(x)}</b></li>" for x in xs)
    html = (f"<div style='font-family:Arial,sans-serif;font-size:15px;color:#171b16;max-width:620px'>"
            f"<div style='background:#b42318;color:#fff;padding:14px 18px;font-size:18px;font-weight:bold'>"
            f"ALERTE SÉCURITÉ – Chariot {escape(row['chariot'])}</div>"
            f"<div style='border:1px solid #d9ddd5;border-top:0;padding:16px 18px'>"
            + (f"<p style='margin:0 0 6px'><b>Points de sécurité non conformes :</b></p><ul style='margin:0 0 12px'>{li(crit, '#b42318')}</ul>" if crit else "")
            + ("<p style='margin:0 0 12px;color:#b42318'><b>Le cariste a déclaré que le chariot NE PEUT PAS rouler en sécurité.</b></p>" if row["securite"] == "NON" else "")
            + (f"<p style='margin:0 0 6px'>Autres points non conformes :</p><ul style='margin:0 0 12px'>{li(autres, '#171b16')}</ul>" if autres else "")
            + "<table style='border-collapse:collapse;margin:4px 0 12px'>"
            + "".join(f"<tr><td style='padding:3px 14px 3px 0;color:#5b6358'>{escape(k)}</td><td style='padding:3px 0'><b>{escape(str(v))}</b></td></tr>" for k, v in infos)
            + "</table>"
            + f"<p style='margin:0 0 4px;color:#5b6358'>Commentaire du cariste :</p><p style='margin:0 0 12px;padding:8px 12px;background:#f3f4f1;white-space:pre-wrap'>{escape(row['commentaire'] or '(aucun)')}</p>"
            + (f"<p style='margin:0 0 12px'>{len(photos)} photo(s) en pièce jointe.</p>" if photos else "")
            + (f"<p style='margin:0'><a href='{escape(lien)}' style='background:#171b16;color:#fff;padding:10px 16px;text-decoration:none;border-radius:6px;display:inline-block'>Ouvrir le tableau de bord</a></p>" if lien else "")
            + "</div><p style='font-size:12px;color:#8a9187'>Message automatique de la checklist de prise de poste caristes.</p></div>")
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = formataddr(("Checklist caristes", SMTP["SMTP_FROM"] or SMTP["SMTP_USER"]))
    msg["To"] = ", ".join(ALERTE_EMAILS)
    msg["Message-ID"] = make_msgid(domain="checklist-caristes")
    msg["X-Priority"] = "1"; msg["Importance"] = "high"
    msg.set_content("\n".join(texte))
    msg.add_alternative(html, subtype="html")
    for i, data in enumerate(photos):
        msg.add_attachment(data, maintype="image", subtype="jpeg", filename=f"chariot-{row['chariot']}-photo-{i + 1}.jpg")
    return msg

def _envoyer_port(msg, host, port):
    """Envoie par un port donné. Lève une erreur qui indique l'étape en cause."""
    ctx = ssl.create_default_context()
    etape = f"connexion à {host}:{port}"
    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
        else:
            srv = smtplib.SMTP(host, port, timeout=30)
            etape = "chiffrement (STARTTLS)"
            srv.ehlo()
            if srv.has_extn("starttls"): srv.starttls(context=ctx); srv.ehlo()
        try:
            etape = "identification (SMTP_USER / SMTP_PASSWORD)"
            if SMTP["SMTP_USER"]: srv.login(SMTP["SMTP_USER"], SMTP["SMTP_PASSWORD"].replace(" ", ""))
            etape = "envoi du message"
            srv.send_message(msg)
        finally:
            try: srv.quit()
            except Exception: pass
    except Exception as e:
        raise RuntimeError(f"{etape} : {type(e).__name__}: {e}") from e

def envoyer(msg):
    host = SMTP["SMTP_HOST"]
    try: port = int(re.sub(r"\D", "", SMTP["SMTP_PORT"]) or 587)
    except ValueError: port = 587
    try:
        _envoyer_port(msg, host, port)
    except RuntimeError as e:
        # Mauvais port ou port bloqué : on essaie automatiquement l'autre port standard
        autre = 465 if port != 465 else 587
        if "identification" in str(e) or "envoi du message" in str(e): raise
        try: _envoyer_port(msg, host, autre)
        except RuntimeError as e2: raise RuntimeError(f"Port {port} → {e} | Port {autre} → {e2}") from e2

def noter_alerte(rid, etat):
    with _lock, db() as c:
        c.execute("UPDATE checklists SET alerte=?, maj=? WHERE id=?", (etat, now_iso(), rid))

def alerte_en_fond(row, rid, photos, cfg):
    def run():
        for essai in range(3):
            try:
                envoyer(construire_alerte(row, rid, photos, cfg))
                noter_alerte(rid, "envoyee " + now_iso()); return
            except Exception as e:
                err = str(e)[:300]
                print(f"ALERTE e-mail échec (essai {essai + 1}) checklist {rid} : {err}")
                time.sleep(10 * (essai + 1))
        noter_alerte(rid, "echec " + err)
    threading.Thread(target=run, daemon=True).start()

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
        if "alerte" not in cols:
            c.execute("ALTER TABLE checklists ADD COLUMN alerte TEXT")
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
                raw = base64.b64decode(h[6:])
                for enc in ("utf-8", "latin-1"):          # certains navigateurs envoient les accents en latin-1
                    try: _, _, pw = raw.decode(enc).partition(":")
                    except UnicodeDecodeError: continue
                    if hmac.compare_digest(pw.strip().encode(), PASSWORD.encode()): return True
            except Exception: pass
        page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Connexion</title><body style='font:16px system-ui;margin:0;padding:32px 16px;background:#eef0ec;color:#171b16'>"
                "<div style='max-width:460px;margin:auto;background:#fff;border-top:6px solid #f2bd1d;border-radius:10px;padding:24px'>"
                "<h1 style='margin:0 0 8px;font-size:1.3rem'>Mot de passe requis</h1>"
                "<p>Cette page est réservée aux responsables. Rechargez la page : une fenêtre de connexion s'ouvre.</p>"
                "<p><b>Identifiant</b> : ce que vous voulez (ex. admin)<br><b>Mot de passe</b> : celui saisi dans Render (ADMIN_PASSWORD).</p>"
                "<p style='color:#5b6358;font-size:.9rem'>Majuscules et minuscules comptent. Si la fenêtre ne s'ouvre pas, essayez dans Chrome ou Edge sur ordinateur.</p>"
                "<p><a href='' style='display:inline-block;background:#171b16;color:#fff;padding:10px 16px;border-radius:7px;text-decoration:none'>Réessayer</a></p></div>")
        self._send(401, page, "text/html; charset=utf-8",
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
            if path in ("/tableau", "/qr", "/horametres", "/api/checklists", "/api/releves", "/api/stockage", "/export.csv") and not self._authed(): return
            if path == "/horametres": return self._file("horametres.html")
            if path == "/api/stockage":
                du = shutil.disk_usage(DATA_DIR)
                def size(p):
                    try: return os.path.getsize(p)
                    except OSError: return 0
                base = sum(size(DB_PATH + x) for x in ("", "-wal", "-shm"))
                photos = [f for f in os.listdir(PHOTOS_DIR)] if os.path.isdir(PHOTOS_DIR) else []
                return self._send(200, {"total": du.total, "utilise": du.used, "libre": du.free,
                                        "base": base, "photos": sum(size(os.path.join(PHOTOS_DIR, f)) for f in photos),
                                        "nb_photos": len(photos)})
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
                cfg = load_config()
                regle = cfg.get("alertes") or {}
                declenche = points_critiques_nok(json.loads(row["controles"]), cfg) or \
                            (regle.get("si_chariot_non_roulant", True) and row["securite"] == "NON")
                if declenche:
                    if alertes_actives():
                        noter_alerte(rid, "en_cours")
                        alerte_en_fond(dict(row, horodateur=t), rid, photos, cfg)
                    else:
                        noter_alerte(rid, "non_configuree")
                return self._send(201, {"ok": True, "id": rid})
            if path == "/api/alerte-test":
                if not self._authed(): return
                if not alertes_actives():
                    manque = [k for k in ("ALERTE_EMAILS", "SMTP_HOST", "SMTP_FROM") if not (ALERTE_EMAILS if k == "ALERTE_EMAILS" else SMTP[k] or (k == "SMTP_FROM" and SMTP["SMTP_USER"]))]
                    return self._send(400, {"erreur": "Alertes non configurées dans Render. Manque : " + ", ".join(manque)})
                cfg = load_config()
                crit = (cfg.get("alertes") or {}).get("points_critiques") or []
                row = {"chariot": "TEST", "site": "Test", "activite": "Test", "fournisseur": "Test", "cariste": "Message de test",
                       "horodateur": now_iso(), "horametre": None, "securite": "NON", "commentaire": "Ceci est un e-mail de test : aucune action requise.",
                       "controles": json.dumps({p: "NC" for p in crit[:1]})}
                try:
                    envoyer(construire_alerte(row, 0, [], cfg))
                    return self._send(200, {"ok": True, "message": "E-mail de test envoyé à " + ", ".join(ALERTE_EMAILS)})
                except Exception as e:
                    return self._send(502, {"erreur": f"Échec de l'envoi via {SMTP['SMTP_HOST']} (expéditeur {SMTP['SMTP_USER'] or SMTP['SMTP_FROM']}) : {e}"[:500]})
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
    print(f"  Alertes e-mail      : {'actives vers ' + ', '.join(ALERTE_EMAILS) if alertes_actives() else 'non configurées'}")
    if GENERATED_PASSWORD:
        print(f"  ATTENTION : aucun ADMIN_PASSWORD défini. Mot de passe temporaire : {PASSWORD}")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
