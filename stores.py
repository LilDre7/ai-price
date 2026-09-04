"""
BuscaPorMí - Adaptadores de tienda (uno por competidor)

CÓMO FUNCIONA:
Cada competidor tiene su PROPIO buscador. En vez de bajarnos su catálogo
completo, le pasamos la búsqueda del vendedor a ese buscador y leemos los
resultados. Es una sola consulta por tienda, en vivo, sin base de datos.

Los buscadores de las tiendas son flojos (buscar "laptop" en PCTODOCR
devuelve cámaras y tarjetas madre), así que lo que llega acá son CANDIDATOS.
El filtro de precisión con IA vive en ai_matcher.py y corre después, sobre
la lista junta de todas las tiendas.

TRES FORMAS DE LEER UNA TIENDA (de mejor a peor):
  1. API JSON propia del sitio  -> Walmart, Shopify (ATEKCR, Vicortech, Ubalux),
                                    WooCommerce (TechZilla, TICOTEK, CV, Intek,
                                    IGaming, Dataland, BreakingTechnology)
  2. HTML de la página de búsqueda -> Gollo, CyberTeam, PCTODOCR, MExpress,
                                    ExtremeTech y ADN Tienda (tras Cloudflare,
                                    vía cloudscraper)
  3. Catálogo local cacheado     -> Intelec (su buscador está tras Cloudflare)

AGREGAR UNA TIENDA NUEVA:
Escribí una clase que herede de Tienda, implementá buscar(), y agregala a
TIENDAS al final del archivo. Nada más.
"""

from __future__ import annotations

import html
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    # Cloudscraper resuelve los challenges 'managed' de Cloudflare que
    # bloquean con 403 a requests plano. Si no está instalado, seguimos
    # con requests: solo fallarán las tiendas protegidas.
    import cloudscraper
except ImportError:  # pragma: no cover
    cloudscraper = None

TIMEOUT = 25
MAX_POR_TIENDA = 12  # tope de candidatos por tienda, para no inflar el prompt de la IA

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CR,es;q=0.9",
}


# ---------------------------------------------------------------------------
# Modelo de datos: así se ve un resultado de CUALQUIER tienda
# ---------------------------------------------------------------------------
@dataclass
class ProductResult:
    tienda: str
    nombre: str
    precio: Optional[int]      # colones como número entero, para poder ordenar
    precio_texto: str          # cómo se muestra en pantalla
    url: str
    sku: Optional[str] = None
    marca: Optional[str] = None
    ean: Optional[str] = None  # código de barras: match exacto entre tiendas


# ---------------------------------------------------------------------------
# Precios
# ---------------------------------------------------------------------------
def precio_desde_texto(texto: Optional[str]) -> Optional[int]:
    """
    Convierte un precio escrito en la web a un entero de colones.

    En Costa Rica el punto y la coma son separadores de MILES, no decimales:
        '₡1.159.000'  -> 1159000
        '₡471,500'    -> 471500
        '¢ 22.000'    -> 22000
    Si vienen céntimos explícitos (2 dígitos al final) los descartamos:
        '₡19.990,50'  -> 19990
    """
    if not texto:
        return None
    match = re.search(r"\d[\d.,]*\d|\d", texto)
    if not match:
        return None
    numero = match.group(0)
    if re.search(r"[.,]\d{2}$", numero):
        numero = numero[:-3]  # eran céntimos
    digitos = re.sub(r"\D", "", numero)
    return int(digitos) if digitos else None


def precio_desde_numero(valor) -> Optional[int]:
    """Para APIs que ya devuelven el precio como número ('304900.00' -> 304900)."""
    if valor is None:
        return None
    try:
        return int(round(float(valor)))
    except (TypeError, ValueError):
        return None


def formato_colones(monto: Optional[int]) -> str:
    if monto is None:
        return "N/A"
    return "₡" + f"{monto:,}".replace(",", ".")


