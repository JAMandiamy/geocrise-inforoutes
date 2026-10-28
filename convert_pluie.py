# -*- coding: utf-8 -*-
"""
Pluie en cours et à venir (Open-Meteo, modèle Météo-France AROME/ARPEGE)
sur une grille, convertie en GeoJSON (WGS84) :
  - geocrise_pluie.geojson         : points (centres de maille)
  - geocrise_pluie_mailles.geojson : mailles carrées (polygones), mêmes champs

Champs :
  intensite_mmh       : intensité actuelle (mm/h, sur le dernier quart d'heure)
  cumul_1h_passe_mm   : pluie tombée sur la dernière heure (mm)
  cumul_1h_avenir_mm  : pluie prévue sur l'heure qui vient (mm)
  cumul_3h_avenir_mm  : pluie prévue sur les 3 prochaines heures (mm)
  intensite_max_3h_mmh: intensité maximale prévue sur 3 h (mm/h)
  classe_actuelle     : classe d'intensité actuelle (pour la symbologie)
  classe_3h           : classe de l'intensité max prévue sur 3 h
  heure_valeur        : heure de la valeur (heure de Paris)

Lancé par GitHub Actions. Pour rester dans la limite gratuite d'Open-Meteo
(partagée avec convert_vent.py), le script ne réinterroge l'API que si le
fichier a plus de 55 minutes.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ====== ZONE ET GRILLE (modifiable) ========================================
# Même emprise que le vent : Bourgogne, pas de 0.25° (~20-28 km)
LON_MIN, LON_MAX = 2.8, 5.6
LAT_MIN, LAT_MAX = 46.1, 48.45
PAS = 0.25
# ===========================================================================

SORTIE = "geocrise_pluie.geojson"
SORTIE_MAILLES = "geocrise_pluie_mailles.geojson"
API = "https://api.open-meteo.com/v1/forecast"
MODELE = "meteofrance_seamless"
LOT = 50                    # points par requête
AGE_MIN_SECONDES = 55 * 60  # 1 appel par heure maximum
PASSE = 4                   # 4 quarts d'heure passés  = 1 h
AVENIR = 12                 # 12 quarts d'heure à venir = 3 h


def grille():
    pts = []
    nlat = int(round((LAT_MAX - LAT_MIN) / PAS)) + 1
    nlon = int(round((LON_MAX - LON_MIN) / PAS)) + 1
    for i in range(nlat):
        for j in range(nlon):
            pts.append((round(LON_MIN + j * PAS, 4), round(LAT_MIN + i * PAS, 4)))
    return pts


def trop_recent():
    if os.getenv("PLUIE_FORCER") or not os.path.exists(SORTIE_MAILLES):
        return False
    try:
        with open(SORTIE, encoding="utf-8") as f:
            maj = json.load(f).get("maj_script")
        age = time.time() - datetime.fromisoformat(maj).timestamp()
        return age < AGE_MIN_SECONDES
    except Exception:
        return False


def appel(lot):
    params = {
        "latitude": ",".join(str(p[1]) for p in lot),
        "longitude": ",".join(str(p[0]) for p in lot),
        "current": "precipitation",
        "minutely_15": "precipitation",
        "past_minutely_15": PASSE,
        "forecast_minutely_15": AVENIR,
        "models": MODELE,
        "timezone": "Europe/Paris",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "geocrise-inforoutes"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data if isinstance(data, list) else [data]


def classe(mmh):
    if mmh is None:
        return "inconnu"
    if mmh >= 10:
        return "4 - tres forte (>=10 mm/h)"
    if mmh >= 4:
        return "3 - forte (4-10 mm/h)"
    if mmh >= 1:
        return "2 - moderee (1-4 mm/h)"
    if mmh >= 0.1:
        return "1 - faible (0.1-1 mm/h)"
    return "0 - pas de pluie"


def somme(valeurs):
    v = [x for x in valeurs if x is not None]
    return round(sum(v), 1) if v else None


def main():
    if trop_recent():
        print("Pluie : fichier récent (< 55 min), pas de nouvel appel.")
        return
    pts = grille()
    feats = []
    for k in range(0, len(pts), LOT):
        lot = pts[k:k + LOT]
        try:
            res = appel(lot)
        except Exception as e:
            sys.exit(f"Erreur Open-Meteo : {e} - fichier conservé.")
        for (lon, lat), r in zip(lot, res):
            c = r.get("current") or {}
            q = (r.get("minutely_15") or {}).get("precipitation") or []
            passe, avenir = q[:PASSE], q[PASSE:PASSE + AVENIR]

            actuel = c.get("precipitation")          # mm sur le dernier 1/4 h
            if actuel is None and passe:
                actuel = passe[-1]
            if actuel is None:
                continue
            intensite = round(actuel * 4, 1)          # mm / 1/4 h -> mm/h
            vals_avenir = [x for x in avenir if x is not None]
            imax = round(max(vals_avenir) * 4, 1) if vals_avenir else None

            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "intensite_mmh": intensite,
                    "cumul_1h_passe_mm": somme(passe),
                    "cumul_1h_avenir_mm": somme(avenir[:4]),
                    "cumul_3h_avenir_mm": somme(avenir),
                    "intensite_max_3h_mmh": imax,
                    "classe_actuelle": classe(intensite),
                    "classe_3h": classe(imax),
                    "heure_valeur": c.get("time"),    # heure de Paris
                    "modele": MODELE,
                },
            })
    if not feats:
        sys.exit("Aucune donnée de pluie reçue - fichier conservé.")
    maj = datetime.now(timezone.utc).isoformat()
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "maj_script": maj, "features": feats},
                  f, ensure_ascii=False)

    # Mailles carrées centrées sur chaque point (mêmes champs)
    h = PAS / 2
    mailles = []
    for ft in feats:
        lon, lat = ft["geometry"]["coordinates"]
        x0, x1 = round(lon - h, 6), round(lon + h, 6)
        y0, y1 = round(lat - h, 6), round(lat + h, 6)
        mailles.append({
            "type": "Feature",
            "geometry": {"type": "Polygon",
                         "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]},
            "properties": ft["properties"],
        })
    with open(SORTIE_MAILLES, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "maj_script": maj, "features": mailles},
                  f, ensure_ascii=False)
    nb = sum(1 for f in feats if f["properties"]["intensite_mmh"] >= 0.1)
    imax = max(f["properties"]["intensite_mmh"] for f in feats)
    print(f"Pluie : {len(feats)} points, {nb} sous la pluie, intensité max {imax} mm/h")


if __name__ == "__main__":
    main()
