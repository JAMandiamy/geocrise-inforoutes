#!/usr/bin/env python3
"""
convert_autoroutes.py
---------------------
Lit le « Récapitulatif trafic France entière » de Bison Futé (page HTML publique,
qui regroupe les événements des DIR ET des sociétés d'autoroutes : APRR, SANEF...)
et produit deux couches GeoJSON pour les départements choisis (Bourgogne par défaut) :

  geocrise_inforoutes_autoroutes_ponctuels.geojson   (Point)
  geocrise_inforoutes_autoroutes_lineaires.geojson   (LineString, « de X à Y »)

La page ne donne pas de coordonnées : chaque événement est placé sur la commune
citée (« à Beaune ») à l'aide de communes_bourgogne.csv, ou de l'API
geo.api.gouv.fr pour une commune hors de la table.  Précision : la commune.

Usage :
  python convert_autoroutes.py                    # télécharge la page
  python convert_autoroutes.py --fichier recap.html
  python convert_autoroutes.py --deps 21,58,71,89 --sortie ./
"""
import argparse, csv, hashlib, html, json, os, re, sys, unicodedata
import urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:
    PARIS = timezone.utc

URL = ("https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/"
       "Evenementiel-DIR/cnir/RecapTraficFranceEntiere.html")
ICI = os.path.dirname(os.path.abspath(__file__))
TABLE_COMMUNES = os.path.join(ICI, "communes_bourgogne.csv")
CACHE_GEO = os.path.join(ICI, "cache_communes_api.json")
MOIS = {m: i + 1 for i, m in enumerate(
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split())}

# --------------------------------------------------------------------------- groupes
# Mêmes 5 groupes que la couche Bison Futé RRN (ordre = priorité crise).
GROUPES = [
    ("1 - Accidents et obstacles", ["accident", "obstacle", "vehicule", "pieton", "chaussee degradee",
                                    "bouchon", "ralenti", "circulation dense", "objet", "incendie",
                                    "contresens", "contre-sens", "collision", "perte de chargement"]),
    ("2 - Météo et environnement", ["neige", "verglas", "glissant", "pluie", "brouillard", "vent",
                                    "inondation", "arbre", "eboulement", "animal", "animaux", "gibier",
                                    "grele", "orage", "meteo", "visibilite", "boue", "eau sur"]),
    ("3 - Fermetures et déviations", ["fermee", "ferme", "fermeture", "reduction du nombre de voies",
                                      "deviation", "basculement", "limitation", "vitesse", "interdi",
                                      "coupee", "neutralis", "restriction"]),
    ("4 - Travaux", ["travaux", "chantier", "entretien", "fauchage", "balayage"]),
]
GROUPE_DEFAUT = "5 - Information et services"


def sans_accents(s):
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def groupe_de(type_lib):
    t = sans_accents(type_lib)
    for nom, mots in GROUPES:
        if any(m in t for m in mots):
            return nom
    return GROUPE_DEFAUT


# --------------------------------------------------------------------------- HTML -> lignes
class Texte(HTMLParser):
    BLOCS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.morceaux = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCS:
            self.morceaux.append("\n")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag in self.BLOCS:
            self.morceaux.append("\n")

    def handle_data(self, data):
        self.morceaux.append(data)


def lignes_texte(contenu):
    if "<" in contenu and ">" in contenu:
        p = Texte(); p.feed(contenu); contenu = "".join(p.morceaux)
    contenu = html.unescape(contenu).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", l).strip() for l in contenu.splitlines() if l.strip()]


# --------------------------------------------------------------------------- analyse
RE_DEP = re.compile(r"^D[ée]partement\s+(\d{2}|2A|2B|97\d)\b\s*(?:\(([^)]+)\))?", re.I)
RE_DATE_PAGE = re.compile(r"(\d{1,2})(?:er)?\s+([a-zéèûô]+)\s+(\d{4})\s*(?:à|a|-|,)?\s*(\d{1,2})\s*[h:]\s*(\d{2})", re.I)
RE_EVT = re.compile(r"^(\*{1,3})\s*(.+)$")
RE_ROUTE = re.compile(r"\b((?:A|N|D|E|RN|RD)\s?\d{1,4}[a-z]?)\b")
RE_ORIGINE = re.compile(r"\[Origine\s*:\s*([^\]]+)\]")