def precio_de_tarjeta(tarjeta, selector_precio: str) -> Optional[int]:
    """
    Lee el precio de una tarjeta de producto en HTML.

    OJO - esto es lo que hacía mal la versión anterior: cuando un producto
    está en oferta, el HTML trae DOS precios (el tachado y el vigente). Hay
    que quedarse con el de <ins>, no con el primero que aparezca, o reportás
    la competencia más cara de lo que realmente está.
    """
    vigente = tarjeta.select_one("ins .amount, ins .woocommerce-Price-amount, ins bdi, ins")
    if vigente:
        precio = precio_desde_texto(vigente.get_text(" ", strip=True))
        if precio:
            return precio
    nodo = tarjeta.select_one(selector_precio)
    if not nodo:
        return None
    # Si el bloque trae varios montos, el vigente en WooCommerce es el último
    montos = [
        precio_desde_texto(t)
        for t in re.findall(r"[₡¢$]\s?[\d.,]+", nodo.get_text(" ", strip=True))
    ]
    montos = [m for m in montos if m]
    if montos:
        return montos[-1] if len(montos) > 1 else montos[0]
    return precio_desde_texto(nodo.get_text(" ", strip=True))


# ---------------------------------------------------------------------------
# Páginas "not found"
# ---------------------------------------------------------------------------
_PATRON_NO_ENCONTRADA = re.compile(
    r"\b404\b|not ?found|no se encontr(?:ó|o|aron)|"
    r"p[áa]gina no encontrada|no (?:hay|encontramo)|"
    r"no hay resultados|sin resultados|no results|"
    r"producto no disponible",
    re.IGNORECASE,
)


def es_pagina_no_encontrada(soup) -> bool:
    """True si la página parece un 404/'not found' disfrazado de página 200
    (los llamados 'soft 404'). Algunas tiendas flojas responden 200 con el
    mensaje de error dentro del HTML normal, o devuelven el título del
    producto anterior con el cuerpo vacío."""
    if soup is None:
        return True
    piezas = []
    if soup.title:
        piezas.append(soup.title.get_text(" ", strip=True))
    h1 = soup.select_one("h1")
    if h1:
        piezas.append(h1.get_text(" ", strip=True))
    texto = " ".join(piezas).lower()
    return bool(texto and _PATRON_NO_ENCONTRADA.search(texto))


# ---------------------------------------------------------------------------
# Interfaz base
# ---------------------------------------------------------------------------
class Tienda(ABC):
    name: str
    base: str

    # True para las tiendas cuyo sitio entero está tras un challenge de
    # Cloudflare ('Just a moment...'): se consulta con cloudscraper, que
    # resuelve el challenge, en vez de requests plano.
    usa_cloudflare = False

    def __init__(self):
        if self.usa_cloudflare and cloudscraper is not None:
            # La API de cloudscraper imita la de requests, así que todo el
            # resto del código (get/json/...) funciona igual.
            self.session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
        else:
            self.session = requests.Session()
        self.session.headers.update(HEADERS)

    @abstractmethod
    def buscar(self, query: str) -> List[ProductResult]:
        """Devuelve candidatos para esa búsqueda. Sin filtrar por precisión."""
        ...

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[requests.Response]:
        """GET tolerante: cualquier fallo devuelve None en vez de romper la búsqueda."""
        try:
            resp = self.session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException:
            return None
        return resp if resp.status_code == 200 else None

    def _get_html(self, url: str, params: Optional[dict] = None):
        """GET de una página HTML devolviendo el BeautifulSoup, o None.

        Si la tienda responde 200 pero es una página de 'no encontrado',
        la tratamos como si no existiera: parsear su decoración como si
        fueran productos ensuciaría la lista de candidatos.
        """
        resp = self._get(url, params)
        if resp is None:
            return None
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:
            return None
        if es_pagina_no_encontrada(soup):
            return None
        return soup

    def _absoluta(self, href: str) -> str:
        return urljoin(self.base, href) if href else self.base


