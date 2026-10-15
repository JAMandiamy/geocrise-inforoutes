# -*- coding: utf-8 -*-
"""
Convertit le flux Bison Futé (DATEX II, réseau routier national non concédé)
en 2 fichiers GeoJSON (WGS84) :
  - geocrise_inforoutes_evenements_ponctuels.geojson
  - geocrise_inforoutes_evenements_lineaires.geojson
Lancé automatiquement par GitHub Actions (.github/workflows/maj.yml).
"""
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET

URL = "http://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/Evenementiel-DIR/grt/RRN/content.xml"
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
ATTRS = ["situation_id", "record_id", "type_evt", "debut", "fin",
         "creation", "gravite", "route", "commentaire"]
MAX_SIZE = 50 * 1024 * 1024  # 50 Mo maximum


def local(tag):
    """Nom de balise sans espace de noms."""
    return tag.rsplit("}", 1)[-1]


def first_text(elem, name):
    """Texte de la première balise 'name' trouvée sous elem (ou '')."""
    for e in elem.iter():
        if local(e.tag) == name and e.text and e.text.strip():
            return e.text.strip()
    return ""


def comment_text(rec):
    """Premier commentaire lisible (balise <value> sous un commentaire)."""
    for e in rec.iter():
        if local(e.tag) in ("generalPublicComment", "nonGeneralPublicComment", "comment"):
            txt = first_text(e, "value")
            if txt:
                return txt
    return ""


def coords_of(elem, tags):
    """Liste ordonnée des (lon, lat) trouvés dans les balises 'tags'."""
    out = []
    for e in elem.iter():
        if local(e.tag) in tags:
            lat, lon = first_text(e, "latitude"), first_text(e, "longitude")
            if lat and lon:
                pt = (float(lon), float(lat))
                if not out or out[-1] != pt:
                    out.append(pt)
    return out


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    records = []
    for sit in root.iter():
        if local(sit.tag) != "situation":
            continue
        sit_id = sit.get("id", "")
        for rec in sit.iter():
            if local(rec.tag) != "situationRecord":
                continue
            coords = coords_of(rec, ("pointCoordinates",))
            if not coords:  # repli : point d'affichage
                coords = coords_of(rec, ("locationForDisplay",))
            if not coords:
                continue
            records.append({
                "situation_id": sit_id,
                "record_id": rec.get("id", ""),
                "type_evt": (rec.get(XSI_TYPE) or "").split(":")[-1],
                "debut": first_text(rec, "overallStartTime"),
                "fin": first_text(rec, "overallEndTime"),
                "creation": first_text(rec, "situationRecordCreationTime"),
                "gravite": first_text(rec, "severity") or first_text(rec, "overallSeverity"),
                "route": first_text(rec, "roadNumber"),
                "commentaire": comment_text(rec)[:1000],
                "coords": coords,
            })
    return records



def to_geojson(rows, is_point):
    feats = []
    for r in rows:
        if is_point:
            geom = {"type": "Point", "coordinates": list(r["coords"][0])}
        else:
            geom = {"type": "LineString", "coordinates": [list(c) for c in r["coords"]]}
        feats.append({"type": "Feature", "geometry": geom,
                      "properties": {a: r[a] for a in ATTRS}})
    return {"type": "FeatureCollection", "features": feats}


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "geocrise-inforoutes"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        sys.exit("Flux trop volumineux : arrêt.")
    recs = parse(data)
    if not recs:
        print("Flux vide ou illisible : fichiers conservés.")
        return
    points = [r for r in recs if len(r["coords"]) == 1]
    lignes = [r for r in recs if len(r["coords"]) >= 2]
    for name, rows, is_point in (("geocrise_inforoutes_evenements_ponctuels", points, True),
                                 ("geocrise_inforoutes_evenements_lineaires", lignes, False)):
        with open(name + ".geojson", "w", encoding="utf-8") as f:
            json.dump(to_geojson(rows, is_point), f, ensure_ascii=False)
    print(f"{len(points)} ponctuels, {len(lignes)} linéaires")


if __name__ == "__main__":
    main()
