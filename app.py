"""
GD Computadoras - Comparador de Precios
Backend Flask: sirve la página y expone /api/buscar

EJECUTAR:
    pip install -r requirements.txt
    python app.py
    (abrir http://localhost:5000)

VARIABLES DE ENTORNO (archivo .env):
    ANTHROPIC_API_KEY  Filtro de precisión con IA. Sin esta variable el sistema
                       sigue funcionando, pero con un filtro de texto simple que
                       se le escapan productos escritos distinto.
    PORT               Puerto (por defecto 5000)
    FLASK_DEBUG=1      Modo desarrollo. NO usar en un servidor accesible desde
                       la red: el debugger de Werkzeug permite ejecutar código.
    HOST               Por defecto 127.0.0.1 (solo esta máquina).
"""

import os

from flask import Flask, jsonify, render_template, request

from catalog_db import inicializar_db
from price_checker import buscar

app = Flask(__name__)

# Crea las tablas si no existen. Sin esto, una instalación nueva reventaba
# con "no such table: productos" en la primera búsqueda (catalogo.db está
# en .gitignore, así que nunca viene con el repo).
inicializar_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/buscar")
def api_buscar():
    query = request.args.get("q", "").strip()

    if not query:
        return jsonify({"error": "Falta el término de búsqueda"}), 400

    try:
        reporte = buscar(query)
    except Exception as e:
        # Una tienda caída ya se maneja adentro; esto es para lo inesperado.
        # Se registra completo en consola y al vendedor le llega algo legible.
        app.logger.exception("Fallo la búsqueda de %r", query)
        return jsonify({"error": f"Error interno en la búsqueda ({type(e).__name__})"}), 500

    return jsonify(
        {
            "query": reporte["query"],
            "candidatos": reporte["candidatos"],
            "filtrado_por": reporte["filtrado_por"],
            "total": len(reporte["resultados"]),
            "resultados": [
                {
                    "tienda": r.tienda,
                    "nombre": r.nombre,
                    "precio": r.precio,
                    "precio_texto": r.precio_texto,
                    "url": r.url,
                    "sku": r.sku,
                    "ean": r.ean,
                }
                for r in reporte["resultados"]
            ],
        }
    )


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 5000))
    # 127.0.0.1 por defecto: la app queda accesible solo desde esta máquina.
    # Para exponerla en la red local de la tienda: HOST=0.0.0.0 y FLASK_DEBUG
    # apagado.
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug, host=host, port=puerto)
