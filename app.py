# app.py ───────────────────────────────────────────────────────────────────
import streamlit as st
import pandas as pd
import json, io, re, subprocess, tempfile, os, sys
from collections import Counter
from pathlib import Path

############################################################################
# Configuración general de la página
############################################################################
st.set_page_config("CSV ➜ RDF wizard", "🔄", layout="wide")

############################################################################
# Helpers
############################################################################
SEP_CANDIDATES = [",", ";", "\t", "|"]

# Prefijos sugeridos ↔ propiedades habituales
AUTO_PROP_SUGGEST = {
    "title": "dct:title",
    "nombre": "foaf:name",
    "name": "foaf:name",
    "doi": "bibo:doi",
    "abstract": "dct:abstract",
    "keywords": "dct:subject",
    "year": "dct:issued",
    "cited": "schema:citationCount",
    "link": "schema:url",
    "authors": "dct:creator",
    "author": "dct:creator",
}

XSD_MAP = {
    "integer": "xsd:integer",
    "decimal": "xsd:decimal",
    "boolean": "xsd:boolean",
    "date": "xsd:date",
    "dateTime": "xsd:dateTime",
    "gYear": "xsd:gYear",
}

# Tipos de entidad comunes
COMMON_TYPES = [
    "foaf:Person",
    "foaf:Organization", 
    "bibo:Article",
    "bibo:Book",
    "bibo:Document",
    "schema:Person",
    "schema:Organization",
    "schema:Article",
    "skos:Concept",
    "dct:Agent"
]

# Predicados de enlace comunes
COMMON_LINK_PREDICATES = [
    "dct:creator",
    "dct:contributor", 
    "dct:publisher",
    "foaf:maker",
    "schema:author",
    "schema:editor",
    "skos:related",
    "rdfs:seeAlso"
]

def sniff_delimiter(sample: str) -> str:
    """Devuelve el separador de columnas más probable."""
    best, cols = ",", 0
    for sep in SEP_CANDIDATES:
        c = len(sample.split("\n")[0].split(sep))
        if c > cols:
            best, cols = sep, c
    return best

def infer_cell_type(s: str) -> str:
    s = s.strip()
    if re.fullmatch(r"-?\d+", s):
        return "integer"
    if re.fullmatch(r"-?\d+\.\d+", s):
        return "decimal"
    if s.lower() in ("true", "false"):
        return "boolean"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return "date"
    if re.fullmatch(r"\d{4}", s):
        return "gYear"
    return "string"

def infer_column_types(df: pd.DataFrame) -> dict:
    """Heurística sobre las primeras 100 filas."""
    dtype_overrides = {}
    for col in df.columns:
        sample = df[col].dropna().astype(str).head(100)
        types = Counter(infer_cell_type(x) for x in sample)
        if not types:
            continue
        main, freq = types.most_common(1)[0]
        if main != "string" and freq / len(sample) > 0.8:
            dtype_overrides[col] = XSD_MAP[main]
    return dtype_overrides

def detect_multivalues(df: pd.DataFrame, default_sep=";") -> dict:
    mv = {}
    for col in df.columns:
        sample = df[col].astype(str).head(50)
        if any(default_sep in x for x in sample):
            mv[col] = default_sep
    return mv

def suggest_primary_key(cols) -> str:
    for opt in ["id", "ID", "Id", "eid", "EID", "link", "Link", "url", "URL"]:
        if opt in cols:
            return opt
    return ""

