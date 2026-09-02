"""
GD Computadoras - Constructor del catálogo local

PARA QUÉ:
La mayoría de los competidores se consultan en vivo: stores.py le pasa la
búsqueda al buscador de cada tienda y listo. Dos no se pueden consultar así:

  Intelec  su buscador (/?s=) está detrás de un challenge de Cloudflare y
           devuelve una página vacía. Sus LISTADOS por categoría sí responden.
  Monge    su robots.txt prohíbe /catalogsearch/ explícitamente, y además la
           página de resultados la pinta JavaScript (no trae productos en el
           HTML). Pero publica un sitemap y sus páginas de producto sí traen
           los datos.

Para esas dos nos bajamos el catálogo de noche y las búsquedas del día
consultan la copia local.

DOS ESTRATEGIAS, MISMA INTERFAZ:
Cada fuente declara sus 'tareas' (URLs a descargar) y sabe 'parsear' el HTML
de una tarea. El motor las descarga en paralelo y guarda por tandas.

  FuenteListado  -> una tarea = una página de listado con ~20 productos
                    (Intelec: /tienda/page/N/)
  FuenteSitemap  -> una tarea = una página de producto con 1 producto,
                    leída de sus datos estructurados JSON-LD (Monge)

USO:
    python build_catalog.py            # todas las fuentes
    python build_catalog.py intelec    # solo una

Conviene dejarlo en un cron de madrugada:
    0 3 * * *  cd /ruta/al/proyecto && ./venv/bin/python build_catalog.py
"""

import json
import math
import re
import sys
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from catalog_db import (
    ProductoCatalogo,
    contar_productos,
    inicializar_db,
    upsert_productos,
)
from stores import HEADERS, formato_colones, precio_de_tarjeta, precio_desde_numero

HILOS = 5           # suave con el sitio del competidor
PAUSA = 0.25        # segundos entre peticiones de un mismo hilo
TANDA = 500         # productos por escritura a SQLite
MAX_TAREAS = 8000   # tope de seguridad


# ---------------------------------------------------------------------------
# Interfaz común
# ---------------------------------------------------------------------------
class FuenteCatalogo(ABC):
    name: str

    @abstractmethod
    def tareas(self) -> List[str]:
        """URLs a descargar para recorrer el catálogo completo."""
        ...

    @abstractmethod
    def parsear(self, url: str, html: str) -> List[ProductoCatalogo]:
        """Productos que salen de esa URL (0, 1 o muchos)."""
        ...


# ---------------------------------------------------------------------------
# Estrategia A: páginas de listado (muchos productos por descarga)
# ---------------------------------------------------------------------------
class FuenteListado(FuenteCatalogo):
    LISTADO: str
    SELECTOR_ITEM: str

    def url_pagina(self, n: int) -> str:
        return self.LISTADO if n == 1 else f"{self.LISTADO}page/{n}/"

    def tareas(self) -> List[str]:
        """
        Pide la primera página para saber cuántos productos declara el sitio
        y calcular cuántas páginas hay. WooCommerce lo publica así:
        'Mostrando 1–20 de 5313 resultados'
        """
        html = _bajar(self.url_pagina(1))
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        por_pagina = len(soup.select(self.SELECTOR_ITEM)) or 20

        nodo = soup.select_one(".woocommerce-result-count")
        total = None
        if nodo:
            match = re.search(r"de\s+([\d.,]+)\s+resultados", nodo.get_text(" ", strip=True))
            if match:
                total = int(re.sub(r"\D", "", match.group(1)))

        paginas = math.ceil(total / por_pagina) if total else 1
        return [self.url_pagina(n) for n in range(1, min(paginas, MAX_TAREAS) + 1)]


