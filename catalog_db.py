"""
BuscaPorMí - Base de datos local del catálogo (caché)

En vez de golpear el sitio del competidor en cada búsqueda del vendedor,
guardamos su catálogo completo acá, en una base de datos local (SQLite).
La búsqueda del vendedor consulta ESTA base, no la web del competidor:
resultado en milisegundos, sin depender de que el sitio esté arriba.

El catálogo se llena/actualiza con build_catalog.py (correlo 1 vez al día).

Usa SQLite FTS5 (Full-Text Search) para que buscar entre miles de productos
sea instantáneo.
"""

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Optional

# Ruta ABSOLUTA, anclada a la carpeta de este archivo. Si fuera relativa
# ("catalogo.db"), un cron job que corra desde otro directorio crearía una
# base vacía en otro lado y "funcionaría" sin guardar nada donde la app busca.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalogo.db")


@dataclass
class ProductoCatalogo:
    tienda: str
    nombre: str
    precio: Optional[int]
    precio_texto: str
    url: str
    sku: Optional[str] = None
    marca: Optional[str] = None


@contextmanager
def _conexion():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def inicializar_db():
    """Crea las tablas si no existen. Llamar una vez al arrancar la app."""
    with _conexion() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS productos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tienda TEXT NOT NULL,
                nombre TEXT NOT NULL,
                precio INTEGER,
                precio_texto TEXT,
                url TEXT NOT NULL UNIQUE,
                sku TEXT,
                marca TEXT,
                actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Tabla de búsqueda de texto completo, enlazada a 'productos'
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS productos_fts USING fts5(
                nombre, content='productos', content_rowid='id'
            )
            """
        )
        # Triggers para mantener el índice FTS sincronizado automáticamente
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS productos_ai AFTER INSERT ON productos BEGIN
                INSERT INTO productos_fts(rowid, nombre) VALUES (new.id, new.nombre);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS productos_ad AFTER DELETE ON productos BEGIN
                INSERT INTO productos_fts(productos_fts, rowid, nombre) VALUES('delete', old.id, old.nombre);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS productos_au AFTER UPDATE ON productos BEGIN
                INSERT INTO productos_fts(productos_fts, rowid, nombre) VALUES('delete', old.id, old.nombre);
                INSERT INTO productos_fts(rowid, nombre) VALUES (new.id, new.nombre);
            END
            """
        )


_SQL_UPSERT = """
    INSERT INTO productos (tienda, nombre, precio, precio_texto, url, sku, marca, actualizado_en)
    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(url) DO UPDATE SET
        nombre=excluded.nombre,
        precio=excluded.precio,
        precio_texto=excluded.precio_texto,
        sku=excluded.sku,
        marca=excluded.marca,
        actualizado_en=CURRENT_TIMESTAMP
"""


def _fila(p: ProductoCatalogo):
    return (p.tienda, p.nombre, p.precio, p.precio_texto, p.url, p.sku, p.marca)


def upsert_producto(p: ProductoCatalogo):
    """Inserta el producto, o actualiza su precio si la URL ya existía."""
    with _conexion() as conn:
        conn.execute(_SQL_UPSERT, _fila(p))


def upsert_productos(productos: List[ProductoCatalogo]) -> int:
    """
    Guarda una tanda completa en UNA sola conexión y transacción.

    Construir el catálogo de Intelec son ~5.300 productos: hacerlo de uno en
    uno abriría 5.300 conexiones a SQLite. Esta versión es la que usa
    build_catalog.py.
    """
    if not productos:
        return 0
    with _conexion() as conn:
        conn.executemany(_SQL_UPSERT, [_fila(p) for p in productos])
    return len(productos)


def contar_productos(tienda: Optional[str] = None) -> int:
    with _conexion() as conn:
        if tienda:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM productos WHERE tienda = ?", (tienda,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as n FROM productos").fetchone()
        return row["n"]


def buscar_candidatos_locales(
    query: str, limite: int = 40, tienda: Optional[str] = None
) -> List[ProductoCatalogo]:
    """
    Búsqueda de texto rápida sobre el catálogo cacheado. Devuelve los
    candidatos más relevantes (todavía sin el filtro final de precisión
    de la IA, que se aplica después sobre esta lista corta).
    """
    # FTS5 usa sintaxis propia; envolvemos cada palabra con * para
    # coincidencias parciales (ej. "aspire" encuentra "Aspire5").
    # Las comillas dobles dentro de una palabra romperían la sintaxis, así
    # que se escapan duplicándolas, como manda FTS5.
    palabras = [p.strip().replace('"', '""') for p in query.split() if p.strip()]
    if not palabras:
        return []
    consulta_fts = " ".join(f'"{p}"*' for p in palabras)

    filtro_tienda = "AND p.tienda = ?" if tienda else ""
    args_fts = (consulta_fts, tienda, limite) if tienda else (consulta_fts, limite)

    with _conexion() as conn:
        try:
            filas = conn.execute(
                f"""
                SELECT p.tienda, p.nombre, p.precio, p.precio_texto, p.url, p.sku, p.marca
                FROM productos_fts
                JOIN productos p ON p.id = productos_fts.rowid
                WHERE productos_fts MATCH ? {filtro_tienda}
                ORDER BY rank
                LIMIT ?
                """,
                args_fts,
            ).fetchall()
        except sqlite3.OperationalError:
            # Si la sintaxis FTS falla por caracteres raros, respaldo simple
            like = f"%{query}%"
            args_like = (like, tienda, limite) if tienda else (like, limite)
            filas = conn.execute(
                f"""
                SELECT tienda, nombre, precio, precio_texto, url, sku, marca
                FROM productos WHERE nombre LIKE ?
                {"AND tienda = ?" if tienda else ""}
                LIMIT ?
                """,
                args_like,
            ).fetchall()

    return [
        ProductoCatalogo(
            tienda=f["tienda"], nombre=f["nombre"], precio=f["precio"],
            precio_texto=f["precio_texto"], url=f["url"], sku=f["sku"], marca=f["marca"],
        )
        for f in filas
    ]