def render_template_editor(template_name, template_data, available_columns):
    """Renderiza el editor de plantillas para una plantilla específica."""
    st.markdown(f"### 📝 Plantilla: {template_name}")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### Configuración básica")
        source_col = st.selectbox(
            "Columna fuente", 
            options=available_columns,
            index=available_columns.index(template_data.get("source_column", template_name)) if template_data.get("source_column", template_name) in available_columns else 0,
            key=f"source_{template_name}"
        )
        
        separator = st.text_input(
            "Separador",
            value=template_data.get("separator", ";"),
            key=f"sep_{template_name}"
        )
        
        path_template = st.text_input(
            "Plantilla de ruta",
            value=template_data.get("path", "{safe_value}"),
            help="Usa {value} para el valor original, {safe_value} para el valor seguro",
            key=f"path_{template_name}"
        )
        
        link_predicate = st.selectbox(
            "Predicado de enlace",
            options=COMMON_LINK_PREDICATES,
            index=COMMON_LINK_PREDICATES.index(template_data.get("link_predicate", "dct:creator")) if template_data.get("link_predicate", "dct:creator") in COMMON_LINK_PREDICATES else 0,
            key=f"link_{template_name}"
        )
    
    with col2:
        st.markdown("#### Tipos de entidad")
        current_types = template_data.get("types", [])
        
        # Selector de tipos comunes
        selected_common = st.multiselect(
            "Tipos comunes",
            options=COMMON_TYPES,
            default=[t for t in current_types if t in COMMON_TYPES],
            key=f"common_types_{template_name}"
        )
        
        # Campo para tipos personalizados
        custom_types_str = st.text_area(
            "Tipos personalizados (uno por línea)",
            value="\n".join([t for t in current_types if t not in COMMON_TYPES]),
            key=f"custom_types_{template_name}"
        )
        
        custom_types = [t.strip() for t in custom_types_str.split("\n") if t.strip()]
        all_types = selected_common + custom_types
    
    # Editor de literales
    st.markdown("#### Propiedades literales")
    
    current_literals = template_data.get("literals", {})
    
    # Mostrar literales existentes
    literals_to_remove = []
    updated_literals = {}
    
    for lit_pred, lit_config in current_literals.items():
        with st.expander(f"🏷️ {lit_pred}", expanded=True):
            col_a, col_b, col_c = st.columns([3, 3, 1])
            
            with col_a:
                new_pred = st.text_input(
                    "Predicado",
                    value=lit_pred,
                    key=f"lit_pred_{template_name}_{lit_pred}"
                )
            
            with col_b:
                if isinstance(lit_config, str):
                    # Literal simple
                    mode = st.selectbox(
                        "Modo",
                        options=["raw", "safe"],
                        index=0 if lit_config == "raw" else 1,
                        key=f"lit_mode_{template_name}_{lit_pred}"
                    )
                    new_config = mode
                else:
                    # Literal desde otra columna
                    st.markdown("**Desde otra columna:**")
                    from_col = st.selectbox(
                        "Columna",
                        options=available_columns,
                        index=available_columns.index(lit_config.get("from_column", "")) if lit_config.get("from_column", "") in available_columns else 0,
                        key=f"lit_from_col_{template_name}_{lit_pred}"
                    )
                    
                    match_by_index = st.checkbox(
                        "Emparejar por índice",
                        value=lit_config.get("match_by_index", False),
                        key=f"lit_match_{template_name}_{lit_pred}"
                    )
                    
                    new_config = {
                        "from_column": from_col,
                        "match_by_index": match_by_index
                    }
            
            with col_c:
                if st.button("🗑️", key=f"del_lit_{template_name}_{lit_pred}"):
                    literals_to_remove.append(lit_pred)
                else:
                    updated_literals[new_pred] = new_config
    
    # Agregar nueva propiedad literal
    st.markdown("##### ➕ Agregar nueva propiedad literal")
    col_x, col_y, col_z = st.columns([3, 2, 2])
    
    with col_x:
        new_lit_pred = st.text_input(
            "Nuevo predicado",
            key=f"new_lit_pred_{template_name}"
        )
    
    with col_y:
        new_lit_type = st.selectbox(
            "Tipo",
            options=["Valor directo", "Desde otra columna"],
            key=f"new_lit_type_{template_name}"
        )
    
    with col_z:
        if new_lit_type == "Valor directo":
            new_lit_mode = st.selectbox(
                "Modo",
                options=["raw", "safe"],
                key=f"new_lit_mode_{template_name}"
            )
        else:
            new_lit_from_col = st.selectbox(
                "Columna",
                options=[""] + available_columns,
                key=f"new_lit_from_col_{template_name}"
            )
    
    if st.button(f"➕ Agregar propiedad", key=f"add_lit_{template_name}") and new_lit_pred:
        if new_lit_type == "Valor directo":
            updated_literals[new_lit_pred] = new_lit_mode
        else:
            if new_lit_from_col:
                match_by_index = st.session_state.get(f"new_lit_match_{template_name}", False)
                updated_literals[new_lit_pred] = {
                    "from_column": new_lit_from_col,
                    "match_by_index": match_by_index
                }
        st.experimental_rerun()
    
    # Construir la plantilla actualizada
    updated_template = {
        "source_column": source_col,
        "separator": separator,
        "path": path_template,
        "types": all_types,
        "link_predicate": link_predicate,
        "literals": updated_literals
    }
    
    return updated_template

############################################################################
# Estado de sesión
############################################################################
if "config" not in st.session_state:
    st.session_state.config = {}
if "csv_columns" not in st.session_state:
    st.session_state.csv_columns = []
if "csv_df" not in st.session_state:
    st.session_state.csv_df = None
if "rdf_bytes" not in st.session_state:
    st.session_state.rdf_bytes = b""
if "selected_template" not in st.session_state:
    st.session_state.selected_template = None