def date_page(lignes):
    """Date « du 22 octobre 2026 à 04h42 » cherchée dans toute la page."""
    mois = {sans_accents(k): v for k, v in MOIS.items()}
    for texte in lignes + [" ".join(lignes[:300])]:
        for m in RE_DATE_PAGE.finditer(texte):
            mo = mois.get(sans_accents(m.group(2)))
            if mo:
                try:
                    return datetime(int(m.group(3)), mo, int(m.group(1)), int(m.group(4)), int(m.group(5)))
                except ValueError:
                    pass
    return None


def decouper(lignes, deps):
    """Rend [(dep, nom_dep, ligne_evenement)] pour les départements voulus.
    Une ligne d'événement peut être coupée sur plusieurs lignes HTML :
    on recolle jusqu'au prochain '*' ou au prochain département."""
    res, dep, nom, cur = [], None, None, None
    for l in lignes:
        m = RE_DEP.match(l)
        if m:
            if cur: res.append(cur)
            cur = None
            dep, nom = m.group(1).upper(), (m.group(2) or "").strip()
            continue
        if dep is None or dep not in deps:
            continue
        if RE_EVT.match(l):
            if cur: res.append(cur)
            cur = [dep, nom, l]
        elif cur:
            cur[2] += " " + l
    if cur: res.append(cur)
    return [tuple(r) for r in res]


def analyser(texte, ref):
    m = RE_EVT.match(texte)
    importance, corps = len(m.group(1)), m.group(2).strip()
    origine = (RE_ORIGINE.search(corps) or [None, ""])[1].strip()
    corps_net = RE_ORIGINE.sub("", corps).strip(" ;")

    # Heure éventuelle en tête : « À 7h21, » ou « Le 15/10/2026 à 20h28, »
    heure = None
    mh = re.match(r"^Le\s+(\d{2})/(\d{2})/(\d{4})\s+à\s+(\d{1,2})h(\d{2}),\s*", corps_net)
    if mh:
        d, mo, y, h, mi = map(int, mh.groups()); heure = datetime(y, mo, d, h, mi)
        corps_net = corps_net[mh.end():]
    else:
        mh = re.match(r"^[ÀA]\s+(\d{1,2})h(\d{2}),\s*", corps_net)
        if mh:
            base = ref or datetime.now(timezone.utc).astimezone(PARIS).replace(tzinfo=None)
            heure = base.replace(hour=int(mh.group(1)), minute=int(mh.group(2)), second=0, microsecond=0)
            if heure > base:  # « À 22h55 » lu à 10h42 = la veille au soir
                heure -= timedelta(days=1)
            corps_net = corps_net[mh.end():]

    parties = [p.strip() for p in corps_net.split(",")]
    type_lib = parties[0]
    signale = "événement signalé" in sans_accents(type_lib) or "evenement signale" in sans_accents(type_lib)
    type_lib = re.sub(r"\s*-\s*[ÉE]v[ée]nement signal[ée]\s*$", "", type_lib)

    route = ""
    mr = RE_ROUTE.search(corps_net[len(parties[0]):])
    if mr: route = re.sub(r"\s", "", mr.group(1)).upper()

    sens = ""
    ms = re.search(r"\(sens ([^)]+)\)", corps_net)
    if ms: sens = ms.group(1)
    elif "(deux sens)" in corps_net: sens = "deux sens"
    direction = ""
    md = re.search(r",\s*de (.+?) vers (.+?)(?:\s*\(sens|,)", corps_net)
    if md: direction = f"{md.group(1)} → {md.group(2)}"

    pr_debut = pr_fin = ""
    mp = re.search(r"entre les PR ([\d+]+) et ([\d+]+)", corps_net)
    if mp: pr_debut, pr_fin = mp.groups()
    else:
        mp = re.search(r"\bau PR ([\d+]+)", corps_net)
        if mp: pr_debut = mp.group(1)
    longueur = ""
    ml = re.search(r"\(sur ([\d,\.]+) km\)", corps_net)
    if ml: longueur = float(ml.group(1).replace(",", "."))

    # Communes : « , de X à Y » (après le PR) ou « , à X »
    commune_debut = commune_fin = ""
    apres_pr = corps_net[mp.end():] if mp else corps_net
    mc = re.search(r",\s*de ([^,;]+?) à ([^,;]+)", apres_pr)
    if mc and " vers " not in mc.group(1):
        commune_debut, commune_fin = mc.group(1).strip(), mc.group(2).strip()
    else:
        mc = re.search(r",\s*à ([^,;]+)", apres_pr) or re.search(r",\s*à ([^,;]+)", corps_net)
        if mc: commune_debut = mc.group(1).strip()
    commune_debut = re.sub(r"\s*\(.*?\)\s*$", "", commune_debut)
    commune_fin = re.sub(r"\s*\(.*?\)\s*$", "", commune_fin)

    return {
        "importance": importance,
        "type_evt": type_lib,
        "groupe": groupe_de(type_lib),
        "signale": "oui" if signale else "non",
        "route": route,
        "sens": sens,
        "direction": direction,
        "pr_debut": pr_debut,
        "pr_fin": pr_fin,
        "longueur_km": longueur,
        "commune": commune_debut,
        "commune_fin": commune_fin,
        "heure_evt": heure.strftime("%Y-%m-%dT%H:%M:00") if heure else "",
        "origine": origine,
        "concession": "oui" if origine.upper().startswith("SCA") else "non",
        "texte": corps,
    }