class IntelecFuente(FuenteListado):
    name = "Intelec"
    LISTADO = "https://www.intelec.co.cr/tienda/"
    # OJO: el selector correcto es .product-grid-item. Usar 'div.product' trae
    # 28 nodos por página, de los cuales 8 son basura sin título.
    SELECTOR_ITEM = ".product-grid-item"

    def parsear(self, url: str, html: str) -> List[ProductoCatalogo]:
        soup = BeautifulSoup(html, "html.parser")
        productos = []

        for tarjeta in soup.select(self.SELECTOR_ITEM):
            enlace = tarjeta.select_one("h3.wd-entities-title a")
            if not enlace:
                continue

            nombre = enlace.get_text(strip=True)
            destino = enlace.get("href", "")
            if not nombre or not destino:
                continue

            # precio_de_tarjeta prioriza el <ins> (precio vigente) sobre el
            # <del> (tachado). Sin eso, un producto en oferta se guardaba con
            # el precio viejo: ₡600 en vez de ₡550.
            precio = precio_de_tarjeta(tarjeta, ".price")
            if not precio:
                continue

            sku = tarjeta.select_one(".wd-sku")
            marca = tarjeta.select_one(".wd-product-brands-links a")

            productos.append(
                ProductoCatalogo(
                    tienda=self.name,
                    nombre=nombre,
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=destino,
                    sku=sku.get_text(strip=True) if sku else None,
                    marca=marca.get_text(strip=True) if marca else None,
                )
            )
        return productos


# ---------------------------------------------------------------------------
# Estrategia B: sitemap + JSON-LD por producto
#
# JSON-LD (schema.org) es el formato ESTÁNDAR que las tiendas publican para
# Google Shopping. Cuando existe es la forma más confiable de leer
# nombre/precio/SKU, porque no depende de adivinar clases CSS del sitio.
# ---------------------------------------------------------------------------
def leer_json_ld_producto(html: str) -> Optional[dict]:
    """
    Busca un bloque <script type="application/ld+json"> con un Producto.
    Soporta las tres formas en que las tiendas lo publican: objeto suelto,
    lista de objetos, y grafo ('@graph', que es lo que usa Yoast SEO).
    """
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            datos = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        pendientes = datos if isinstance(datos, list) else [datos]
        for item in pendientes:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                pendientes.extend(item["@graph"])
                continue
            tipo = item.get("@type", "")
            if tipo == "Product" or (isinstance(tipo, list) and "Product" in tipo):
                return item
    return None


class FuenteSitemap(FuenteCatalogo):
    SITEMAP: str
    PREFIJO_PRODUCTO: Optional[str] = None  # si las URLs de producto se distinguen

    def tareas(self) -> List[str]:
        urls = _urls_de_sitemap(self.SITEMAP)
        if self.PREFIJO_PRODUCTO:
            urls = [u for u in urls if self.PREFIJO_PRODUCTO in u]
        return urls[:MAX_TAREAS]

    def parsear(self, url: str, html: str) -> List[ProductoCatalogo]:
        datos = leer_json_ld_producto(html)
        if not datos:
            return []

        oferta = datos.get("offers") or {}
        if isinstance(oferta, list):
            oferta = oferta[0] if oferta else {}

        precio = precio_desde_numero(oferta.get("price") if isinstance(oferta, dict) else None)
        nombre = datos.get("name")
        if not nombre or not precio:
            return []

        marca = datos.get("brand")
        if isinstance(marca, dict):
            marca = marca.get("name")

        return [
            ProductoCatalogo(
                tienda=self.name,
                nombre=str(nombre).strip(),
                precio=precio,
                precio_texto=formato_colones(precio),
                url=url,
                sku=str(datos.get("sku")) if datos.get("sku") else None,
                marca=str(marca) if marca else None,
            )
        ]


