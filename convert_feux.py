# -*- coding: utf-8 -*-
"""
Feux détectés par satellite — source directe NASA FIRMS (remplace feufrance.fr, devenu payant).
Produit les 3 mêmes fichiers GeoJSON (WGS84), avec les mêmes champs qu'avant :
  - geocrise_feux_evenements.geojson     : feux regroupés (points)
  - geocrise_feux_emprises.geojson       : emprise de chaque feu (polygone d'après la bbox)
  - geocrise_feux_detections_24h.geojson : détections satellites brutes des dernières 24 h

Clé gratuite : https://firms.modaps.eosdis.nasa.gov/api/map_key/  -> secret GitHub FIRMS_MAP_KEY
Satellites : VIIRS S-NPP, NOAA-20, NOAA-21 (pixels 375 m) + MODIS (1 km), 5 derniers jours.
Les détections sont regroupées en « feux » (pixels voisins à moins de ~1,2 km).
Les sources de chaleur permanentes probables (usines, torchères : vues 4 jours sur 5) sont exclues.
Commune et département : API officielle geo.api.gouv.fr (hors France = feu ignoré).
Lancé automatiquement par GitHub Actions (.github/workflows/maj.yml).
"""
import csv
import hashlib
import io
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

MAP_KEY = os.getenv("FIRMS_MAP_KEY", "").strip()
SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"]
ZONE = os.getenv("FEUX_ZONE", "-5.6,41.2,9.8,51.2")   # France métropolitaine + Corse (O,S,E,N)
JOURS = int(os.getenv("FEUX_JOURS", "5"))              # 1 à 5
DIST_KM = 1.2                                          # distance de regroupement des pixels
PAD = 0.0005                                           # ~50 m : élargit les emprises réduites à un point
SAT = {"N": "Suomi NPP", "N20": "NOAA-20", "N21": "NOAA-21", "1": "NOAA-20", "Terra": "Terra",
       "Aqua": "Aqua", "T": "Terra", "A": "Aqua"}
CACHE = "cache_feux_communes.json"


# --------------------------------------------------------------------------- lecture FIRMS
def telecharger(source):
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{source}/{ZONE}/{JOURS}"
    req = urllib.request.Request(url, headers={"User-Agent": "geocrise-inforoutes"})
    with urllib.request.urlopen(req, timeout=90) as r:
        texte = r.read().decode("utf-8", "replace")
    if not texte.lower().startswith("latitude"):
        raise RuntimeError(texte.strip()[:200] or "réponse vide")
    return texte