# ---------------------------------------------------------------------------
# 1. Walmart CR - VTEX, API "intelligent-search"
#    La mejor fuente del grupo: JSON limpio, precio vigente y de lista, y
#    EAN (código de barras del fabricante), que permite emparejar productos
#    con otras tiendas de forma exacta en vez de comparar nombres.
# ---------------------------------------------------------------------------
class WalmartTienda(Tienda):
    name = "Walmart"
    base = "https://www.walmart.co.cr"
    API = "https://www.walmart.co.cr/api/io/_v/api/intelligent-search/product_search"

    def buscar(self, query: str) -> List[ProductResult]:
        resp = self._get(self.API, {"query": query, "count": MAX_POR_TIENDA})
        if not resp:
            return []
        try:
            productos = resp.json().get("products", [])
        except ValueError:
            return []

        resultados = []
        for p in productos:
            oferta = self._mejor_oferta(p)
            if not oferta:
                continue
            precio, item, vendedor = oferta

            # Walmart es un marketplace: cuando el precio lo pone un tercero y
            # no Walmart mismo, hay que decirlo. No es lo mismo comparar contra
            # Walmart que contra un revendedor que usa su plataforma.
            etiqueta = self.name
            if vendedor and "walmart" not in vendedor.lower():
                etiqueta = f"{self.name}/{vendedor}"

            enlace = p.get("linkText")
            resultados.append(
                ProductResult(
                    tienda=etiqueta,
                    nombre=p.get("productName") or "",
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=f"{self.base}/{enlace}/p" if enlace else self.base,
                    sku=item.get("itemId"),
                    marca=p.get("brand"),
                    ean=item.get("ean") or None,
                )
            )
        return resultados

    @staticmethod
    def _mejor_oferta(producto: dict):
        """
        Un producto de VTEX puede tener varias variantes ('items') y cada una
        varios vendedores. El primero suele ser Walmart, pero si está agotado
        viene con Price=0 mientras un vendedor del marketplace SÍ tiene precio.
        Leer solo el primero descartaba productos que sí se pueden comparar.

        Devuelve (precio, item, nombre_del_vendedor) del más barato disponible.
        """
        mejor = None
        for item in producto.get("items") or []:
            for vendedor in item.get("sellers") or []:
                comercial = vendedor.get("commertialOffer") or {}
                precio = precio_desde_numero(comercial.get("Price"))
                disponible = comercial.get("AvailableQuantity") or 0
                if not precio or disponible <= 0:
                    continue
                if mejor is None or precio < mejor[0]:
                    mejor = (precio, item, vendedor.get("sellerName"))
        return mejor


# ---------------------------------------------------------------------------
# 2. WooCommerce Store API (JSON público, sin autenticación)
#    Base compartida por todas las tiendas WooCommerce de Costa Rica que
#    exponen /wp-json/wc/store/v1/products: TechZilla, TICOTEK, CV Electrónica
#    e Intek. Todas devuelven el precio en la misma forma, solo cambian
#    name/base/API.
# ---------------------------------------------------------------------------
class WooCommerceApiTienda(Tienda):
    name = ""
    base = ""
    API = ""

    def buscar(self, query: str) -> List[ProductResult]:
        resp = self._get(self.API, {"search": query, "per_page": MAX_POR_TIENDA})
        if not resp:
            return []
        try:
            productos = resp.json()
        except ValueError:
            return []
        if not isinstance(productos, list):
            return []

        resultados = []
        for p in productos:
            precios = p.get("prices") or {}
            # Woo entrega el precio en "unidades menores": con minor_unit=2 el
            # valor 830000 significa ₡8.300. En CRC suele ser 0, pero lo leemos
            # en vez de asumirlo.
            escala = 10 ** int(precios.get("currency_minor_unit") or 0)
            crudo = precio_desde_numero(precios.get("price"))
            precio = int(crudo / escala) if crudo is not None else None
            if not precio:
                continue

            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=p.get("name") or "",
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=p.get("permalink") or self.base,
                    sku=p.get("sku") or None,
                )
            )
        return resultados


class TechZillaTienda(WooCommerceApiTienda):
    # Especialista en componentes y enfriamiento líquido: buscar "laptop"
    # acá da poco, y eso es su catálogo real, no un error.
    name = "TechZilla"
    base = "https://techzilla.cr"
    API = "https://techzilla.cr/wp-json/wc/store/v1/products"


class TicotekTienda(WooCommerceApiTienda):
    # Especialista en componentes de PC y gaming (tarjetas de video,
    # procesadores, memorias, monitores).
    name = "TICOTEK"
    base = "https://www.ticotek.com"
    API = "https://www.ticotek.com/wp-json/wc/store/v1/products"


class CvElectronicaTienda(WooCommerceApiTienda):
    # Electrónica, componentes, audio, seguridad y cómputo.
    name = "CV Electrónica"
    base = "https://cvelectronica.com"
    API = "https://cvelectronica.com/wp-json/wc/store/v1/products"