# --------------------------------------------------------------------------- géocodage
def cle(nom):
    n = sans_accents(nom)
    n = re.sub(r"\bst\b", "saint", n); n = re.sub(r"\bste\b", "sainte", n)
    n = re.sub(r"\bles\b", "les", n)
    return re.sub(r"[^a-z0-9]", "", n)


class Geocodeur:
    def __init__(self):
        self.table, self.par_insee = {}, {}
        with open(TABLE_COMMUNES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.table[(r["dep"], cle(r["nom"]))] = (float(r["lon"]), float(r["lat"]))
                self.par_insee[r["insee"]] = (float(r["lon"]), float(r["lat"]))
        try:
            self.cache = json.load(open(CACHE_GEO, encoding="utf-8"))
        except Exception:
            self.cache = {}
        self.cache_modifie = False

    def __call__(self, nom, dep):
        if not nom:
            return None
        k = (dep, cle(nom))
        if k in self.table:
            return self.table[k]
        ck = f"{dep}|{k[1]}"
        if ck in self.cache:
            return tuple(self.cache[ck]) if self.cache[ck] else None
        pos = None
        try:  # API officielle, gratuite et sans clé (communes actuelles puis anciennes communes)
            for typ in ("commune-actuelle", "commune-deleguee,commune-associee"):
                q = urllib.parse.urlencode({"nom": nom, "codeDepartement": dep, "type": typ,
                                            "fields": "centre,chefLieu,code", "limit": 1})
                with urllib.request.urlopen(f"https://geo.api.gouv.fr/communes?{q}", timeout=15) as r:
                    d = json.load(r)
                if not d:
                    continue
                c = d[0]
                if c.get("centre"):
                    pos = tuple(c["centre"]["coordinates"]); break
                if c.get("chefLieu") and c["chefLieu"] in self.par_insee:
                    pos = self.par_insee[c["chefLieu"]]; break
        except Exception as e:
            print(f"  ! géocodage impossible pour {nom} ({dep}) : {e}", file=sys.stderr)
            return None
        self.cache[ck] = pos; self.cache_modifie = True
        return pos

    def sauver(self):
        if self.cache_modifie:
            json.dump(self.cache, open(CACHE_GEO, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


# --------------------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fichier", help="page HTML déjà téléchargée (sinon téléchargement)")
    ap.add_argument("--deps", default="21,58,71,89", help="départements, ex. 21,58,71,89")
    ap.add_argument("--sortie", default=".", help="dossier de sortie")
    ap.add_argument("--tout", action="store_true", help="garder aussi les événements non concédés (GRT)")
    a = ap.parse_args()
    deps = {d.strip().upper() for d in a.deps.split(",")}

    if a.fichier:
        brut = open(a.fichier, "rb").read()
    else:
        # http:// d'abord (comme convert.py) : le https de tipi peut échouer
        # sur les serveurs GitHub (chaîne de certificat incomplète).
        brut, erreurs = None, []
        for url in (URL.replace("https://", "http://"), URL):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "geocrise-inforoutes"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    brut = r.read()
                print(f"Page téléchargée : {url} ({len(brut)} octets)")
                break
            except Exception as e:
                erreurs.append(f"{url} -> {e}")
        if brut is None:
            sys.exit("ERREUR de téléchargement :\n  " + "\n  ".join(erreurs))
    for enc in ("utf-8", "cp1252", "latin-1"):
        try: contenu = brut.decode(enc); break
        except UnicodeDecodeError: continue

    lignes = lignes_texte(contenu)
    ref = date_page(lignes)
    if not any(RE_DEP.match(l) for l in lignes):
        sys.exit("ERREUR : aucun bloc « Département NN » trouvé — la page a peut-être changé de format.")

    geo = Geocodeur()
    maj = datetime.now(timezone.utc).astimezone(PARIS).strftime("%Y-%m-%dT%H:%M:%S")
    publi = ref.strftime("%Y-%m-%dT%H:%M:00") if ref else ""
    pts, lins, vus, non_places = [], [], set(), []

    for dep, nom_dep, texte in decouper(lignes, deps):
        p = analyser(texte, ref)
        if not a.tout and p["concession"] != "oui":
            continue  # les événements GRT sont déjà dans la couche Bison Futé RRN
        ident = hashlib.md5(f"{dep}|{p['texte']}".encode()).hexdigest()[:12]
        if ident in vus:
            continue
        vus.add(ident)
        p.update({"evt_id": ident, "dep": dep, "nom_dep": nom_dep,
                  "date_publication": publi, "date_maj": maj})
        a1 = geo(p["commune"], dep)
        a2 = geo(p["commune_fin"], dep) if p["commune_fin"] else None
        if a1 and a2 and a1 != a2:
            p["precision"] = "communes (début → fin)"
            lins.append({"type": "Feature", "properties": p,
                         "geometry": {"type": "LineString", "coordinates": [list(a1), list(a2)]}})
            mid = [(a1[0] + a2[0]) / 2, (a1[1] + a2[1]) / 2]
            pts.append({"type": "Feature", "properties": dict(p),
                        "geometry": {"type": "Point", "coordinates": mid}})
        elif a1 or a2:
            p["precision"] = "commune"
            pts.append({"type": "Feature", "properties": p,
                        "geometry": {"type": "Point", "coordinates": list(a1 or a2)}})
        else:
            non_places.append(f"{dep} | {p['commune'] or '?'} | {texte[:90]}")
    geo.sauver()

    os.makedirs(a.sortie, exist_ok=True)
    for nom, feats in (("geocrise_inforoutes_autoroutes_ponctuels.geojson", pts),
                       ("geocrise_inforoutes_autoroutes_lineaires.geojson", lins)):
        with open(os.path.join(a.sortie, nom), "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "name": nom[:-8],
                       "date_publication": publi, "date_maj": maj, "features": feats},
                      f, ensure_ascii=False, indent=1)
    print(f"Page du {publi or '?'} — départements {','.join(sorted(deps))} : "
          f"{len(pts)} points, {len(lins)} tronçons, {len(non_places)} non placés")
    for n in non_places:
        print("  non placé :", n)


if __name__ == "__main__":
    main()