DEFAULT_CFG = {
    "base_uri": "http://universidad-ec.edu.ec/resource/",
    "entity_base_uri": "http://universidad-ec.edu.ec/",
    "primary_key": "",
    "format": "turtle",
    "csv_delimiter": ",",
    "separator": ";",
    "multivalued": {},
    "prefixes": {
        "bibo": "http://purl.org/ontology/bibo/",
        "dct":  "http://purl.org/dc/terms/",
        "foaf": "http://xmlns.com/foaf/0.1/",
        "schema": "https://schema.org/",
        "skos":   "http://www.w3.org/2004/02/skos/core#",
        "rdfs":   "http://www.w3.org/2000/01/rdf-schema#",
        "xsd":    "http://www.w3.org/2001/XMLSchema#",
    },
    "entity_templates": {},
    "property_map": {},
    "catalogs": [],
    "classes": "bibo:Article",
    "datatype_overrides": {},
}

############################################################################
# Sidebar – Carga de archivos
############################################################################
with st.sidebar:
    st.header("📁 Archivos")
    up_csv = st.file_uploader("Sube un CSV", type="csv")
    up_cfg = st.file_uploader("Cargar configuración JSON", type="json")

    if st.button("🔄 Resetear"):
        st.session_state.clear()
        st.experimental_rerun()

############################################################################
# Procesamiento de CSV
############################################################################
if up_csv:
    sample = str(up_csv.read(2048), "utf-8")
    up_csv.seek(0)
    delim = sniff_delimiter(sample)

    df = pd.read_csv(up_csv, sep=delim, dtype=str)
    df.columns = df.columns.str.strip()
    st.session_state.csv_columns = df.columns.tolist()
    st.session_state.csv_df = df.head(100)  # vista previa
    # Inicializamos config si aún no
    if not st.session_state.config:
        cfg = {**DEFAULT_CFG}
        cfg["csv_delimiter"] = delim
        cfg["primary_key"]   = suggest_primary_key(df.columns)
        cfg["multivalued"]   = detect_multivalues(df)
        cfg["datatype_overrides"] = infer_column_types(df)
        # property_map auto
        for col in df.columns:
            key = col.lower().replace(" ", "")
            if key in AUTO_PROP_SUGGEST:
                cfg["property_map"][col] = AUTO_PROP_SUGGEST[key]
        st.session_state.config = cfg

if up_cfg:
    try:
        cfg_data = json.load(up_cfg)
        if st.session_state.config:
            st.session_state.config.update(cfg_data)
        else:
            st.session_state.config = cfg_data
        st.success("Configuración cargada")
    except Exception as e:
        st.error(e)

cfg = st.session_state.config if st.session_state.config else DEFAULT_CFG

############################################################################
# UI – Pestañas de configuración
############################################################################
tabs = st.tabs(["📋 Básica", "🔀 Multivalor", "🏷️ Prefijos",
                "🔗 Propiedades", "📚 Catálogos", "🔢 Tipos", "👥 Plantillas",
                "🧐 Vista RDF", "🕸️ Grafo RDF"])

# 1. Básica ────────────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("Configuración básica")
    col1, col2 = st.columns(2)
    with col1:
        cfg["base_uri"] = st.text_input("Base URI", cfg.get("base_uri", ""))
        cfg["entity_base_uri"] = st.text_input("Entity Base URI", cfg.get("entity_base_uri", ""))
        cfg["primary_key"] = st.selectbox("Clave primaria", [""]+st.session_state.csv_columns,
                                          index=([""]+st.session_state.csv_columns).index(cfg.get("primary_key", "")) if cfg.get("primary_key", "") in [""]+st.session_state.csv_columns else 0)
    with col2:
        cfg["format"] = st.selectbox("Formato", ["turtle","xml","json-ld","nt"], index=["turtle","xml","json-ld","nt"].index(cfg.get("format", "turtle")))
        cfg["csv_delimiter"] = st.text_input("Delimitador CSV", cfg.get("csv_delimiter", ","))
        cfg["separator"] = st.text_input("Separador por defecto", cfg.get("separator", ";"))
        cfg["classes"] = st.text_input("Clases", cfg.get("classes", ""))

# 2. Multivalor ────────────────────────────────────────────────────────────
with tabs[1]:
    st.subheader("Columnas multivalor")
    st.markdown("Especifica qué separador usar para dividir valores múltiples en cada columna.")
    
    for col in st.session_state.csv_columns:
        mv = cfg.get("multivalued", {}).get(col, "")
        new = st.text_input(f"**{col}**", mv, key=f"mv_{col}", help=f"Separador para dividir múltiples valores en '{col}' (ej: ';', ',', '|')")
        if new:
            if "multivalued" not in cfg:
                cfg["multivalued"] = {}
            cfg["multivalued"][col] = new
        elif col in cfg.get("multivalued", {}):
            del cfg["multivalued"][col]