class IntekTienda(WooCommerceApiTienda):
    # Catálogo chico (~105 items): equipos recertificados, outlet y ensambles.
    name = "Intek"
    base = "https://intekcr.com"
    API = "https://intekcr.com/wp-json/wc/store/v1/products"


class IGamingTienda(WooCommerceApiTienda):
    # Gaming y PC hardware: tarjetas de video, procesadores, memorias,
    # monitores, sillas y accesorios.
    name = "IGaming"
    base = "https://igamingcr.com"
    API = "https://igamingcr.com/wp-json/wc/store/v1/products"


class DatalandTienda(WooCommerceApiTienda):
    # Componentes de PC: procesadores, tarjetas de video, RAM, fuentes de
    # poder, gabinetes y refrigeración. Precios en colones (minor_unit 0).
    name = "Dataland"
    base = "https://datalandcr.com"
    API = "https://datalandcr.com/wp-json/wc/store/v1/products"


class BreakingTechnologyTienda(WooCommerceApiTienda):
    # Laptops, desktops y componentes con precio en colones (minor_unit 2,
    # o sea el JSON trae 87500000 = 875.000, ya lo maneja la base).
    name = "BreakingTechnology"
    base = "https://btcr.cr"
    API = "https://btcr.cr/wp-json/wc/store/v1/products"


# ---------------------------------------------------------------------------
# 3. Shopify, endpoint de sugerencias (JSON)
#    ATEKCR, Vicortech y Ubalux usan la misma plataforma: /search/suggest.json
#    les pasa la búsqueda y devuelven productos con precio. Solo cambia el
#    dominio y el nombre a mostrar.
# ---------------------------------------------------------------------------
class ShopifyTienda(Tienda):
    name = ""
    base = ""
    API = ""

    def buscar(self, query: str) -> List[ProductResult]:
        resp = self._get(
            self.API,
            {
                "q": query,
                "resources[type]": "product",
                "resources[limit]": MAX_POR_TIENDA,
            },
        )
        if not resp:
            return []
        try:
            productos = resp.json()["resources"]["results"]["products"]
        except (ValueError, KeyError, TypeError):
            return []

        resultados = []
        for p in productos:
            precio = precio_desde_numero(p.get("price"))
            if not precio:
                continue
            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=p.get("title") or "",
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=self._absoluta(p.get("url", "")),
                    marca=p.get("vendor") or None,
                )
            )
        return resultados


class AtekTienda(ShopifyTienda):
    name = "ATEKCR"
    base = "https://atekcr.com"
    API = "https://atekcr.com/search/suggest.json"


class VicortechTienda(ShopifyTienda):
    # Celulares, relojes inteligentes, gaming, monitores, computadoras,
    # electrodomésticos y cuidado personal. Precios en colones.
    name = "Vicortech"
    base = "https://tiendavicortech.com"
    API = "https://tiendavicortech.com/search/suggest.json"


class UbaluxTienda(ShopifyTienda):
    # Tecnología y gadgets para el hogar: audífonos, smartwatches,
    # televisores, streaming, redes. Precios en colones.
    name = "Ubalux"
    base = "https://ubalux.com"
    API = "https://ubalux.com/search/suggest.json"


# ---------------------------------------------------------------------------
# 4. Gollo - Magento, página de búsqueda
#    Repite cada producto dos veces en el HTML (vista grid + lista); la
#    deduplicación por URL en price_checker se encarga de eso.
# ---------------------------------------------------------------------------
class GolloTienda(Tienda):
    name = "Gollo"
    base = "https://www.gollo.com"

    def buscar(self, query: str) -> List[ProductResult]:
        soup = self._get_html(f"{self.base}/catalogsearch/result/", {"q": query})
        if not soup:
            return []

        resultados = []
        for tarjeta in soup.select("li.product-item, .product-item-info")[: MAX_POR_TIENDA * 2]:
            enlace = tarjeta.select_one(".product-item-link")
            if not enlace:
                continue
            precio = precio_de_tarjeta(tarjeta, ".price")
            if not precio:
                continue
            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=enlace.get_text(strip=True),
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=self._absoluta(enlace.get("href", "")),
                )
            )
        return resultados


