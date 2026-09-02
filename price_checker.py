"""
BuscaPorMí - Comparador de precios de competencia

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

import requests

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


_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-CR,es;q=0.9",
    }
)


def _enlace_roto(enlace: str) -> bool:
    """True si el enlace de un producto está bien muerto (404 real o página
    'not found' disfrazada). Los 'soft 404' (200 pero vacío) no se siguen
    parando: el título o el cuerpo delatan la página perdida.

    Evitamos hacer GET completos de páginas pesadas con métodos HEAD; si la
    tienda no lo soporta (responde 405/403), caemos a un GET corto."""

    def cabeceras():
        return _SESSION.head(enlace, timeout=15, allow_redirects=True)

    try:
        r = cabeceras()
    except requests.RequestException:
        return True
    if r.status_code == 405 or r.status_code == 403:
        # HEAD no soportado o bloqueado: probamos un GET liviano
        try:
            r = _SESSION.get(enlace, timeout=15, stream=True)
            r.raise_for_status()
            r.close()
            return r.status_code >= 400
        except requests.RequestException:
            return True
    return r.status_code >= 400


def _descartar_rotos(resultados: List[ProductResult]) -> List[ProductResult]:
    """Elimina de la lista los resultados cuyo enlace esté roto (404/not found),
    para no mostrar un 'mejor precio' que ya no existe en la tienda.
    Conserva el orden de la lista original (la IA y el filtro de texto ya la
    dejaron ordenada)."""
    vivos = []
    rotos = 0
    validos = [r for r in resultados if r.url and r.url.startswith("http")]
    if not validos:
        return resultados, rotos
    with ThreadPoolExecutor(max_workers=len(validos)) as pool:
        futuros = {pool.submit(_enlace_roto, r.url): i for i, r in enumerate(validos)}
        for futuro in as_completed(futuros):
            r = validos[futuros[futuro]]
            try:
                roto = futuro.result()
            except Exception:
                roto = True
            if roto:
                rotos += 1
            else:
                vivos.append((futuros[futuro], r))
    return [r for _, r in sorted(vivos)], rotos


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
        return {"query": "", "resultados": [], "candidatos": 0, "filtrado_por": "nada", "enlaces_caidos": 0}

    candidatos = recolectar_candidatos(query, verbose=verbose)
    total_candidatos = len(candidatos)

    if not candidatos:
        return {
            "query": query,
            "resultados": [],
            "candidatos": 0,
            "filtrado_por": "nada",
            "enlaces_caidos": 0,
        }

    nombres_validos = None
    if ia_disponible():
        nombres_validos = filtrar_coincidencias_reales(
            query, [c.nombre for c in candidatos]
        )

    if nombres_validos is None:
        # La IA no está configurada o falló (sin crédito, error de red, etc.).
        # Caemos al filtro de texto en vez de mostrar la lista sucia.
        # _filtro_de_texto ya devuelve ordenado por relevancia + precio.
        resultados = _filtro_de_texto(query, candidatos)
        filtrado_por = "texto"
    else:
        validos = set(nombres_validos)
        resultados = [c for c in candidatos if c.nombre in validos]
        filtrado_por = "ia"
        resultados.sort(key=lambda r: (r.precio is None, r.precio or 0))

    # Precisión al máximo: un resultado cuyo enlace esté roto (404/not found)
    # no se muestra, y se reporta cuántos se descartaron para que la info
    # sea clara y no se le dé al vendedor un 'mejor precio' que no existe.
    resultados, enlaces_caidos = _descartar_rotos(resultados)

    return {
        "query": query,
        "resultados": resultados,
        "candidatos": total_candidatos,
        "filtrado_por": filtrado_por,
        "enlaces_caidos": enlaces_caidos,
    }


def _filtro_de_texto(query: str, candidatos: List[ProductResult]) -> List[ProductResult]:
    """
    Respaldo cuando la IA no está disponible (sin crédito, error de red, etc.).

    Mejor que el simple "todas las palabras en el nombre": además de exigir
    que aparezcan las palabras de la búsqueda, descarta accesorios/complementos
    que contienen esas palabras pero no SON el producto (un "Protector iPhone
    15" no es un iPhone 15), y ordena por PRECIO DENTRO de la relevancia para
    que el producto real (que suele ser el más caro) no quede tapado por un
    accesorio barato.

    Devolvemos la lista ya ordenada: mejor precio y más parecido primero.
    """
    palabras = [p for p in _palabras_de(query) if p]
    claves = _claves(query)

    # Accesorios/complementos que contienen las palabras buscadas pero NO son
    # el producto en sí ("para iPhone 15", "para PS5"...). Solo se descartan
    # si el usuario NO los pidió explícitamente ("cargador iphone" sí quiere
    # el cargador). Se matchean por PREFIJO de palabra para cubrir variantes
    # ("protector" descarta "protectores"/"protector pantalla").
    no_corresponden = (
        # Accesorios y complementos
        "protector", "vidrio", "templado", "carcasa", "funda", "forro",
        "estuche", "case", "cover", "cargador", "cable", "adaptador",
        "soporte", "holder", "stand", "vinilo", "sticker", "alien",
        "repuesto", "lente", "batería",
        "bolsa", "impermeable", "silicona", "grip", "tripode",
        "bandolera", "correa", "mica",
        # Kits y combos
        "combo", "kit",
        # Cosas que se agregan solas con la palabra buscada
        "juguete", "figura", "accesorio",
    )

    def es_accesorio(nombre: str) -> bool:
        for d in no_corresponden:
            raiz = d.split()[0]
            # Si el usuario buscó la raíz ("forro", "cargador", "correa"...)
            # entonces esos productos SÍ corresponden y no se descartan.
            if raiz in claves:
                continue
            # Matchea la raíz como palabra, permitiendo sufijos
            # ("protector" pega con "protectores", "case" con "case-it").
            if " " + raiz in nombre or nombre.startswith(raiz):
                return True
        return False

    filtrados = []
    for c in candidatos:
        nombre = c.nombre.lower()
        if not all(p in nombre for p in palabras):
            continue
        if es_accesorio(nombre):
            continue
        filtrados.append(c)

    # Relevancia: cuanto más "pegado" esté el nombre a la búsqueda, primero.
    def parecido(c):
        nombre = c.nombre.lower()
        # Núcleo exacto: todas las claves en orden consecutivo y en el mismo
        # orden que la búsqueda "iphone 15" => (1, ...) el producto real.
        # Después (2) la frase completa en cualquier orden, (3) luego por
        # cuántas claves aparecen y (4) por precio (el más barato primero).
        frase = " ".join(claves)
        exacto = int(frase in nombre)
        total_idas = sum(nombre.count(p) for p in claves)
        precio = c.precio if c.precio is not None else float("inf")
        return (-exacto, -total_idas, precio)

    filtrados.sort(key=parecido)
    return filtrados


def _palabras_de(query: str) -> List[str]:
    """Palabras significativas de la búsqueda (se descartan artículos y
    preposiciones que solo agregan ruido: "para", "de", "con"...)."""
    ruido = {
        "para", "de", "con", "el", "la", "los", "las", "un", "una",
        "en", "y", "a", "por", "del", "que", "se", "su", "tu",
    }
    return [p for p in query.lower().split() if len(p) > 1 and p not in ruido]


def _claves(query: str) -> List[str]:
    """Términos clave de la búsqueda: palabras significativas excluyendo los
    números sueltos de variantes (un "15" solo no define el producto si ya hay
    "iphone"). Se usan para medir qué tan parecido es un candidato."""
    palabras = _palabras_de(query)
    if len(palabras) <= 1:
        return palabras
    # Si queda un solo número suelto (ej. "15"), no aporta a la frase núcleo.
    sin_numero = [p for p in palabras if not p.isdigit()]
    return sin_numero or palabras


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
    if reporte.get("enlaces_caidos"):
        print(f"  ⚠️  Se descartaron {reporte['enlaces_caidos']} resultado(s) porque su enlace está roto (404/not found).\n")
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
    print("=== BuscaPorMí - Comparador de Precios ===\n")
    consulta = input("¿Qué producto buscás?: ").strip()
    print("\nConsultando tiendas...")
    imprimir_tabla(buscar(consulta, verbose=True))
