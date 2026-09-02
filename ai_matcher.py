"""
GD Computadoras - Filtro de precisión con IA (Claude)

Este módulo recibe una lista "cruda" de productos (ya filtrados solo por
marca, sin filtro estricto de texto) y usa Claude para decidir cuáles
realmente corresponden al modelo que el usuario buscó.

Por qué hace falta:
El filtro de texto simple (`"aspire 5" in nombre.lower()`) falla cuando
el nombre en la tienda viene escrito distinto, ej:
  - "Acer A515-58P-53Q4 Aspire 5"       -> SÍ es el mismo modelo
  - "Acer Aspire 5 Slim A514"           -> es un modelo DIFERENTE
Un humano lo distingue al toque; un match de texto simple no.

Requiere la variable de entorno ANTHROPIC_API_KEY.
Conseguí una key en: https://console.anthropic.com/settings/keys

Si la variable no está configurada, este módulo se desactiva solo y el
sistema sigue funcionando con el filtro de texto normal (sin romper nada).
"""

import json
import os
from typing import List

import requests
from dotenv import load_dotenv

load_dotenv()  # lee el archivo .env y carga sus variables al entorno

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def ia_disponible() -> bool:
    return bool(ANTHROPIC_API_KEY)


def extraer_busqueda(query: str) -> dict:
    """
    A partir de una búsqueda libre en lenguaje natural (ej. "parlante jbl grip",
    "iphone 17 pro max", "acer aspire 5"), devuelve la marca detectada (para
    poder filtrar en la tienda) y el término núcleo de búsqueda.

    Si la IA no está disponible, usa una heurística simple: la primera
    palabra se asume como marca.
    """
    if not ia_disponible():
        partes = query.strip().split(maxsplit=1)
        marca = partes[0] if partes else ""
        return {"marca": marca, "termino": query.strip()}

    prompt = f"""Un cliente de una tienda de tecnología en Costa Rica escribió esta búsqueda:
"{query}"

Identificá la MARCA del producto (ej. Acer, Apple, JBL, HP, Samsung, Asus) y
el término núcleo de búsqueda (nombre del producto sin relleno).

Responde ÚNICAMENTE con un JSON válido, sin texto adicional, formato exacto:
{{"marca": "Acer", "termino": "aspire 5"}}

Si no podés identificar una marca clara, usa {{"marca": null, "termino": "{query}"}}"""

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        texto = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()
        texto = texto.replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto)
        return {
            "marca": resultado.get("marca"),
            "termino": resultado.get("termino", query),
        }
    except Exception as e:
        print(f"⚠️  extraer_busqueda falló, usando heurística simple: {e}")
        partes = query.strip().split(maxsplit=1)
        marca = partes[0] if partes else ""
        return {"marca": marca, "termino": query.strip()}


def filtrar_coincidencias_reales(
    query: str, nombres_productos: List[str]
) -> List[str]:
    """
    Recibe la búsqueda libre del usuario (ej. "acer aspire 5", "iphone 17",
    "parlante jbl grip") y la lista de nombres de producto tal como aparecen
    en la tienda. Devuelve SOLO los que Claude confirma que son el producto
    exacto buscado — descartando combos, kits, accesorios sueltos o
    variantes que no correspondan, salvo que la búsqueda los pida explícitamente.

    Si la IA no está configurada o falla, devuelve None. NO devuelve la lista
    sin filtrar: eso haría pasar productos irrelevantes como coincidencias y
    además mentiría en el reporte ("filtrado por IA" cuando no se filtró nada).
    El llamador (price_checker.buscar) aplica el filtro de texto como respaldo.
    """
    if not ia_disponible():
        return None
    if not nombres_productos:
        return []

    lista_numerada = "\n".join(
        f"{i}. {nombre}" for i, nombre in enumerate(nombres_productos)
    )

    prompt = f"""Un cliente de una tienda de tecnología busca:
"{query}"

Esta es la lista de productos encontrados en los catálogos de VARIAS tiendas
competidoras. El mismo producto puede aparecer repetido porque lo vende más de
una tienda, con el nombre escrito distinto:

{lista_numerada}

Responde ÚNICAMENTE con un JSON válido, sin texto adicional, con este formato exacto:
{{"coincidencias": [0, 2, 5]}}

Donde los números son los índices (empezando en 0) de los productos que
corresponden EXACTAMENTE a lo que el cliente busca.

Reglas estrictas:
- Excluí productos etiquetados como "COMBO", "KIT" o que incluyan accesorios
  extra (mochila, mouse, funda, etc.) junto al producto principal, A MENOS
  que la búsqueda del cliente mencione explícitamente "combo" o "kit".
- Excluí variantes de modelo distintas a la buscada (ej. si busca "Aspire 5"
  no incluyas "Aspire Lite" ni "Aspire Go" salvo que la búsqueda sea genérica
  como solo "Aspire", en cuyo caso sí incluí todas las líneas Aspire).
- Excluí accesorios o repuestos sueltos del producto (ej. si busca "iPhone 17"
  no incluyas fundas o cargadores para iPhone 17).
- Si hay varias variantes de almacenamiento/color del mismo modelo exacto
  (ej. iPhone 17 Pro Max 256GB y 512GB), incluí todas — son el mismo producto
  buscado, solo con distinta configuración.
- NO dedupliques: si el mismo producto aparece varias veces (porque lo
  venden distintas tiendas), incluí TODOS los índices. El objetivo es
  justamente comparar el precio de esas tiendas entre sí."""

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                # Con 8 tiendas pueden llegar ~90 candidatos; la lista de
                # índices que devuelve necesita espacio para no truncarse.
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()

        texto = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()
        texto = texto.replace("```json", "").replace("```", "").strip()

        resultado = json.loads(texto)
        indices = resultado.get("coincidencias", [])

        return [
            nombres_productos[i] for i in indices if 0 <= i < len(nombres_productos)
        ]

    except Exception as e:
        print(f"⚠️  Filtro IA falló, usando filtro de texto como respaldo: {e}")
        return None