def lire():
    """Rend la liste des détections. FEUX_FICHIERS=a.csv,b.csv pour tester hors ligne."""
    locaux = os.getenv("FEUX_FICHIERS")
    textes = []
    if locaux:
        textes = [open(f, encoding="utf-8").read() for f in locaux.split(",")]
    else:
        if not MAP_KEY:
            sys.exit("ERREUR : secret FIRMS_MAP_KEY absent (voir l'en-tête du script).")
        for s in SOURCES:
            try:
                textes.append(telecharger(s))
            except Exception as e:
                print(f"  ! {s} : {e}")
        if not textes:
            sys.exit("ERREUR : aucune source FIRMS n'a répondu — fichiers conservés.")
    dets = []
    for t in textes:
        for r in csv.DictReader(io.StringIO(t)):
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
                hhmm = r["acq_time"].zfill(4)
                quand = datetime.strptime(f'{r["acq_date"]} {hhmm}', "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            modis = "bright_t31" in r
            conf = r.get("confidence", "")
            if modis and conf.isdigit():
                conf = "faible" if int(conf) < 30 else "nominale" if int(conf) < 80 else "haute"
            else:
                conf = {"l": "faible", "n": "nominale", "h": "haute"}.get(conf.lower(), conf)
            dets.append({
                "lat": lat, "lon": lon, "t": quand,
                "sat": SAT.get(r.get("satellite", ""), r.get("satellite", "")),
                "frp": float(r.get("frp") or 0),
                "conf": conf,
                "bright": float(r.get("bright_ti4") or r.get("brightness") or 0),
                "dn": r.get("daynight", ""),
                "pix_ha": 100.0 if modis else 14.06,   # 1 km² MODIS, 375 m x 375 m VIIRS
            })
    return dets


# --------------------------------------------------------------------------- regroupement
def km(a, b):
    dy = (a["lat"] - b["lat"]) * 111.2
    dx = (a["lon"] - b["lon"]) * 111.2 * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return math.hypot(dx, dy)


def regrouper(dets):
    cell = 0.02
    grille = {}
    for i, d in enumerate(dets):
        grille.setdefault((int(d["lat"] / cell), int(d["lon"] / cell)), []).append(i)
    parent = list(range(len(dets)))

    def racine(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (gy, gx), idx in grille.items():
        voisins = [j for dy in (-1, 0, 1) for dx in (-1, 0, 1) for j in grille.get((gy + dy, gx + dx), [])]
        for i in idx:
            for j in voisins:
                if j > i and km(dets[i], dets[j]) <= DIST_KM:
                    a, b = racine(i), racine(j)
                    if a != b:
                        parent[b] = a
    groupes = {}
    for i in range(len(dets)):
        groupes.setdefault(racine(i), []).append(dets[i])
    return list(groupes.values())


def intensite(frp):
    return "faible" if frp < 10 else "modérée" if frp < 50 else "forte" if frp < 200 else "très forte"


# --------------------------------------------------------------------------- commune
class Communes:
    def __init__(self):
        try:
            self.cache = json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            self.cache = {}

    def __call__(self, lat, lon):
        k = f"{lat:.3f},{lon:.3f}"
        if k in self.cache:
            return self.cache[k]
        res = None
        try:
            q = urllib.parse.urlencode({"lat": lat, "lon": lon, "fields": "nom,code,departement", "limit": 1})
            with urllib.request.urlopen(f"https://geo.api.gouv.fr/communes?{q}", timeout=15) as r:
                d = json.load(r)
            if d:
                c = d[0]
                res = {"commune": c.get("nom"), "departement": (c.get("departement") or {}).get("nom"),
                       "dep_code": (c.get("departement") or {}).get("code")}
            else:
                res = {}   # hors de France
        except Exception as e:
            print(f"  ! commune introuvable pour {k} : {e}")
            return None    # réseau : on ne met pas en cache
        self.cache[k] = res
        return res

    def sauver(self):
        if len(self.cache) > 20000:
            self.cache = dict(list(self.cache.items())[-20000:])
        json.dump(self.cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)


# --------------------------------------------------------------------------- sortie
def fc(features):
    return {"type": "FeatureCollection", "features": features}


def bbox_polygon(b):
    xmin, ymin, xmax, ymax = b
    if xmax - xmin < 2 * PAD:
        xmin, xmax = xmin - PAD, xmax + PAD
    if ymax - ymin < 2 * PAD:
        ymin, ymax = ymin - PAD, ymax + PAD
    return {"type": "Polygon", "coordinates": [[[xmin, ymin], [xmax, ymin], [xmax, ymax],
                                                 [xmin, ymax], [xmin, ymin]]]}


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    dets = lire()
    if not dets:
        print("Aucune détection sur la zone : fichiers vidés.")
    maintenant = datetime.now(timezone.utc)
    il_y_a_24h = maintenant - timedelta(hours=24)
    derniere = max((d["t"] for d in dets), default=maintenant)
    maj = iso(derniere)
    communes = Communes()

    points, polys, garder = [], [], set()
    for g in regrouper(dets):
        jours = {d["t"].date() for d in g}
        if len(jours) >= max(4, JOURS - 1) and JOURS >= 4:
            continue  # source de chaleur permanente probable (usine, torchère...)
        g.sort(key=lambda d: d["t"])
        lat = sum(d["lat"] * max(d["frp"], 0.1) for d in g) / sum(max(d["frp"], 0.1) for d in g)
        lon = sum(d["lon"] * max(d["frp"], 0.1) for d in g) / sum(max(d["frp"], 0.1) for d in g)
        lieu = communes(lat, lon)
        if lieu == {}:
            continue  # hors de France
        lieu = lieu or {}
        r24 = [d for d in g if d["t"] >= il_y_a_24h]
        pixels = {(round(d["lat"], 3), round(d["lon"], 3)): d["pix_ha"] for d in g}
        props = {
            "id": hashlib.md5(f'{g[0]["lat"]:.3f}{g[0]["lon"]:.3f}{iso(g[0]["t"])}'.encode()).hexdigest()[:10],
            "commune": lieu.get("commune"),
            "departement": lieu.get("departement"),
            "dep_code": lieu.get("dep_code"),
            "actif": "oui" if r24 else "non",
            "intensite": intensite(max(d["frp"] for d in g)),
            "frp_max_mw": round(max(d["frp"] for d in g), 1),
            "frp_24h_mw": round(sum(d["frp"] for d in r24), 1),
            "frp_total_mw": round(sum(d["frp"] for d in g), 1),
            "nb_detect_24h": len(r24),
            "nb_detect_total": len(g),
            "surface_ha": round(sum(pixels.values()), 1),   # estimation : pixels touchés
            "premiere_detection": iso(g[0]["t"]),
            "derniere_detection": iso(g[-1]["t"]),
            "nb_jours": len(jours),
            "satellites": ", ".join(sorted({d["sat"] for d in g})),
            "maj_source": maj,
        }
        points.append({"type": "Feature", "properties": props,
                       "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]}})
        b = [min(d["lon"] for d in g), min(d["lat"] for d in g), max(d["lon"] for d in g), max(d["lat"] for d in g)]
        polys.append({"type": "Feature", "properties": props, "geometry": bbox_polygon(b)})
        for d in r24:
            garder.add(id(d))
    communes.sauver()

    det24 = []
    for d in dets:
        if id(d) in garder:
            det24.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
                          "properties": {
                              "date_acquisition": iso(d["t"]),
                              "jour": d["t"].strftime("%Y-%m-%d"),
                              "satellite": d["sat"],
                              "frp_mw": d["frp"],
                              "confiance": d["conf"],
                              "brillance_k": d["bright"],
                              "jour_nuit": {"D": "jour", "N": "nuit"}.get(d["dn"], d["dn"]),
                          }})

    points.sort(key=lambda f: f["properties"]["derniere_detection"], reverse=True)
    for name, feats in (("geocrise_feux_evenements", points),
                        ("geocrise_feux_emprises", polys),
                        ("geocrise_feux_detections_24h", det24)):
        with open(name + ".geojson", "w", encoding="utf-8") as f:
            json.dump(fc(feats), f, ensure_ascii=False)
    actifs = sum(1 for p in points if p["properties"]["actif"] == "oui")
    print(f"{len(dets)} détections FIRMS -> {len(points)} feux ({actifs} actifs), "
          f"{len(polys)} emprises, {len(det24)} détections 24 h")


if __name__ == "__main__":
    main()