# 3. Prefijos ──────────────────────────────────────────────────────────────
with tabs[2]:
    st.subheader("Prefijos y namespaces")
    st.markdown("Define los prefijos para los vocabularios RDF que utilizarás.")
    
    if "prefixes" not in cfg:
        cfg["prefixes"] = {}
    
    to_del = []
    for p, uri in cfg["prefixes"].items():
        c1,c2,c3 = st.columns([2,5,1])
        p_new = c1.text_input("Prefijo", p, key=f"pref_{p}")
        u_new = c2.text_input("URI", uri, key=f"uri_{p}")
        if c3.button("🗑️", key=f"delpref_{p}"):
            to_del.append(p)
        if (p_new, u_new) != (p, uri):
            del cfg["prefixes"][p]
            if p_new and u_new:
                cfg["prefixes"][p_new] = u_new
    for p in to_del: 
        cfg["prefixes"].pop(p, None)
    
    st.markdown("---")
    st.markdown("##### ➕ Agregar nuevo prefijo")
    col_a, col_b, col_c = st.columns([2, 5, 1])
    with col_a:
        new_p = st.text_input("Prefijo", key="n_pref")
    with col_b:
        new_u = st.text_input("URI", key="n_uri")
    with col_c:
        if st.button("➕") and new_p and new_u:
            cfg["prefixes"][new_p] = new_u
            st.experimental_rerun()

# 4. Propiedades ───────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("Mapeo de propiedades")
    st.markdown("Asigna propiedades RDF específicas a cada columna de tu CSV.")
    
    if "property_map" not in cfg:
        cfg["property_map"] = {}
    
    for col in st.session_state.csv_columns:
        prop = cfg["property_map"].get(col, "")
        new = st.text_input(f"**{col}**", prop, key=f"pm_{col}", 
                           help=f"Propiedad RDF para '{col}' (ej: dct:title, foaf:name)")
        if new:
            cfg["property_map"][col] = new
        elif col in cfg["property_map"]:
            del cfg["property_map"][col]

# 5. Catálogos SKOS ────────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("Catálogos SKOS")
    st.markdown("Convierte columnas específicas en conceptos SKOS para crear vocabularios controlados.")
    
    sel = st.multiselect("Columnas para convertir en catálogos SKOS", 
                         options=st.session_state.csv_columns,
                         default=cfg.get("catalogs", []))
    cfg["catalogs"] = sel

# 6. Datatype overrides ────────────────────────────────────────────────────
with tabs[5]:
    st.subheader("Tipos de dato específicos")
    st.markdown("Fuerza tipos de datos específicos para columnas particulares.")
    
    if "datatype_overrides" not in cfg:
        cfg["datatype_overrides"] = {}
    
    dtypes = list(XSD_MAP.values()) + ["xsd:string"]
    for col in st.session_state.csv_columns:
        cur = cfg["datatype_overrides"].get(col, "")
        new = st.selectbox(f"**{col}**", [""]+dtypes,
                           index=([""]+dtypes).index(cur) if cur in [""]+dtypes else 0,
                           key=f"dtype_{col}")
        if new:
            cfg["datatype_overrides"][col] = new
        elif col in cfg["datatype_overrides"]:
            del cfg["datatype_overrides"][col]

# 7. Entity templates ──────────────────────────────────────────────────────
with tabs[6]:
    st.subheader("Plantillas de entidades")
    st.markdown("""
    Las plantillas de entidades te permiten crear entidades RDF complejas a partir de valores de columnas.
    Por ejemplo, convertir una lista de autores en entidades `foaf:Person` individuales.
    """)
    
    if "entity_templates" not in cfg:
        cfg["entity_templates"] = {}
    
    # Selector de plantilla actual
    col_select, col_actions = st.columns([3, 1])
    
    with col_select:
        template_options = ["Nueva plantilla..."] + list(cfg["entity_templates"].keys())
        selected_template = st.selectbox(
            "Seleccionar plantilla",
            options=template_options,
            index=template_options.index(st.session_state.selected_template) if st.session_state.selected_template in template_options else 0
        )
    
    with col_actions:
        st.markdown("<br>", unsafe_allow_html=True)  # Espaciado
        if st.button("🗑️ Eliminar") and selected_template != "Nueva plantilla..." and selected_template in cfg["entity_templates"]:
            del cfg["entity_templates"][selected_template]
            st.session_state.selected_template = None
            st.experimental_rerun()
    
    if selected_template == "Nueva plantilla...":
        # Crear nueva plantilla
        st.markdown("#### ➕ Crear nueva plantilla")
        new_template_name = st.text_input("Nombre de la plantilla", 
                                         help="Normalmente el nombre de la columna que contiene los datos")
        
        if st.button("Crear plantilla") and new_template_name:
            if new_template_name not in cfg["entity_templates"]:
                cfg["entity_templates"][new_template_name] = {
                    "source_column": new_template_name,
                    "separator": ";",
                    "path": "{safe_value}",
                    "types": ["foaf:Person"],
                    "link_predicate": "dct:creator",
                    "literals": {
                        "foaf:name": "raw",
                        "rdfs:label": "safe"
                    }
                }
                st.session_state.selected_template = new_template_name
                st.experimental_rerun()
            else:
                st.error("Ya existe una plantilla con ese nombre")
    
    elif selected_template in cfg["entity_templates"]:
        # Editar plantilla existente
        st.session_state.selected_template = selected_template
        updated_template = render_template_editor(
            selected_template, 
            cfg["entity_templates"][selected_template], 
            st.session_state.csv_columns
        )
        cfg["entity_templates"][selected_template] = updated_template
    
    # Mostrar JSON de todas las plantillas
    if cfg["entity_templates"]:
        with st.expander("📋 Ver JSON de plantillas"):
            st.json(cfg["entity_templates"])

