#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
csv2rdf.py – CSV → RDF   (Turtle / JSON-LD / N-Triples / RDF-XML)

**Cambios clave respecto a tu versión original**
----------------------------------------------------------------
1.  Plantillas de entidad (`entity_templates`)
    •  Soportan el placeholder **{id}** dentro de `path`.
    •  Nuevo campo `id_source`  
       ```json
       "id_source": {"from_column": "Author(s) ID", "match_by_index": true}
       ```
       –igual sintaxis que en `literals`–  
       Se usa para sustituir {id}.
    •  Nuevo campo opcional **inverse_predicate** para crear el enlace
       inverso `<obj_uri>  inverse_predicate  <subj>`.

2.  Sin tocar nada más de la API ni del `cfg.json`.  
    Si no declaras `id_source` o `inverse_predicate`, el
    comportamiento queda idéntico al anterior.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime
from typing import Dict, List, Tuple, Union

import pandas as pd
from rdflib import Graph, Literal, Namespace, RDF, RDFS, SKOS, URIRef, XSD

# --------------------------------------------------------------------------- #
# 1.  Configuración por defecto
# --------------------------------------------------------------------------- #
BASE_DEFAULT = "http://example.org/resource/"
SEP_DEFAULT = ","

NS_INTERNAL = {"rec": "", "col": "column/", "val": "value/"}

DEFAULT_CFG: Dict[str, Union[str, Dict, List, None]] = {
    "base_uri": BASE_DEFAULT,
    "entity_base_uri": BASE_DEFAULT,
    "prefixes": {
        "xsd": str(XSD),
        "bibo": "http://purl.org/ontology/bibo/"
    },
    "csv_delimiter": ",",
    "separator": SEP_DEFAULT,
    "primary_key": None,
    "multivalued": {},
    "catalogs": [],
    "relations": [],
    "property_map": {},
    "entity_templates": {},
    "classes": ["bibo:Article"],
    "datatype_overrides": {},
    "lang": "en",
    "format": "turtle",
    "skip_rows": 0,
    "quotechar": "\"",
    "escapechar": None
}

SUPPORTED_FORMATS = {
    "turtle": "turtle",
    "ttl": "turtle",
    "json-ld": "json-ld",
    "nt": "nt",
    "ntriples": "nt",
    "xml": "xml",
    "rdfxml": "xml"
}

# --------------------------------------------------------------------------- #
# 2.  Funciones auxiliares
# --------------------------------------------------------------------------- #
def safe(text: str) -> str:
    """Normaliza a ASCII y reemplaza lo no alfanumérico por “_”."""
    txt = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", "_", txt.strip()).strip("_")


def split_cell(cell: str, sep: str) -> List[str]:
    """Devuelve lista de valores de una celda según separador."""
    if not sep:
        return [str(cell).strip()]
    return [x.strip() for x in str(cell).split(sep) if x.strip()]


def expand(curie: str, pref: Dict[str, str]) -> str:
    """Expande un CURIE con la tabla de prefijos."""
    if ":" in curie:
        pre, loc = curie.split(":", 1)
        return pref.get(pre, pre) + loc
    return curie


def infer_datatype(value: str):
    """Heurística básica de XSD datatype."""
    v = str(value).strip()
    if re.fullmatch(r"-?\d+", v):
        return XSD.integer
    if re.fullmatch(r"-?\d+\.\d+", v):
        return XSD.decimal
    if v.lower() in ("true", "false"):
        return XSD.boolean
    try:
        dt = datetime.fromisoformat(v)
        return XSD.dateTime if dt.time() != datetime.min.time() else XSD.date
    except ValueError:
        return None


def pk_to_uri(pk_val: str, base: str) -> URIRef:
    """Construye URI de la clave primaria, respetando URLs absolutas."""
    return URIRef(pk_val) if pk_val.startswith(("http://", "https://")) else URIRef(base + safe(pk_val))