# ---------------------------------------------------------------------------
# 5. CyberTeam - sitio propio (Tailwind). El nombre completo del producto
#    vive en el alt de la imagen, no en el texto del enlace.
# ---------------------------------------------------------------------------
class CyberTeamTienda(Tienda):
    name = "CyberTeam"
    base = "https://cyberteamcr.com"

    def buscar(self, query: str) -> List[ProductResult]:
        soup = self._get_html(f"{self.base}/search", {"q": query})
        if not soup:
            return []

        resultados = []
        for tarjeta in soup.select("div.product-card-dark")[:MAX_POR_TIENDA]:
            enlace = tarjeta.select_one('a[href*="/product/"]')
            imagen = tarjeta.select_one("img[alt]")
            if not enlace or not imagen:
                continue
            precio = precio_de_tarjeta(tarjeta, "span.current-price, .price-row")
            if not precio:
                continue
            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=imagen.get("alt", "").strip(),
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=self._absoluta(enlace.get("href", "")),
                )
            )
        return resultados


# ---------------------------------------------------------------------------
# 6. PCTODOCR - sitio propio. Su .card-brand es en realidad la categoría
#    ("CASES", "Camaras"), no la marca, así que no la usamos como marca.
# ---------------------------------------------------------------------------
class PcTodoTienda(Tienda):
    name = "PCTODOCR"
    base = "https://www.pctodocr.com"

    def buscar(self, query: str) -> List[ProductResult]:
        soup = self._get_html(f"{self.base}/", {"s": query, "post_type": "product"})
        if not soup:
            return []

        resultados = []
        for tarjeta in soup.select("article.product-card, .product-card")[:MAX_POR_TIENDA]:
            enlace = tarjeta.select_one("a.card-title")
            if not enlace:
                continue
            precio = precio_de_tarjeta(tarjeta, ".card-price")
            if not precio:
                continue
            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=enlace.get_text(strip=True),
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=self._absoluta(enlace.get("href", "")),
                )
            )
        return resultados


# ---------------------------------------------------------------------------
# 7. MExpress - nopCommerce. Es la tienda más lenta del grupo (su búsqueda
#    tarda ~30s en responder), por eso lleva timeout propio más generoso.
# ---------------------------------------------------------------------------
class MExpressTienda(Tienda):
    name = "MExpress"
    base = "https://www.tiendamexpress.com"

    def _get(self, url, params=None):
        try:
            resp = self.session.get(url, params=params, timeout=60)
        except requests.RequestException:
            return None
        return resp if resp.status_code == 200 else None

    def buscar(self, query: str) -> List[ProductResult]:
        soup = self._get_html(f"{self.base}/search", {"q": query})
        if not soup:
            return []

        resultados = []
        for caja in soup.select(".item-box")[:MAX_POR_TIENDA]:
            enlace = caja.select_one("h3.product-title a, .product-title a")
            if not enlace:
                continue
            precio = precio_de_tarjeta(
                caja, ".prices .actual-price, .actual-price, .prices"
            )
            if not precio:
                continue
            item = caja.select_one("[data-productid]")
            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=enlace.get_text(strip=True),
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=self._absoluta(enlace.get("href", "")),
                    sku=item.get("data-productid") if item else None,
                )
            )
        return resultados


# ---------------------------------------------------------------------------
# 8. ExtremeTech - WooCommerce con tema Woodmart (mismo HTML que Intelec).
#    Todo el sitio está tras un challenge de Cloudflare, así que se consulta
#    con cloudscraper. Su buscador (?s=&post_type=product) es flojo y devuelve
#    candidatos de cualquier categoría; el filtro de IA los limpia después.
# ---------------------------------------------------------------------------
class ExtremeTechTienda(Tienda):
    name = "ExtremeTech"
    base = "https://extremetechcr.com"
    usa_cloudflare = True

    def buscar(self, query: str) -> List[ProductResult]:
        soup = self._get_html(
            f"{self.base}/", {"s": query, "post_type": "product"}
        )
        if not soup:
            return []

        resultados = []
        for tarjeta in soup.select(".product-grid-item")[:MAX_POR_TIENDA]:
            enlace = tarjeta.select_one("h3.wd-entities-title a")
            if not enlace:
                continue
            nombre = enlace.get_text(strip=True)
            if not nombre:
                continue
            precio = precio_de_tarjeta(tarjeta, ".price")
            if not precio:
                continue
            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=nombre,
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=self._absoluta(enlace.get("href", "")),
                )
            )
        return resultados