# 8. Visualización del Grafo RDF ──────────────────────────────────────────
with tabs[8]:
    st.subheader("🕸️ Visualización del Grafo RDF")
    st.markdown("Explora visualmente el grafo RDF generado con nodos y relaciones interactivas.")
    
    if st.session_state.rdf_bytes:
        try:
            # Parsear el RDF generado
            from rdflib import Graph as RDFGraph
            import networkx as nx
            from pyvis.network import Network
            import tempfile
            import base64
            
            # Crear un grafo RDF desde los bytes generados
            rdf_graph = RDFGraph()
            rdf_format = "turtle" if cfg["format"] == "turtle" else cfg["format"]
            if cfg["format"] == "xml":
                rdf_format = "xml"
            elif cfg["format"] == "json-ld":
                rdf_format = "json-ld"
            elif cfg["format"] == "nt":
                rdf_format = "nt"
            
            rdf_graph.parse(data=st.session_state.rdf_bytes.decode('utf-8'), format=rdf_format)
            
            # Opciones de visualización
            st.markdown("#### ⚙️ Opciones de visualización")
            col_opt1, col_opt2, col_opt3 = st.columns(3)
            
            with col_opt1:
                max_nodes = st.slider("Máximo de nodos", 10, 200, 50, 
                                    help="Limita el número de nodos para mejor rendimiento")
                
            with col_opt2:
                layout_algorithm = st.selectbox("Algoritmo de layout", 
                                               ["spring", "hierarchical", "random", "circular"],
                                               help="Algoritmo para posicionar los nodos")
                
            with col_opt3:
                show_literals = st.checkbox("Mostrar literales", value=False,
                                          help="Incluir nodos de valores literales (puede hacer el grafo muy denso)")
            
            # Filtros adicionales
            with st.expander("🔍 Filtros avanzados"):
                filter_col1, filter_col2 = st.columns(2)
                
                with filter_col1:
                    # Filtrar por tipos de nodos
                    node_types = set()
                    for s, p, o in rdf_graph:
                        if str(p) == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type":
                            node_types.add(str(o))
                    
                    selected_types = st.multiselect("Tipos de entidades a mostrar", 
                                                   list(node_types), 
                                                   default=list(node_types)[:5])
                
                with filter_col2:
                    # Filtrar por predicados
                    predicates = set(str(p) for s, p, o in rdf_graph)
                    selected_predicates = st.multiselect("Predicados a mostrar",
                                                        list(predicates)[:10],
                                                        default=list(predicates)[:5])
            
            if st.button("🎨 Generar visualización del grafo", type="primary"):
                with st.spinner("Generando visualización del grafo..."):
                    try:
                        # Crear grafo NetworkX
                        nx_graph = nx.DiGraph()
                        
                        # Contadores para limitar nodos
                        node_count = 0
                        added_nodes = set()
                        
                        # Función para truncar URIs largas
                        def truncate_uri(uri_str, max_length=30):
                            if len(uri_str) <= max_length:
                                return uri_str
                            # Intentar mostrar solo la parte final de la URI
                            if "/" in uri_str:
                                parts = uri_str.split("/")
                                return ".../" + parts[-1]
                            elif "#" in uri_str:
                                parts = uri_str.split("#")
                                return "...#" + parts[-1]
                            else:
                                return uri_str[:max_length-3] + "..."
                        
                        # Función para determinar el color del nodo
                        def get_node_color(node_uri, rdf_graph):
                            # Verificar el tipo de la entidad
                            for s, p, o in rdf_graph:
                                if str(s) == node_uri and str(p) == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type":
                                    type_uri = str(o)
                                    if "Person" in type_uri:
                                        return "#FF6B6B"  # Rojo para personas
                                    elif "Article" in type_uri or "Document" in type_uri:
                                        return "#4ECDC4"  # Verde para documentos
                                    elif "Organization" in type_uri:
                                        return "#45B7D1"  # Azul para organizaciones
                                    elif "Concept" in type_uri:
                                        return "#FFA07A"  # Naranja para conceptos
                            return "#DDD"  # Gris por defecto
                        
                        # Añadir nodos y aristas
                        for s, p, o in rdf_graph:
                            if node_count >= max_nodes:
                                break
                                
                            subj_str = str(s)
                            pred_str = str(p)
                            obj_str = str(o)
                            
                            # Filtrar por tipos seleccionados
                            if selected_types and pred_str == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type":
                                if obj_str not in selected_types:
                                    continue
                            
                            # Filtrar por predicados seleccionados
                            if selected_predicates and pred_str not in selected_predicates:
                                continue
                            
                            # Filtrar literales si no se desean mostrar
                            if not show_literals and not obj_str.startswith("http"):
                                continue
                            
                            # Añadir nodo sujeto
                            if subj_str not in added_nodes:
                                nx_graph.add_node(subj_str, 
                                                 label=truncate_uri(subj_str),
                                                 title=subj_str,
                                                 color=get_node_color(subj_str, rdf_graph))
                                added_nodes.add(subj_str)
                                node_count += 1
                            
                            # Añadir nodo objeto (si no es literal o si se muestran literales)
                            if obj_str.startswith("http") or show_literals:
                                if obj_str not in added_nodes and node_count < max_nodes:
                                    color = get_node_color(obj_str, rdf_graph) if obj_str.startswith("http") else "#F0E68C"
                                    nx_graph.add_node(obj_str,
                                                     label=truncate_uri(obj_str),
                                                     title=obj_str,
                                                     color=color)
                                    added_nodes.add(obj_str)
                                    node_count += 1
                                
                                # Añadir arista
                                edge_label = truncate_uri(pred_str, 20)
                                nx_graph.add_edge(subj_str, obj_str, 
                                                 label=edge_label,
                                                 title=pred_str)
                        
                        # Crear visualización con pyvis
                        net = Network(height="600px", width="100%", bgcolor="#ffffff", font_color="black")
                        
                        # Configurar física del grafo
                        if layout_algorithm == "spring":
                            net.barnes_hut()
                        elif layout_algorithm == "hierarchical":
                            net.set_options("""
                            {
                              "layout": {
                                "hierarchical": {
                                  "enabled": true,
                                  "direction": "UD"
                                }
                              }
                            }
                            """)
                        
                        # Convertir NetworkX a pyvis
                        net.from_nx(nx_graph)
                        
                        # Guardar y mostrar
                        with tempfile.NamedTemporaryFile(delete=False, suffix='.html') as tmp_file:
                            net.save_graph(tmp_file.name)
                            
                            # Leer el archivo HTML generado
                            with open(tmp_file.name, 'r', encoding='utf-8') as f:
                                html_content = f.read()
                            
                            # Mostrar el grafo
                            st.components.v1.html(html_content, height=650)
                            
                            # Limpiar archivo temporal
                            os.unlink(tmp_file.name)
                        
                        # Estadísticas del grafo
                        st.markdown("#### 📊 Estadísticas del grafo")
                        stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
                        
                        with stat_col1:
                            st.metric("🔗 Nodos", len(nx_graph.nodes))
                        with stat_col2:
                            st.metric("↔️ Aristas", len(nx_graph.edges))
                        with stat_col3:
                            st.metric("📊 Densidad", f"{nx.density(nx_graph):.3f}")
                        with stat_col4:
                            if len(nx_graph.nodes) > 0:
                                avg_degree = sum(dict(nx_graph.degree()).values()) / len(nx_graph.nodes)
                                st.metric("📈 Grado promedio", f"{avg_degree:.1f}")
                        
                        # Leyenda de colores
                        st.markdown("#### 🎨 Leyenda de colores")
                        legend_col1, legend_col2 = st.columns(2)
                        
                        with legend_col1:
                            st.markdown("""
                            - 🔴 **Personas** (foaf:Person)
                            - 🟢 **Documentos/Artículos** (bibo:Article)
                            - 🔵 **Organizaciones** (foaf:Organization)
                            """)
                        with legend_col2:
                            st.markdown("""
                            - 🟠 **Conceptos** (skos:Concept)
                            - 🟡 **Literales** (valores de texto)
                            - ⚪ **Otros tipos**
                            """)
                            
                    except ImportError:
                        st.error("""
                        **Bibliotecas requeridas no encontradas**
                        
                        Para usar la visualización de grafos, necesitas instalar:
                        ```bash
                        pip install networkx pyvis rdflib
                        ```
                        """)
                    except Exception as e:
                        st.error(f"Error al generar la visualización: {str(e)}")
                        st.info("Asegúrate de que el RDF generado sea válido y prueba con un dataset más pequeño.")
        
        except Exception as e:
            st.error(f"Error al procesar el RDF: {str(e)}")
    
    else:
        st.info("💡 Primero genera el RDF en la pestaña 'Vista RDF' para poder visualizar el grafo.")
        st.markdown("""
        **¿Qué podrás ver en la visualización del grafo?**
        
        - 🔗 **Nodos interactivos** representando entidades y conceptos
        - ↔️ **Aristas etiquetadas** mostrando las relaciones entre entidades  
        - 🎨 **Colores diferenciados** por tipo de entidad (personas, documentos, conceptos)
        - 🔍 **Zoom y navegación** para explorar grafos grandes
        - 📊 **Estadísticas del grafo** (densidad, grado promedio, etc.)
        - ⚙️ **Filtros configurables** para enfocar aspectos específicos
        
        **Algoritmos de layout disponibles:**
        - **Spring**: Distribución natural basada en fuerzas
        - **Hierarchical**: Organización jerárquica top-down  
        - **Random**: Distribución aleatoria
        - **Circular**: Disposición en círculo
        """)