# --------------------------------------------------------------------------- #
# 3.  Función principal
# --------------------------------------------------------------------------- #
def csv_to_rdf(csv_path: str, cfg_path: str = None,
               out_path: str = None, to_stdout: bool = False) -> None:
    # --- 3.1  Cargar configuración ----------------------------------------- #
    cfg: Dict = DEFAULT_CFG.copy()
    if cfg_path:
        with open(cfg_path, encoding="utf-8") as f:
            cfg.update(json.load(f))

    if isinstance(cfg.get("classes"), str):
        cfg["classes"] = [c.strip() for c in cfg["classes"].split("|") if c.strip()]

    # --- 3.2  Logging + CSV ------------------------------------------------- #
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    log = logging.getLogger("csv2rdf")

    read_opts = {
        "dtype": str,
        "sep": cfg["csv_delimiter"],
        "quotechar": cfg["quotechar"],
        "escapechar": cfg["escapechar"],
        "quoting": csv.QUOTE_MINIMAL,
        "skiprows": cfg["skip_rows"]
    }
    df = pd.read_csv(csv_path, **{k: v for k, v in read_opts.items() if v is not None})
    df.columns = df.columns.str.strip().str.replace("\ufeff", "", regex=False)

    pk_col = cfg["primary_key"]
    if not pk_col or pk_col not in df.columns:
        raise ValueError(f"Columna primaria '{pk_col}' no encontrada.")

    # --- 3.3  URIs base y prefijos ----------------------------------------- #
    base = cfg["base_uri"].rstrip("/") + "/"
    entity_base = cfg["entity_base_uri"].rstrip("/") + "/"

    prefixes = dict(DEFAULT_CFG["prefixes"])
    prefixes.update(cfg["prefixes"])
    prefixes.update({k: base + v for k, v in NS_INTERNAL.items()})
    ns = {k: Namespace(v) for k, v in prefixes.items()}

    g = Graph()
    for pre, uri in prefixes.items():
        g.bind(pre, uri)

    # Índice de PK → URI
    pk_index = {
        str(row[pk_col]).strip(): pk_to_uri(str(row[pk_col]).strip(), base)
        for _, row in df.fillna("").iterrows()
        if str(row[pk_col]).strip()
    }

    # --- 3.4  Catálogos SKOS ---------------------------------------------- #
    for col in cfg["catalogs"]:
        scheme_uri = ns["col"][safe(col)]
        g.add((scheme_uri, RDF.type, SKOS.ConceptScheme))
        g.add((scheme_uri, RDFS.label, Literal(col, lang=cfg["lang"])))
        for raw_v in df[col].dropna().unique():
            v = str(raw_v).strip()
            if v:
                c_uri = ns["val"][f"{safe(col)}/{safe(v)}"]
                g.add((c_uri, RDF.type, SKOS.Concept))
                g.add((c_uri, SKOS.prefLabel, Literal(v, lang=cfg["lang"])))
                g.add((c_uri, SKOS.inScheme, scheme_uri))

    # --- 3.5  Recorremos filas --------------------------------------------- #
    rel_buffer: List[Tuple[URIRef, URIRef, str]] = []

    for _, row in df.fillna("").iterrows():
        pk_val = str(row[pk_col]).strip()
        if not pk_val:
            continue
        subj = pk_index[pk_val]            # recurso fila

        # Tipado por defecto
        for cl in cfg["classes"]:
            g.add((subj, RDF.type, URIRef(expand(cl, prefixes))))

        # Columnas…
        for col, cell in row.items():
            if col == pk_col or cell == "" or pd.isna(cell):
                continue
            col_sep = cfg["multivalued"].get(col, cfg["separator"])

            # ---------------- 3.5.1  entity_templates --------------------- #
            if col in cfg["entity_templates"]:
                spec = cfg["entity_templates"][col]

                # --  fuente de valores
                src_col = spec.get("source_column", col)
                src_sep = spec.get("separator", col_sep)
                values = split_cell(row.get(src_col, ""), src_sep)

                # --  fuente opcional de IDs para {id}
                id_values: List[str] = []
                if "id_source" in spec and "from_column" in spec["id_source"]:
                    id_col = spec["id_source"]["from_column"]
                    id_sep = cfg["multivalued"].get(id_col, col_sep)
                    id_values = split_cell(row.get(id_col, ""), id_sep)

                pred = URIRef(expand(spec["link_predicate"], prefixes))
                inv_pred_curie = spec.get("inverse_predicate")
                inv_pred = URIRef(expand(inv_pred_curie, prefixes)) if inv_pred_curie else None

                for idx, v in enumerate(values):
                    # -- construir path con {value}/{safe_value}/{id}
                    path_tmpl = spec.get("path", "{safe_value}")
                    path = path_tmpl.format(
                        value=v,
                        safe_value=safe(v),
                        id=(id_values[idx].strip() if idx < len(id_values) and id_values[idx].strip() else safe(v))
                    )
                    obj_uri = URIRef(entity_base + path)

                    # -- tipos del hijo
                    for t in spec.get("types", []):
                        g.add((obj_uri, RDF.type, URIRef(expand(t, prefixes))))

                    # -- literales del hijo
                    for lit_pred, mode in spec.get("literals", {}).items():
                        lit_uri = URIRef(expand(lit_pred, prefixes))
                        if isinstance(mode, str):          # "raw" | "safe"
                            lit_val = v if mode == "raw" else safe(v)
                            g.add((obj_uri, lit_uri, Literal(lit_val, lang=cfg["lang"])))
                        elif isinstance(mode, dict) and "from_column" in mode:
                            from_col = mode["from_column"]
                            from_raw = row.get(from_col, "")
                            if mode.get("match_by_index"):
                                aux_values = split_cell(from_raw, cfg["multivalued"].get(from_col, col_sep))
                                if idx < len(aux_values):
                                    lit_val = aux_values[idx].strip()
                                    if lit_val:
                                        g.add((obj_uri, lit_uri, Literal(lit_val, lang=cfg["lang"])))
                            else:
                                lit_val = from_raw.strip()
                                if lit_val:
                                    g.add((obj_uri, lit_uri, Literal(lit_val, lang=cfg["lang"])))

                    # -- enlaces principal ↔ hijo
                    g.add((subj, pred, obj_uri))
                    if inv_pred:
                        g.add((obj_uri, inv_pred, subj))
                continue  # siguiente columna

            # ---------------- 3.5.2  catalogs ----------------------------- #
            if col in cfg["catalogs"]:
                p_uri = URIRef(expand(cfg["property_map"].get(col), prefixes)) \
                        if col in cfg["property_map"] else ns["col"][safe(col)]
                for v in split_cell(cell, col_sep):
                    g.add((subj, p_uri, ns["val"][f"{safe(col)}/{safe(v)}"]))
                continue

            # ---------------- 3.5.3  relations ---------------------------- #
            rel = next((r for r in cfg["relations"] if r["from"] == col), None)
            if rel:
                pred = URIRef(expand(rel["predicate"], prefixes))
                for v in split_cell(cell, col_sep):
                    obj = pk_index.get(v.strip())
                    if obj:
                        g.add((subj, pred, obj))
                    else:
                        rel_buffer.append((subj, pred, v.strip()))
                continue

            # ---------------- 3.5.4  literal normal ----------------------- #
            p_curie = cfg["property_map"].get(col)
            p_uri = URIRef(expand(p_curie, prefixes)) if p_curie else ns["col"][safe(col)]
            for v in split_cell(cell, col_sep):
                dt_over = cfg["datatype_overrides"].get(col)
                dt = URIRef(expand(dt_over, prefixes)) if dt_over else infer_datatype(v)
                g.add((subj, p_uri, Literal(v, datatype=dt)))

    # --- 3.6  Resolver enlaces diferidos ---------------------------------- #
    for subj, pred, key in rel_buffer:
        obj = pk_index.get(key)
        if obj:
            g.add((subj, pred, obj))

    # --- 3.7  Serializar --------------------------------------------------- #
    fmt = SUPPORTED_FORMATS.get(cfg["format"].lower())
    if fmt is None:
        raise ValueError(f"Formato '{cfg['format']}' no soportado.")
    if to_stdout:
        g.serialize(sys.stdout.buffer, format=fmt)
    else:
        if not out_path:
            ext = "ttl" if fmt == "turtle" else fmt
            out_path = os.path.splitext(csv_path)[0] + f".{ext}"
        g.serialize(out_path, format=fmt)
        log.info(f"RDF generado → {out_path}")

# --------------------------------------------------------------------------- #
# 4.  CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="CSV → RDF")
    ap.add_argument("csv", help="Archivo CSV de entrada")
    ap.add_argument("-c", "--config", help="Archivo JSON de configuración")
    ap.add_argument("-o", "--output", help="Archivo de salida")
    ap.add_argument("--stdout", action="store_true", help="Serializar a stdout")
    args = ap.parse_args()

    try:
        csv_to_rdf(args.csv, args.config, args.output, args.stdout)
    except Exception as e:
        logging.error(e)
        sys.exit(1)

if __name__ == "__main__":
    main()
