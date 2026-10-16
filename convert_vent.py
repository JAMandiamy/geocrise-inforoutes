# -*- coding: utf-8 -*-
"""
Vent à 10 m en temps réel (Open-Meteo, modèle Météo-France AROME/ARPEGE)
sur une grille de points, converti en GeoJSON (WGS84) :
  - geocrise_vent_10m.geojson

Champs : vitesse (km/h), rafales (km/h), direction d'où vient le vent (°),
direction vers où il souffle (° - à utiliser pour la rotation des flèches),
niveau d'alerte selon les rafales, heure de la valeur (heure de Paris).

Lancé par GitHub Actions. Pour respecter la limite gratuite d'Open-Meteo,
le script ne réinterroge l'API que si le fichier a plus de 25 minutes.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ====== ZONE ET GRILLE (modifiable) ========================================
# Bourgogne : lon 2.8 -> 5.6, lat 46.1 -> 48.45 ; pas de 0.25° (~20-28 km)
LON_MIN, LON_MAX = 2.8, 5.6
LAT_MIN, LAT_MAX = 46.1, 48.45
PAS = 0.25
# ===========================================================================

SORTIE = "geocrise_vent_10m.geojson"
API = "https://api.open-meteo.com/v1/forecast"
MODELE = "meteofrance_seamless"
LOT = 50                 # points par requête
AGE_MIN_SECONDES = 25 * 60
POINTS_DIRECTIONS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                     "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]


def grille():
    pts = []
    nlat = int(round((LAT_MAX - LAT_MIN) / PAS)) + 1
    nlon = int(round((LON_MAX - LON_MIN) / PAS)) + 1
    for i in range(nlat):
        for j in range(nlon):
            pts.append((round(LON_MIN + j * PAS, 4), round(LAT_MIN + i * PAS, 4)))
    return pts


def trop_recent():
    if os.getenv("VENT_FORCER"):
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
        "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "models": MODELE,
        "wind_speed_unit": "kmh",
        "timezone": "Europe/Paris",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "geocrise-inforoutes"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data if isinstance(data, list) else [data]


def niveau(rafales):
    if rafales is None:
        return "inconnu"
    if rafales >= 100:
        return "4 - tres fort (>=100 km/h)"
    if rafales >= 80:
        return "3 - fort (80-100 km/h)"
    if rafales >= 50:
        return "2 - modere (50-80 km/h)"
    return "1 - faible (<50 km/h)"


def secteur(deg):
    if deg is None:
        return None
    return POINTS_DIRECTIONS[int((deg % 360) / 22.5 + 0.5) % 16]


def main():
    if trop_recent():
        print("Vent : fichier récent (< 25 min), pas de nouvel appel.")
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
            v, d, g = c.get("wind_speed_10m"), c.get("wind_direction_10m"), c.get("wind_gusts_10m")
            if v is None or d is None:
                continue
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "vitesse_kmh": v,
                    "rafales_kmh": g,
                    "direction_deg": d,                       # d'où vient le vent
                    "direction_vers_deg": (d + 180) % 360,    # rotation des flèches
                    "secteur": secteur(d),
                    "niveau_rafales": niveau(g),
                    "heure_valeur": c.get("time"),            # heure de Paris
                    "modele": MODELE,
                },
            })
    if not feats:
        sys.exit("Aucune donnée de vent reçue - fichier conservé.")
    out = {"type": "FeatureCollection",
           "maj_script": datetime.now(timezone.utc).isoformat(),
           "features": feats}
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    vmax = max(f["properties"]["rafales_kmh"] or 0 for f in feats)
    print(f"Vent : {len(feats)} points, rafale max {vmax} km/h")


if __name__ == "__main__":
    main()
