"""
GD Computadoras - Comparador de precios de competencia

CÓMO FUNCIONA UNA BÚSQUEDA:
  1. El vendedor escribe lo que sea ("iphone 17 pro max", "router tp-link
     ax3000", "camara hikvision domo").
  2. Esa búsqueda se le pasa AL BUSCADOR DE CADA TIENDA, todas en paralelo.
     Cada una responde con sus candidatos (stores.py sabe cómo leer cada sitio).
  3. Los buscadores de las tiendas son flojos y devuelven cosas que no son lo
     buscado. Entonces la IA (ai_matcher.py) revisa la lista junta y deja solo
     lo que corresponde EXACTAMENTE al producto pedido.
  4. Se ordena por precio, más barato primero.

Todo el paso 2 es en vivo: no hay que mantener un catálogo al día. La única
excepción es Intelec, cuyo buscador está bloqueado por Cloudflare y se
consulta desde la copia local que arma build_catalog.py.

USO POR CONSOLA:
    python price_checker.py
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

from stores import TIENDAS, ProductResult

# Las tiendas responden en paralelo, así que una búsqueda tarda lo que tarda
# la tienda MÁS LENTA, no la suma de todas.
HILOS = len(TIENDAS)


def _sin_duplicados(resultados: List[ProductResult]) -> List[ProductResult]:
    """Algunas tiendas repiten el mismo producto en el HTML (Gollo lo lista
    dos veces: vista grid y vista lista). Nos quedamos con una por URL."""
    vistos = set()
    unicos = []
    for r in resultados:
        clave = (r.tienda, r.url)
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(r)
    return unicos


def recolectar_candidatos(query: str, verbose: bool = False) -> List[ProductResult]:
    """Consulta las 8 tiendas en paralelo. Una tienda caída no rompe el resto."""
    candidatos: List[ProductResult] = []

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        futuros = {pool.submit(t.buscar, query): t for t in TIENDAS}
        for futuro in as_completed(futuros):
            tienda = futuros[futuro]
            try:
                encontrados = futuro.result()
            except Exception as e:
                if verbose:
                    print(f"  ⚠️  {tienda.name}: {type(e).__name__}: {e}")
                continue
            if verbose:
                print(f"  {tienda.name}: {len(encontrados)} candidatos")
            candidatos.extend(encontrados)

    return _sin_duplicados(candidatos)


def buscar(query: str, verbose: bool = False) -> Dict:
    """
    Punto de entrada único. Devuelve un dict con los resultados ya filtrados
    y ordenados, más un poco de diagnóstico para mostrar en pantalla.
    """
    from ai_matcher import filtrar_coincidencias_reales, ia_disponible

    query = (query or "").strip()
    if not query:
        return {"query": "", "resultados": [], "candidatos": 0, "filtrado_por": "nada"}

    candidatos = recolectar_candidatos(query, verbose=verbose)
    total_candidatos = len(candidatos)

    if not candidatos:
        return {
            "query": query,
            "resultados": [],
            "candidatos": 0,
            "filtrado_por": "nada",
        }

    nombres_validos = None
    if ia_disponible():
        nombres_validos = filtrar_coincidencias_reales(
            query, [c.nombre for c in candidatos]
        )

    if nombres_validos is None:
        # La IA no está configurada o falló (sin crédito, error de red, etc.).
        # Caemos al filtro de texto en vez de mostrar la lista sucia.
        resultados = _filtro_de_texto(query, candidatos)
        filtrado_por = "texto"
    else:
        validos = set(nombres_validos)
        resultados = [c for c in candidatos if c.nombre in validos]
        filtrado_por = "ia"

    resultados.sort(key=lambda r: (r.precio is None, r.precio or 0))

    return {
        "query": query,
        "resultados": resultados,
        "candidatos": total_candidatos,
        "filtrado_por": filtrado_por,
    }


def _filtro_de_texto(query: str, candidatos: List[ProductResult]) -> List[ProductResult]:
    """
    Respaldo cuando no hay ANTHROPIC_API_KEY configurada.

    Es notablemente peor que la IA: exige que TODAS las palabras de la
    búsqueda aparezcan en el nombre, así que se le escapan los productos
    escritos distinto ("A515-58P Aspire 5" no matchea "aspire 5 acer").
    Sirve para que el sistema no quede inutilizable, nada más.
    """
    palabras = [p for p in query.lower().split() if len(p) > 1]
    descartes = ("combo", "kit")
    pide_combo = any(d in query.lower() for d in descartes)

    filtrados = []
    for c in candidatos:
        nombre = c.nombre.lower()
        if not all(p in nombre for p in palabras):
            continue
        if not pide_combo and any(d in nombre for d in descartes):
            continue
        filtrados.append(c)
    return filtrados


# ---------------------------------------------------------------------------
# Salida por consola
# ---------------------------------------------------------------------------
def imprimir_tabla(reporte: Dict):
    resultados = reporte["resultados"]

    if not resultados:
        print(
            f"\nSin coincidencias para \"{reporte['query']}\" "
            f"({reporte['candidatos']} candidatos revisados)."
        )
        print("Probá con el nombre del modelo más corto, o revisá la ortografía.\n")
        return

    mejor = next((r.precio for r in resultados if r.precio is not None), None)

    print(
        f"\n{len(resultados)} resultado(s) de {reporte['candidatos']} candidatos "
        f"(filtro: {reporte['filtrado_por']})\n"
    )
    print(f"{'Tienda':<12}{'Producto':<52}{'Precio':<14}{'vs. mejor':<10}")
    print("-" * 88)
    for r in resultados:
        nombre = (r.nombre[:49] + "...") if len(r.nombre) > 49 else r.nombre
        if r.precio and mejor and r.precio != mejor:
            diff = f"+{(r.precio - mejor) / mejor * 100:.1f}%"
        else:
            diff = "← mejor" if r.precio == mejor else ""
        print(f"{r.tienda:<12}{nombre:<52}{r.precio_texto:<14}{diff:<10}")

    print()
    for r in resultados:
        print(f"  {r.tienda}: {r.url}")
    print()


if __name__ == "__main__":
    print("=== GD Computadoras - Comparador de Precios ===\n")
    consulta = input("¿Qué producto buscás?: ").strip()
    print("\nConsultando tiendas...")
    imprimir_tabla(buscar(consulta, verbose=True))
