from rdflib import Graph

# 1) cargar y contar triples
g = Graph().parse("articulos.ttl", format="turtle")
print(len(g), "triples")        # debe mostrar el total sin lanzar excepciones

# 2) consulta SPARQL sencilla: título y número de citas de cada artículo
q = """
PREFIX dct:    <http://purl.org/dc/terms/>
PREFIX schema: <https://schema.org/>

SELECT ?title ?year ?cites
WHERE {
  ?art a <http://purl.org/ontology/bibo/Article> ;
       dct:title ?title ;
       dct:issued ?year ;
       schema:citationCount ?cites .
}
ORDER BY DESC(?cites)
"""

for row in g.query(q):
    print(f"{row.title[:40]:40}  {row.year}  citas:{row.cites}")