# ---------------------------------------------------------------------------
# 9. ADN Tienda - Odoo (e-commerce). Busca en /shop?search=... y cada tarjeta
#    es un <form itemprop="Product"> con el precio vigente y el tachado.
#    Todo el sitio está tras un challenge de Cloudflare, como ExtremeTech.
# ---------------------------------------------------------------------------
class AdnTienda(Tienda):
    name = "ADN Tienda"
    base = "https://www.adntienda.com"
    usa_cloudflare = True

    def buscar(self, query: str) -> List[ProductResult]:
        soup = self._get_html(f"{self.base}/shop", {"search": query})
        if not soup:
            return []

        resultados = []
        for tarjeta in soup.select("form[itemprop='product'], form[action='/shop/cart/update']")[
            :MAX_POR_TIENDA
        ]:
            enlace = tarjeta.select_one("a[itemprop='url']")
            if not enlace:
                continue

            # El nombre está en el itemprop="name" del form; si el tema no lo
            # trae, el texto del enlace de la imagen suele ser el título.
            nodo_nombre = tarjeta.find(attrs={"itemprop": "name"})
            nombre = (
                nodo_nombre.get_text(strip=True)
                if nodo_nombre
                else enlace.get_text(strip=True)
            )
            if not nombre:
                continue

            # En Odoo el precio vigente está en .product_price (el <del> es el
            # tachado); el <span itemprop="price"> trae el número ya limpio.
            span_precio = tarjeta.select_one('[itemprop="price"]')
            precio = (
                precio_desde_numero(span_precio.get("content") or span_precio.get_text())
                if span_precio
                else precio_de_tarjeta(tarjeta.select_one(".product_price") or tarjeta, ".oe_currency_value")
            )
            if not precio:
                continue

            resultados.append(
                ProductResult(
                    tienda=self.name,
                    nombre=nombre,
                    precio=precio,
                    precio_texto=formato_colones(precio),
                    url=self._absoluta(enlace.get("href", "")),
                )
            )
        return resultados


# ---------------------------------------------------------------------------
# Tiendas que NO se pueden consultar en vivo
#
# Para estas dos, build_catalog.py se baja el catálogo de noche y acá solo
# consultamos la copia local (SQLite + FTS5): instantáneo y sin tocar el sitio
# del competidor en horas de trabajo.
# ---------------------------------------------------------------------------
class TiendaCatalogoLocal(Tienda):
    """Busca en el catálogo cacheado en vez de en el sitio del competidor."""

    def buscar(self, query: str) -> List[ProductResult]:
        from catalog_db import buscar_candidatos_locales

        try:
            productos = buscar_candidatos_locales(
                query, limite=MAX_POR_TIENDA, tienda=self.name
            )
        except Exception:
            return []  # el catálogo todavía no se construyó

        return [
            ProductResult(
                tienda=p.tienda,
                nombre=p.nombre,
                precio=p.precio,
                precio_texto=p.precio_texto or formato_colones(p.precio),
                url=p.url,
                sku=p.sku,
                marca=p.marca,
            )
            for p in productos
            if p.precio
        ]


class IntelecTienda(TiendaCatalogoLocal):
    # Su buscador (/?s=) está detrás de un challenge de Cloudflare y devuelve
    # una página vacía. Sus listados por categoría sí responden, así que el
    # catálogo se arma desde ahí.
    name = "Intelec"
    base = "https://www.intelec.co.cr"


class MongeTienda(TiendaCatalogoLocal):
    # Dos razones para no consultarla en vivo: su robots.txt prohíbe
    # explícitamente /catalogsearch/ y /search, y además la página de
    # resultados la pinta JavaScript (no trae productos en el HTML).
    # Sus páginas de producto sí están permitidas y publican JSON-LD.
    name = "Monge"
    base = "https://www.tiendamonge.com"


