"""Servidor MCP del BCRP. Empiecen aquí: buscar_series ya funciona; completen los TODO hasta que pasen las pruebas.

    uv run pytest                                           # el contrato: las 8 pruebas deben pasar
    uv run server.py                                        # el servidor, por stdio
    claude mcp add --scope project bcrp -- uv run server.py # conectarlo a Claude Code

Las cuentas las hace Python, no el modelo: así cada cifra sale de una llamada que se puede repetir.
"""
import json
import re
import time
import unicodedata
from calendar import monthrange
from datetime import date
from functools import cache
from pathlib import Path

import httpx
import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

AQUI = Path(__file__).parent
CATALOGO = AQUI / "datos" / "catalogo_bcrp.csv"
URL_CATALOGO = "https://estadisticas.bcrp.gob.pe/estadisticas/series/metadata"
API = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
SOLO_LECTURA = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP("bcrp", instructions=(
    "Series oficiales del Banco Central de Reserva del Perú (BCRP). Primero busca el código con buscar_series; "
    "luego usa resumen para variaciones, promedios y extremos, y serie para los datos. No calcules a mano: "
    "cita siempre el código de la serie y el periodo."))


def _sin_tildes(s):
    return unicodedata.normalize("NFD", str(s).lower()).encode("ascii", "ignore").decode()


@cache
def catalogo():
    """El catálogo oficial: 17 mil series en unos 7 MB. Se baja una vez y queda en datos/."""
    if not CATALOGO.exists():
        CATALOGO.parent.mkdir(exist_ok=True)
        r = httpx.get(URL_CATALOGO, timeout=120)
        r.raise_for_status()
        CATALOGO.write_text(r.content.decode("latin-1"), encoding="utf-8")
    df = pd.read_csv(CATALOGO, sep=";", dtype=str).fillna("")
    df = df.rename(columns={"Código de serie": "codigo", "Categoría de serie": "categoria", "Grupo de serie": "grupo",
                            "Nombre de serie": "nombre", "Frecuencia": "frecuencia",
                            "Fecha de actualización": "actualizada", "Fecha de inicio": "inicio"})
    df["texto"] = (df.categoria + " " + df.grupo + " " + df.nombre).map(_sin_tildes)
    return df


def _ficha(codigo):
    fila = catalogo().query("codigo == @codigo")
    if fila.empty:
        raise ToolError(f"No existe el código de serie {codigo!r}. Búsquenlo con buscar_series.")
    f = fila.iloc[0]
    return {"codigo": f.codigo, "nombre": f"{f.grupo} - {f.nombre}", "categoria": f.categoria,
            "frecuencia": f.frecuencia, "inicio": f.inicio, "actualizada": f.actualizada}


@mcp.tool(annotations=SOLO_LECTURA)
def buscar_series(texto: str, frecuencia: str | None = None, limite: int = 10) -> list[dict]:
    """Busca series del BCRP por palabras: todas deben aparecer en la categoría, el grupo o el nombre.

    Ejemplos: "ipc 12 meses", "tipo de cambio interbancario venta", "reservas internacionales netas",
    "tasa de referencia", "cobre precio". frecuencia: "Diaria", "Mensual", "Trimestral" o "Anual".
    Devuelve el código, el nombre completo, la frecuencia y desde cuándo hay datos. Si no hay resultados,
    prueben con menos palabras.
    """
    df = catalogo()
    m = pd.Series(True, index=df.index)
    for palabra in _sin_tildes(texto).split():
        m &= df.texto.str.contains(re.escape(palabra))
    if frecuencia:
        m &= df.frecuencia.str.lower() == frecuencia.lower()
    # primero las series vigentes y, entre ellas, las de nombre más corto (las más generales)
    sel = df[m]
    orden = sel.assign(vieja=sel.actualizada < "2025", largo=(sel.grupo + sel.nombre).str.len())
    return [_ficha(c) for c in orden.sort_values(["vieja", "largo"]).head(limite).codigo]


MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12}