############################################################################
# Vista previa de RDF dentro de Streamlit
############################################################################
with tabs[7]:
    st.subheader("Generar y descargar RDF")
    
    # Validación
    validation_errors = []
    if not cfg.get("primary_key"):
        validation_errors.append("Debes seleccionar una 'Clave primaria'")
    if st.session_state.csv_df is None:
        validation_errors.append("Debes subir un archivo CSV")
    
    if validation_errors:
        for error in validation_errors:
            st.warning(error)
    else:
        # Botón para ejecutar conversión
        if st.button("🚀 Generar RDF", type="primary"):
            with st.spinner("Ejecutando conversión…"):
                try:
                    # Guardamos CSV y config temporales
                    with tempfile.TemporaryDirectory() as tmp:
                        csv_path = Path(tmp)/"data.csv"
                        cfg_path = Path(tmp)/"cfg.json"
                        out_path = Path(tmp)/"out.rdf"
                        
                        # Preparar el DataFrame completo (no solo la vista previa)
                        # Re-leer el archivo completo
                        up_csv.seek(0)
                        full_df = pd.read_csv(up_csv, sep=cfg["csv_delimiter"], dtype=str)
                        full_df.columns = full_df.columns.str.strip()
                        
                        full_df.to_csv(csv_path, index=False, sep=cfg["csv_delimiter"])
                        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                        
                        # Llamamos al script
                        cmd = [sys.executable, "csv2rdf.py", str(csv_path), "-c", str(cfg_path), "-o", str(out_path)]
                        res = subprocess.run(cmd, capture_output=True, text=True)
                        
                        if res.returncode != 0:
                            st.error(f"Error en la conversión: {res.stderr}")
                        else:
                            data = out_path.read_bytes()
                            st.session_state.rdf_bytes = data
                            st.success(f"✅ RDF generado exitosamente ({len(data):,} bytes)")
                            
                except Exception as e:
                    st.error(f"Error durante la conversión: {str(e)}")

        if st.session_state.rdf_bytes:
            # Botón de descarga
            file_ext = "ttl" if cfg["format"] == "turtle" else cfg["format"]
            st.download_button(
                "💾 Descargar archivo RDF",
                data=st.session_state.rdf_bytes,
                file_name=f"output.{file_ext}",
                mime="application/xml" if cfg["format"] == "xml" else "text/plain",
                type="primary"
            )
            
            # Vista previa
            st.markdown("#### Vista previa del RDF generado")
            try:
                preview_text = st.session_state.rdf_bytes[:3000].decode("utf-8", "ignore")
                st.code(preview_text, language="turtle" if cfg["format"] == "turtle" else "xml")
            except Exception:
                st.text("No se puede mostrar la vista previa del contenido")