# ---------------------------------------------------------------------------
# Registro central. Agregar una tienda nueva = una línea más acá.
#
# Verificadas y funcionando. No incluidas, con razón:
#   Compatibles pero en dólares (no en colones): HighTechCR, RavenCorp?
#   Faith Technology (faithtechnologycr.com) 403 de Cloudflare
#   Sintec (sinteccr.com)             no publica precios, vende por cotización
#   Star Computers                    no tiene sitio web, solo TikTok/Threads
#   PriceSmart                        Next.js, sus rutas de API dan 404/500
#   CompuComponentes / CQNet          buscador no devuelve productos (PrestaShop
#                                     o catálogo no indexado)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 10. YIDRO - WooCommerce. Los productos de preventa/no indexados en la
#      API Store se leen desde el HTML de las páginas de categoría
#      (JSON-LD estructurado). Scrapea /escritorios-elevables/ y /escritorios/
# ---------------------------------------------------------------------------
class YidroTienda(Tienda):
    name = "YIDRO"
    base = "https://yidro.online"

    # Mapeo de búsquedas comunes a las URLs de categoría de YIDRO
    CATEGORY_MAP = {
        "elevable": "/escritorios-elevables/",
        "electrico": "/escritorios-elevables/",
        "standing": "/escritorios-elevables/",
        "escritorio": "/escritorios/",
        "mesa": "/escritorios/",
    }

    def _resolve_url(self, query: str) -> str:
        q = query.lower()
        for keyword, path in self.CATEGORY_MAP.items():
            if keyword in q:
                return f"{self.base}{path}"
        return f"{self.base}/escritorios/"

    def buscar(self, query: str) -> List[ProductResult]:
        url = self._resolve_url(query)
        soup = self._get_html(url)
        if not soup:
            return []

        resultados = []
        vistos = set()

        # Parsear JSON-LD estructurado de cada <script type="application/ld+json">
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

            # Puede venir como lista o como objeto único
            # Manejar ItemList (productos anidados) o Product directo
            if data.get("@type") == "ItemList" and "itemListElement" in data:
                items = [e.get("item") for e in data.get("itemListElement", []) if isinstance(e, dict)]
            elif isinstance(data, list):
                items = data
            else:
                items = [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "ItemList" and "itemListElement" in item:
                    # Anidado doble nivel
                    for nested in item.get("itemListElement", []):
                        if isinstance(nested, dict) and nested.get("item"):
                            items.append(nested["item"])
                    continue
                if item.get("@type") != "Product":
                    continue

                nombre = html.unescape(item.get("name", "")).strip()
                if not nombre:
                    continue

                # Extraer precio de las ofertas
                precio = None
                offers = item.get("offers")
                if isinstance(offers, dict):
                    precio = precio_desde_numero(offers.get("price"))
                elif isinstance(offers, list):
                    for offer in offers:
                        if isinstance(offer, dict):
                            precio = precio_desde_numero(offer.get("price"))
                            if precio:
                                break

                if not precio:
                    continue

                url_prod = item.get("url") or ""
                if not url_prod:
                    # Intentar construir desde el SKU
                    sku = item.get("sku", "")
                    if sku:
                        url_prod = f"{self.base}/{sku}/"

                # Deduplicar por URL
                url_key = (self.name, url_prod)
                if url_key in vistos:
                    continue
                vistos.add(url_key)

                resultados.append(
                    ProductResult(
                        tienda=self.name,
                        nombre=nombre,
                        precio=precio,
                        precio_texto=formato_colones(precio),
                        url=url_prod or self.base,
                        sku=item.get("sku") or None,
                    )
                )

        return resultados


TIENDAS: List[Tienda] = [
    # En vivo: le pasan la búsqueda al buscador de la tienda
    AdnTienda(),
    AtekTienda(),
    BreakingTechnologyTienda(),
    CvElectronicaTienda(),
    CyberTeamTienda(),
    DatalandTienda(),
    ExtremeTechTienda(),
    GolloTienda(),
    IGamingTienda(),
    IntekTienda(),
    MExpressTienda(),
    PcTodoTienda(),
    TechZillaTienda(),
    TicotekTienda(),
    UbaluxTienda(),
    VicortechTienda(),
    WalmartTienda(),
    # Desde el catálogo local que arma build_catalog.py
    IntelecTienda(),
    MongeTienda(),
    # Tienda en vivo de muebles y oficina
    YidroTienda(),
]