def _anio(aa):
    """La API da años de dos dígitos en las series diarias y trimestrales: '26' -> 2026, '97' -> 1997."""
    a = 2000 + int(aa)
    return a if a <= date.today().year else a - 100


def _fecha_api(fecha, frecuencia, fin):
    """Traduce '2025-01', '2025-T1', '2026-10-01' o '2025' al formato que pide la API según la frecuencia.

    Si falta el mes o el día, completa con el comienzo (desde) o el final (hasta) del periodo.
    """
    m = re.fullmatch(r"(\d{4})(?:-T?(\d{1,2}))?(?:-(\d{1,2}))?", str(fecha).strip(), re.IGNORECASE)
    if not m:
        raise ToolError(f"Fecha inválida {fecha!r}. Usen 2025, 2025-01, 2025-T1 o 2025-01-15.")
    a, p, d = int(m[1]), m[2] and int(m[2]), m[3] and int(m[3])
    if frecuencia == "Anual":
        return f"{a}"
    if frecuencia == "Trimestral":
        return f"{a}-{p or (4 if fin else 1)}"
    if frecuencia == "Mensual":
        return f"{a}-{p or (12 if fin else 1)}"
    p = p or (12 if fin else 1)                                   # Diaria
    d = d or (monthrange(a, p)[1] if fin else 1)
    return f"{a}-{p:02d}-{d:02d}"


def _periodo(nombre, frecuencia):
    """Traduce el nombre que devuelve la API: 'Ene.2025' -> '2025-01', '01.Oct.26' -> '2026-10-01',
    'T1.25' -> '2025-T1', '2025' -> '2025'."""
    partes = nombre.split(".")
    if frecuencia == "Mensual":
        mes, a = partes
        return f"{a}-{MESES[mes.lower()]:02d}"
    if frecuencia == "Diaria":
        d, mes, aa = partes
        return f"{_anio(aa)}-{MESES[mes.lower()]:02d}-{int(d):02d}"
    if frecuencia == "Trimestral":
        t, aa = partes
        return f"{_anio(aa)}-{t.upper()}"
    return nombre


@cache
def _descargar(url):
    """Pide la URL a la API; si responde HTML (demasiados pedidos), espera y reintenta. Guarda en caché lo bajado."""
    for intento in range(4):
        r = httpx.get(url, timeout=60)
        try:
            return r.json()
        except ValueError:
            time.sleep(2 * (intento + 1))
    raise ToolError(f"La API del BCRP no respondió datos para {url}. Prueben de nuevo en unos segundos.")


@mcp.tool(annotations=SOLO_LECTURA)
def serie(codigo: str, desde: str, hasta: str) -> dict:
    """Descarga los datos de una serie del BCRP entre dos fechas, ambas incluidas.

    codigo: el que da buscar_series, por ejemplo "PN01271PM". Fechas según la frecuencia de la serie:
    "2025-01" (mensual), "2025-T1" (trimestral), "2026-10-01" (diaria) o "2025" (anual); con solo el año se
    toma el año completo. Devuelve los periodos en ese mismo formato, sin los días sin dato, y la URL de la
    fuente. Para variaciones, promedios o extremos usen resumen en vez de calcular.
    """
    ficha = _ficha(codigo)
    frec = ficha["frecuencia"]
    url = f"{API}/{codigo}/json/{_fecha_api(desde, frec, False)}/{_fecha_api(hasta, frec, True)}"
    datos = [{"periodo": _periodo(p["name"], frec), "valor": float(p["values"][0])}
             for p in _descargar(url).get("periods", [])
             if p["values"] and p["values"][0] not in ("n.d.", "")]
    return {"codigo": codigo, "nombre": ficha["nombre"], "frecuencia": frec, "datos": datos, "fuente": url}


def _datos(codigo, desde, hasta):
    s = serie(codigo, desde, hasta)
    if not s["datos"]:
        raise ToolError(f"La serie {codigo} no tiene datos entre {desde} y {hasta}.")
    return s


