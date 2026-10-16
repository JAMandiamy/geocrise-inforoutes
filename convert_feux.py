# -*- coding: utf-8 -*-
"""
Convertit l'API feufrance.fr (feux détectés par satellite, données NASA FIRMS)
en 3 fichiers GeoJSON (WGS84) :
  - geocrise_feux_evenements.geojson    : feux regroupés (points)
  - geocrise_feux_emprises.geojson      : emprise de chaque feu (polygones, d'après la bbox)
  - geocrise_feux_detections_24h.geojson: détections satellites brutes des dernières 24 h
Les sources de chaleur permanentes (usines, torchères...) sont exclues.
Lancé automatiquement par GitHub Actions (.github/workflows/maj.yml).
"""
import json
import os
import sys
import urllib.request

URL = "https://feufrance.fr/api/fires"
MAX_SIZE = 50 * 1024 * 1024
PAD = 0.0005  # ~50 m : élargit les emprises réduites à un point


def load():
    local = os.getenv("FEUX_FICHIER")  # pour tester sur un fichier local
    if local:
        with open(local, "rb") as f:
            return json.load(f)
    req = urllib.request.Request(URL, headers={"User-Agent": "geocrise-inforoutes"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        sys.exit("Réponse trop volumineuse : arrêt.")
    return json.loads(data)


def fc(features):
    return {"type": "FeatureCollection", "features": features}


def event_props(e, maj):
    return {
        "id": e.get("id"),
        "commune": e.get("commune"),
        "departement": e.get("departement"),
        "dep_code": e.get("departementCode"),
        "actif": "oui" if e.get("active") else "non",
        "intensite": e.get("intensity"),
        "frp_max_mw": e.get("frpPeak"),
        "frp_24h_mw": e.get("frp24"),
        "frp_total_mw": e.get("frpTotal"),
        "nb_detect_24h": e.get("detections24"),
        "nb_detect_total": e.get("detectionsTotal"),
        "surface_ha": e.get("areaHa"),
        "premiere_detection": e.get("firstSeen"),
        "derniere_detection": e.get("lastSeen"),
        "nb_jours": e.get("daysSeen"),
        "satellites": ", ".join(e.get("satellites") or []),
        "maj_source": maj,
    }


def bbox_polygon(b):
    xmin, ymin, xmax, ymax = b
    if xmax - xmin < 2 * PAD:
        xmin, xmax = xmin - PAD, xmax + PAD
    if ymax - ymin < 2 * PAD:
        ymin, ymax = ymin - PAD, ymax + PAD
    return {"type": "Polygon", "coordinates": [[[xmin, ymin], [xmax, ymin], [xmax, ymax],
                                                 [xmin, ymax], [xmin, ymin]]]}


def main():
    d = load()
    events = d.get("events")
    if not isinstance(events, list):
        print("Format inattendu ou réponse vide : fichiers conservés.")
        return
    maj = d.get("lastAcquisition") or d.get("generatedAt") or ""
    events = [e for e in events
              if not e.get("likelyPermanentSource") and e.get("lat") is not None and e.get("lon") is not None]

    points, polys = [], []
    for e in events:
        props = event_props(e, maj)
        points.append({"type": "Feature", "properties": props,
                       "geometry": {"type": "Point", "coordinates": [e["lon"], e["lat"]]}})
        b = e.get("bbox")
        if isinstance(b, list) and len(b) == 4:
            polys.append({"type": "Feature", "properties": props, "geometry": bbox_polygon(b)})

    dets = []
    for x in d.get("detections") or []:
        if x.get("lat") is None or x.get("lon") is None:
            continue
        dets.append({"type": "Feature",
                     "geometry": {"type": "Point", "coordinates": [x["lon"], x["lat"]]},
                     "properties": {
                         "date_acquisition": x.get("acq"),
                         "jour": x.get("day"),
                         "satellite": x.get("sat"),
                         "frp_mw": x.get("frp"),
                         "confiance": x.get("confidence"),
                         "brillance_k": x.get("brightness"),
                         "jour_nuit": {"D": "jour", "N": "nuit"}.get(x.get("daynight"), x.get("daynight")),
                     }})

    for name, feats in (("geocrise_feux_evenements", points),
                        ("geocrise_feux_emprises", polys),
                        ("geocrise_feux_detections_24h", dets)):
        with open(name + ".geojson", "w", encoding="utf-8") as f:
            json.dump(fc(feats), f, ensure_ascii=False)
    actifs = sum(1 for p in points if p["properties"]["actif"] == "oui")
    print(f"{len(points)} feux ({actifs} actifs), {len(polys)} emprises, {len(dets)} détections 24 h")


if __name__ == "__main__":
    main()