############################################################################
# Panel de configuración final
############################################################################
st.sidebar.markdown("---")
st.sidebar.markdown("### 📥 Configuración")
cfg_json = json.dumps(cfg, indent=2, ensure_ascii=False)
st.sidebar.download_button(
    "Descargar config.json", 
    cfg_json, 
    "config.json", 
    "application/json"
)

# Mostrar resumen de configuración
with st.sidebar.expander("📊 Resumen de configuración"):
    st.markdown(f"**Primary Key:** {cfg.get('primary_key', 'No definida')}")
    st.markdown(f"**Formato:** {cfg.get('format', 'turtle')}")
    st.markdown(f"**Plantillas:** {len(cfg.get('entity_templates', {}))}")
    st.markdown(f"**Propiedades mapeadas:** {len(cfg.get('property_map', {}))}")
    st.markdown(f"**Catálogos SKOS:** {len(cfg.get('catalogs', []))}")

############################################################################
# Vista previa CSV (centrada abajo)
############################################################################
if st.session_state.csv_df is not None:
    st.markdown("---")
    st.markdown("### 📊 Vista previa del CSV")
    
    # Información del CSV
    total_rows = len(st.session_state.csv_df)
    total_cols = len(st.session_state.csv_df.columns)
    st.markdown(f"*Mostrando las primeras {total_rows} filas de un total de {total_cols} columnas*")
    
    # Mostrar estadísticas básicas
    col_stats1, col_stats2, col_stats3 = st.columns(3)
    with col_stats1:
        st.metric("📊 Filas", total_rows)
    with col_stats2:
        st.metric("📋 Columnas", total_cols)
    with col_stats3:
        # Contar valores no nulos
        non_null_count = st.session_state.csv_df.count().sum()
        total_cells = total_rows * total_cols
        completeness = (non_null_count / total_cells * 100) if total_cells > 0 else 0
        st.metric("✅ Completitud", f"{completeness:.1f}%")
    
    # DataFrame con búsqueda
    with st.expander("🔍 Opciones de visualización", expanded=False):
        col_filter1, col_filter2 = st.columns(2)
        with col_filter1:
            # Filtro de columnas
            selected_columns = st.multiselect(
                "Columnas a mostrar",
                options=st.session_state.csv_df.columns.tolist(),
                default=st.session_state.csv_df.columns.tolist()
            )
        with col_filter2:
            # Número de filas a mostrar
            max_rows = st.slider("Filas a mostrar", 5, 100, min(50, total_rows))
    
    # Mostrar el DataFrame filtrado
    if selected_columns:
        display_df = st.session_state.csv_df[selected_columns].head(max_rows)
        st.dataframe(display_df, use_container_width=True, height=400)
    else:
        st.info("Selecciona al menos una columna para mostrar")
    
    # Información adicional sobre las columnas
    with st.expander("📋 Información detallada de columnas"):
        for col in st.session_state.csv_df.columns:
            with st.container():
                col_info1, col_info2, col_info3 = st.columns([2, 1, 1])
                with col_info1:
                    st.write(f"**{col}**")
                with col_info2:
                    null_count = st.session_state.csv_df[col].isnull().sum()
                    st.write(f"Nulos: {null_count}")
                with col_info3:
                    unique_count = st.session_state.csv_df[col].nunique()
                    st.write(f"Únicos: {unique_count}")
                
                # Mostrar algunos valores de ejemplo
                sample_values = st.session_state.csv_df[col].dropna().head(3).tolist()
                if sample_values:
                    st.write(f"Ejemplos: {', '.join(str(v)[:50] + ('...' if len(str(v)) > 50 else '') for v in sample_values)}")
                st.markdown("---")