@mcp.tool(annotations=SOLO_LECTURA)
def resumen(codigo: str, desde: str, hasta: str) -> dict:
    """Resume una serie del BCRP entre dos fechas: primer y último dato, cambio absoluto, variación porcentual
    entre ambos, promedio, máximo y mínimo (con su periodo). Fechas como en serie.

    Úsenla siempre que la pregunta pida una variación, un promedio o un extremo: así ninguna cifra se calcula
    a mano. Ojo: si la serie ya es una variación % (por ejemplo el IPC a 12 meses), "cambio" está en puntos
    porcentuales y "variacion_pct" es la variación de esa tasa.
    """
    s = _datos(codigo, desde, hasta)
    df = pd.DataFrame(s["datos"])
    primero, ultimo = s["datos"][0], s["datos"][-1]
    maximo, minimo = df.loc[df.valor.idxmax()], df.loc[df.valor.idxmin()]
    cambio = ultimo["valor"] - primero["valor"]
    return {"codigo": codigo, "nombre": s["nombre"], "frecuencia": s["frecuencia"], "n": len(df),
            "primero": primero, "ultimo": ultimo,
            "cambio": round(cambio, 6),
            "variacion_pct": round(100 * cambio / abs(primero["valor"]), 4) if primero["valor"] else None,
            "promedio": round(df.valor.mean(), 6),
            "maximo": {"periodo": maximo.periodo, "valor": float(maximo.valor)},
            "minimo": {"periodo": minimo.periodo, "valor": float(minimo.valor)},
            "fuente": s["fuente"]}


@mcp.tool(annotations=SOLO_LECTURA)
def correlacion(codigo_a: str, codigo_b: str, desde: str, hasta: str) -> dict:
    """Correlación de Pearson entre dos series del BCRP de la misma frecuencia, usando solo los periodos en que
    ambas tienen dato. Fechas como en serie.

    Úsenla para preguntas como "¿se mueven juntos el tipo de cambio y las reservas?". Devuelve el coeficiente
    (entre -1 y 1), cuántos periodos se usaron y el rango común. Correlación no implica causalidad.
    """
    a, b = _datos(codigo_a, desde, hasta), _datos(codigo_b, desde, hasta)
    if a["frecuencia"] != b["frecuencia"]:
        raise ToolError(f"{codigo_a} es {a['frecuencia']} y {codigo_b} es {b['frecuencia']}: "
                        "elijan dos series de la misma frecuencia.")
    df = pd.DataFrame(a["datos"]).merge(pd.DataFrame(b["datos"]), on="periodo", suffixes=("_a", "_b"))
    if len(df) < 3:
        raise ToolError(f"Solo hay {len(df)} periodos en común: amplíen el rango de fechas.")
    return {"codigo_a": codigo_a, "nombre_a": a["nombre"], "codigo_b": codigo_b, "nombre_b": b["nombre"],
            "correlacion": round(float(df.valor_a.corr(df.valor_b)), 4), "n": len(df),
            "desde": df.periodo.iloc[0], "hasta": df.periodo.iloc[-1], "fuentes": [a["fuente"], b["fuente"]]}


@mcp.resource("bcrp://serie/{codigo}", mime_type="application/json")
def ficha_serie(codigo: str) -> str:
    """La ficha de una serie del BCRP: nombre, categoría, frecuencia, inicio y última actualización."""
    return json.dumps(_ficha(codigo), ensure_ascii=False)


@mcp.prompt
def informe(tema: str) -> str:
    """Pide un informe breve sobre un tema económico del Perú con datos oficiales del BCRP."""
    return (f"Escribe un informe breve sobre «{tema}» en el Perú con datos oficiales del BCRP.\n"
            "1. Busca las series pertinentes con buscar_series.\n"
            "2. Usa resumen para cada variación, promedio o extremo; no hagas cuentas a mano.\n"
            "3. Junto a cada cifra cita el código de la serie y el periodo, por ejemplo (PN01273PM, 2026-09).\n"
            "4. Si un dato no está en el BCRP, dilo; no lo estimes.")


if __name__ == "__main__":
    mcp.run()