class MongeFuente(FuenteSitemap):
    name = "Monge"
    # El sitemap está declarado en su propio robots.txt. Sus páginas de
    # producto SÍ están permitidas; lo prohibido es /catalogsearch/ y /search,
    # que es justamente por lo que Monge no se consulta en vivo.
    SITEMAP = "https://www.tiendamonge.com/media/sitemap_tienda_monge_cr.xml"

    def tareas(self) -> List[str]:
        # El índice apunta a un sitemap de URLs y otro de imágenes; el de
        # imágenes no sirve para leer precios.
        return [u for u in super().tareas() if "-images" not in u][:MAX_TAREAS]


FUENTES: List[FuenteCatalogo] = [
    IntelecFuente(),
    MongeFuente(),
]


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------
def _bajar(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    time.sleep(PAUSA)
    return resp.text if resp.status_code == 200 else None


def _urls_de_sitemap(sitemap_url: str, _vistos=None) -> List[str]:
    """Lee un sitemap.xml. Si es un índice que apunta a otros, los sigue."""
    if _vistos is None:
        _vistos = set()
    if sitemap_url in _vistos:
        return []
    _vistos.add(sitemap_url)

    try:
        resp = requests.get(sitemap_url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.content, "xml")
    except Exception as e:
        print(f"    ⚠️  No se pudo leer el sitemap {sitemap_url}: {e}")
        return []

    sub = [loc.get_text(strip=True) for loc in soup.select("sitemap > loc")]
    if sub:
        urls = []
        for s in sub:
            if "-images" in s:
                continue
            urls.extend(_urls_de_sitemap(s, _vistos))
        return urls

    return [loc.get_text(strip=True) for loc in soup.select("url > loc")]


def construir_fuente(fuente: FuenteCatalogo) -> int:
    print(f"\n=== {fuente.name} ===")
    print("  Armando la lista de páginas a recorrer...")

    tareas = fuente.tareas()
    if not tareas:
        print(f"  ⚠️  No se obtuvieron URLs para {fuente.name}")
        return 0
    print(f"  {len(tareas)} páginas a descargar")

    acumulados: List[ProductoCatalogo] = []
    guardados = 0
    sin_respuesta = 0
    sin_datos = 0

    # Los hilos SOLO descargan. El parseo y el guardado en SQLite se hacen en
    # este hilo, por tandas, para no pelear por el archivo de la base.
    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        futuros = {pool.submit(_bajar, url): url for url in tareas}
        for i, futuro in enumerate(as_completed(futuros), 1):
            url = futuros[futuro]
            html = futuro.result()

            if not html:
                sin_respuesta += 1
            else:
                encontrados = fuente.parsear(url, html)
                if not encontrados:
                    sin_datos += 1
                acumulados.extend(encontrados)

            if len(acumulados) >= TANDA:
                guardados += upsert_productos(acumulados)
                acumulados = []

            if i % 250 == 0:
                print(
                    f"  ...{i}/{len(tareas)} páginas, "
                    f"{guardados + len(acumulados)} productos"
                )

    guardados += upsert_productos(acumulados)

    print(f"  ✅ {guardados} productos guardados de {fuente.name}")
    if sin_respuesta:
        print(f"     ({sin_respuesta} páginas no respondieron)")
    if sin_datos:
        print(f"     ({sin_datos} páginas sin producto legible: categorías, agotados, etc.)")
    return guardados


def construir_catalogo(solo: Optional[str] = None):
    inicializar_db()

    fuentes = FUENTES
    if solo:
        fuentes = [f for f in FUENTES if f.name.lower() == solo.lower()]
        if not fuentes:
            disponibles = ", ".join(f.name for f in FUENTES)
            print(f"No existe la fuente {solo!r}. Disponibles: {disponibles}")
            return

    for fuente in fuentes:
        construir_fuente(fuente)

    print(f"\nTotal en el catálogo local: {contar_productos()} productos")
    for f in FUENTES:
        print(f"  {f.name}: {contar_productos(f.name)}")


if __name__ == "__main__":
    inicio = time.time()
    construir_catalogo(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Terminado en {time.time() - inicio:.0f}s")