############################################################################
# Footer con información adicional
############################################################################
st.markdown("---")
st.markdown("""
### 📚 Ayuda y documentación

**¿Cómo usar esta herramienta?**
1. **Sube tu archivo CSV** usando el panel lateral
2. **Configura los parámetros básicos** como URI base y clave primaria
3. **Define mapeos de propiedades** para asignar propiedades RDF a tus columnas
4. **Crea plantillas de entidades** para generar entidades complejas (como personas, organizaciones)
5. **Genera y descarga** tu archivo RDF

**Formatos soportados:**
- 🐢 **Turtle** (.ttl) - Recomendado para legibilidad
- 📄 **RDF/XML** (.xml) - Estándar W3C
- 🔗 **JSON-LD** (.json) - Para aplicaciones web
- 📝 **N-Triples** (.nt) - Para procesamiento simple

**Tipos de entidad comunes:**
- `foaf:Person` - Personas
- `bibo:Article` - Artículos académicos
- `schema:Organization` - Organizaciones
- `skos:Concept` - Conceptos de vocabularios controlados
""")

# Footer final
st.markdown("""
<div style='text-align: center; color: #666; padding: 20px;'>
    <hr>
    <p>CSV ➜ RDF Wizard | Herramienta para conversión de datos tabulares a RDF</p>
    <p><small>Desarrollado con Streamlit • Versión 2.0</small></p>
</div>
""", unsafe_allow_html=True)