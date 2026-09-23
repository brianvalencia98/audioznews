"""Publica en Telegram las entradas nuevas de un RSS.

Diseñado para ejecutarse una vez por GitHub Actions. El estado se guarda en
estado.json para que el workflow pueda versionarlo después de cada ejecución.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import requests


DIRECTORIO_BASE = Path(__file__).resolve().parent
ARCHIVO_ESTADO = DIRECTORIO_BASE / "estado.json"
TIMEOUT = 30
MAXIMO_IDS_GUARDADOS = 10_000
PATRON_FICHA_TECNICA = re.compile(
    r"^\s*(?P<ficha>[^<\n]+?\|\s*\d[\d.,]*\s*(?:KB|MB|GB|TB))\b",
    re.IGNORECASE,
)
PATRON_FECHA = re.compile(
    r"\d{1,2}\s+[A-Za-z]+\s+\d{4}|\d{4}[./-]\d{1,2}[./-]\d{1,2}",
    re.IGNORECASE,
)
PATRON_HOME_PAGE_ENCABEZADO = re.compile(
    r"<h[1-6]\b[^>]*>\s*(?:home\s*page|homepage)\s*</h[1-6]>\s*"
    r"<a\b[^>]*\bhref\s*=\s*[\"'](?P<url>[^\"']+)",
    re.IGNORECASE,
)
PATRON_ENLACE_HTML = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*[\"'](?P<url>[^\"']+)[\"'][^>]*>"
    r"(?P<texto>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


class ExtractorContenido(HTMLParser):
    """Convierte un fragmento HTML del RSS en texto y obtiene su primera imagen."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self.imagen: str | None = None
        self._ignorar = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        atributos = dict(attrs)
        if tag in {"script", "style"}:
            self._ignorar += 1
        elif tag == "img" and self.imagen is None:
            self.imagen = atributos.get("src") or atributos.get("data-src")
        elif tag in {"br", "p", "div", "li"}:
            self.partes.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._ignorar:
            self._ignorar -= 1
        elif tag in {"p", "div", "li"}:
            self.partes.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignorar:
            self.partes.append(data)

    @property
    def texto(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.partes)).strip()


def estado_vacio() -> dict[str, Any]:
    return {"initialized": False, "sent_ids": []}


def cargar_estado() -> dict[str, Any]:
    """Lee el estado sin modificarlo si está dañado."""
    if not ARCHIVO_ESTADO.exists():
        return estado_vacio()

    try:
        with ARCHIVO_ESTADO.open("r", encoding="utf-8") as archivo:
            contenido = json.load(archivo)
        if not isinstance(contenido, dict):
            raise ValueError("el contenido debe ser un objeto JSON")

        # Compatibilidad con una versión previa del proyecto.
        ids = contenido.get("sent_ids", contenido.get("ids_enviados", []))
        if not isinstance(ids, list):
            raise ValueError("sent_ids debe ser una lista")

        return {
            "initialized": bool(
                contenido.get("initialized", contenido.get("inicializado", False))
            ),
            "sent_ids": [str(valor) for valor in ids if str(valor).strip()],
        }
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(
            f"No se pudo leer {ARCHIVO_ESTADO.name}; no se modificará: {error}"
        ) from error


def guardar_estado(estado: dict[str, Any]) -> None:
    """Guarda el estado atómicamente para no corromperlo ante un corte."""
    ids = list(dict.fromkeys(estado["sent_ids"]))[-MAXIMO_IDS_GUARDADOS:]
    contenido = {"initialized": True, "sent_ids": ids}
    temporal = ARCHIVO_ESTADO.with_suffix(".json.tmp")

    try:
        with temporal.open("w", encoding="utf-8") as archivo:
            json.dump(contenido, archivo, ensure_ascii=False, indent=2)
            archivo.write("\n")
        temporal.replace(ARCHIVO_ESTADO)
    except OSError as error:
        raise RuntimeError(f"No se pudo guardar {ARCHIVO_ESTADO.name}: {error}") from error


def obtener_configuracion() -> tuple[str, str, str]:
    nombres = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "RSS_URL")
    valores = {nombre: os.getenv(nombre, "").strip() for nombre in nombres}
    faltantes = [nombre for nombre, valor in valores.items() if not valor]
    if faltantes:
        raise RuntimeError("Faltan variables de entorno: " + ", ".join(faltantes))

    rss_url = valores["RSS_URL"]
    if urlparse(rss_url).scheme not in {"http", "https"}:
        raise RuntimeError("RSS_URL debe comenzar con http:// o https://")

    return (
        valores["TELEGRAM_BOT_TOKEN"],
        valores["TELEGRAM_CHANNEL_ID"],
        rss_url,
    )


def es_url_web(valor: str | None) -> bool:
    return bool(valor and urlparse(valor).scheme.lower() in {"http", "https"})


def obtener_enlace(entrada: Any) -> str:
    enlace = str(entrada.get("link", "")).strip()
    return enlace if es_url_web(enlace) else ""


def obtener_entradas(rss_url: str) -> list[Any]:
    """Descarga y procesa el RSS con timeout y errores HTTP explícitos."""
    respuesta = requests.get(
        rss_url,
        headers={"User-Agent": "TelegramRSSGitHubActions/1.0"},
        timeout=TIMEOUT,
    )
    respuesta.raise_for_status()

    feed = feedparser.parse(respuesta.content)
    entradas = list(feed.entries)
    if getattr(feed, "bozo", False):
        detalle = getattr(feed, "bozo_exception", "RSS no válido")
        if not entradas:
            raise RuntimeError(f"No se pudo interpretar el RSS: {detalle}")
        logger.warning("El RSS contiene errores, pero se pudo leer: %s", detalle)

    return [entrada for entrada in entradas if obtener_enlace(entrada)]


def obtener_id(entrada: Any) -> str:
    identificador = entrada.get("id") or entrada.get("guid") or obtener_enlace(entrada)
    if identificador:
        return str(identificador).strip()

    base = "|".join(
        str(entrada.get(campo, ""))
        for campo in ("title", "published", "updated", "summary")
    )
    return "sha256:" + hashlib.sha256(base.encode("utf-8")).hexdigest()


def contenido_html(entrada: Any) -> str:
    for campo in ("summary", "description"):
        if entrada.get(campo):
            return str(entrada[campo])
    contenidos = entrada.get("content") or []
    if contenidos and contenidos[0].get("value"):
        return str(contenidos[0]["value"])
    return ""


def extraer_texto_e_imagen(fragmento_html: str) -> tuple[str, str | None]:
    extractor = ExtractorContenido()
    extractor.feed(fragmento_html or "")
    return html.unescape(extractor.texto), extractor.imagen


def obtener_imagen(entrada: Any, enlace: str, imagen_html: str | None) -> str | None:
    candidatos: list[str] = []
    for campo in ("media_content", "media_thumbnail"):
        for elemento in entrada.get(campo) or []:
            if elemento.get("url"):
                candidatos.append(str(elemento["url"]))
    for enclosure in entrada.get("enclosures") or []:
        tipo = str(enclosure.get("type", "")).lower()
        url = enclosure.get("href") or enclosure.get("url")
        if url and (tipo.startswith("image/") or not tipo):
            candidatos.append(str(url))
    if imagen_html:
        candidatos.append(imagen_html)

    for candidato in candidatos:
        imagen = urljoin(enlace, candidato.strip())
        if es_url_web(imagen):
            return imagen
    return None


def acortar(texto: str, limite: int) -> str:
    if len(texto) <= limite:
        return texto
    return texto[: limite - 1].rstrip() + "…"


def separar_ficha_tecnica(texto: str) -> tuple[str | None, list[str], str | None, str]:
    """Separa metadatos como grupo, fecha, formatos y tamaño del resumen."""
    coincidencia = PATRON_FICHA_TECNICA.match(texto)
    if not coincidencia:
        return None, [], None, texto

    partes = [parte.strip() for parte in coincidencia.group("ficha").split("|")]
    partes = [parte for parte in partes if parte]
    if len(partes) < 2:
        return None, [], None, texto

    descripcion = texto[coincidencia.end() :].lstrip(" |:-–—")
    return partes[0], partes[1:-1], partes[-1], descripcion


def quitar_etiqueta_home_page(texto: str) -> str:
    return re.sub(
        r"\s*(?:home\s*page|homepage)\s*$",
        "",
        texto,
        flags=re.IGNORECASE,
    ).strip()


def normalizar_pagina_oficial(candidato: str, enlace_publicacion: str) -> str | None:
    url = urljoin(enlace_publicacion, html.unescape(candidato).strip()).rstrip(".,;:)")
    if not es_url_web(url):
        return None
    if urlparse(url).netloc.lower() == urlparse(enlace_publicacion).netloc.lower():
        return None
    return url


def buscar_pagina_oficial_en_html(contenido: str, enlace_publicacion: str) -> str | None:
    """Busca el enlace «Home page» en HTML de RSS o en la página original."""
    candidatos: list[str] = []
    candidatos.extend(
        coincidencia.group("url")
        for coincidencia in PATRON_HOME_PAGE_ENCABEZADO.finditer(contenido)
    )

    for coincidencia in PATRON_ENLACE_HTML.finditer(contenido):
        texto_enlace = re.sub(r"<[^>]+>", "", coincidencia.group("texto"))
        texto_enlace = html.unescape(texto_enlace).strip().lower()
        if re.search(r"\bhome\s*page\b|\bhomepage\b", texto_enlace):
            candidatos.append(coincidencia.group("url"))

    texto_plano, _ = extraer_texto_e_imagen(contenido)
    coincidencia = re.search(
        r"(?:\bhome\s*page\b|\bhomepage\b)\s*(?:[:|–—-]\s*)?"
        r"(?P<url>https?://[^\s<]+)",
        texto_plano,
        re.IGNORECASE,
    )
    if coincidencia:
        candidatos.append(coincidencia.group("url"))

    for candidato in candidatos:
        pagina_oficial = normalizar_pagina_oficial(candidato, enlace_publicacion)
        if pagina_oficial:
            return pagina_oficial
    return None


def obtener_pagina_oficial(entrada: Any, enlace_publicacion: str) -> str | None:
    """Busca la web oficial en RSS y, si falta, en la publicación original."""
    pagina_oficial = buscar_pagina_oficial_en_html(
        contenido_html(entrada),
        enlace_publicacion,
    )
    if pagina_oficial:
        return pagina_oficial

    try:
        respuesta = requests.get(
            enlace_publicacion,
            headers={"User-Agent": "TelegramRSSGitHubActions/1.0"},
            timeout=TIMEOUT,
        )
        respuesta.raise_for_status()
        return buscar_pagina_oficial_en_html(respuesta.text, enlace_publicacion)
    except requests.RequestException as error:
        logger.warning(
            "No se pudo buscar Página oficial en %s: %s",
            enlace_publicacion,
            error,
        )
        return None


def traducir_resumen(texto: str) -> str:
    """Traduce solo el resumen al español y conserva el original si falla."""
    texto = acortar(texto.strip(), 4_500)
    if not texto:
        return texto

    try:
        from deep_translator import GoogleTranslator, MyMemoryTranslator

        try:
            traduccion = GoogleTranslator(source="auto", target="es").translate(texto)
        except Exception as error_google:
            logger.info(
                "Google Translate no está disponible (%s); se usará MyMemory.",
                str(error_google).splitlines()[0][:300],
            )
            # Esta fuente publica las descripciones principalmente en inglés.
            traduccion = MyMemoryTranslator(source="en-GB", target="es-ES").translate(
                texto
            )
        return traduccion.strip() or texto
    except Exception as error:
        logger.warning(
            "No se pudo traducir el resumen; se enviará original: %s",
            str(error).splitlines()[0][:300],
        )
        return texto


def crear_ficha_tecnica(grupo: str | None, datos: list[str], peso: str | None) -> str:
    if not grupo or not peso:
        return ""

    datos_formateados = [
        f"📅 {dato}" if PATRON_FECHA.search(dato) else dato for dato in datos
    ]
    primera_linea = f"💿 <b>{html.escape(grupo)}</b>"
    if datos_formateados:
        primera_linea += "  ·  " + "  ·  ".join(
            html.escape(dato) for dato in datos_formateados
        )
    return f"<blockquote>{primera_linea}\n💾 {html.escape(peso)}</blockquote>\n\n"


def crear_mensaje(
    entrada: Any,
    para_foto: bool = False,
    resumen_traducido: str | None = None,
    pagina_oficial: str | None = None,
) -> str:
    enlace = obtener_enlace(entrada)
    titulo = acortar(str(entrada.get("title") or "Sin título").strip(), 250)
    resumen, _ = extraer_texto_e_imagen(contenido_html(entrada))
    grupo, datos_ficha, peso, resumen = separar_ficha_tecnica(resumen)
    resumen = quitar_etiqueta_home_page(resumen)
    if resumen_traducido is not None:
        resumen = resumen_traducido
    resumen = resumen or "Sin resumen disponible."
    resumen = acortar(resumen, 600 if para_foto else 3_500)
    ficha_tecnica = crear_ficha_tecnica(grupo, datos_ficha, peso)
    bloque_pagina_oficial = ""
    if pagina_oficial:
        bloque_pagina_oficial = (
            f'🌐 <a href="{html.escape(pagina_oficial, quote=True)}">Página oficial</a>\n\n'
        )

    return (
        f"<b>{html.escape(titulo)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ficha_tecnica}"
        f"📝 {html.escape(resumen)}\n\n"
        f"{bloque_pagina_oficial}"
        f'🔗 <a href="{html.escape(enlace, quote=True)}">Abrir publicación</a>'
    )


def llamar_telegram(token: str, metodo: str, datos: dict[str, Any]) -> None:
    respuesta = requests.post(
        f"https://api.telegram.org/bot{token}/{metodo}",
        data=datos,
        timeout=TIMEOUT,
    )
    respuesta.raise_for_status()
    try:
        resultado = respuesta.json()
    except ValueError as error:
        raise RuntimeError("Telegram devolvió una respuesta no válida") from error
    if not resultado.get("ok"):
        raise RuntimeError(f"Telegram rechazó la solicitud: {resultado.get('description')}")


def enviar_publicacion(token: str, canal_id: str, entrada: Any) -> None:
    enlace = obtener_enlace(entrada)
    resumen_original, imagen_html = extraer_texto_e_imagen(contenido_html(entrada))
    _, _, _, resumen_original = separar_ficha_tecnica(resumen_original)
    resumen_original = quitar_etiqueta_home_page(resumen_original)
    resumen_traducido = traducir_resumen(resumen_original)
    pagina_oficial = obtener_pagina_oficial(entrada, enlace)
    imagen = obtener_imagen(entrada, enlace, imagen_html)

    if imagen:
        try:
            llamar_telegram(
                token,
                "sendPhoto",
                {
                    "chat_id": canal_id,
                    "photo": imagen,
                    "caption": crear_mensaje(
                        entrada,
                        para_foto=True,
                        resumen_traducido=resumen_traducido,
                        pagina_oficial=pagina_oficial,
                    ),
                    "parse_mode": "HTML",
                },
            )
            return
        except (requests.RequestException, RuntimeError) as error:
            logger.warning(
                "No se pudo enviar la imagen de %s; se intentará como texto: %s",
                enlace,
                error,
            )

    llamar_telegram(
        token,
        "sendMessage",
        {
            "chat_id": canal_id,
            "text": crear_mensaje(
                entrada,
                resumen_traducido=resumen_traducido,
                pagina_oficial=pagina_oficial,
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )


def publicaciones_nuevas(entradas: list[Any], ids_enviados: set[str]) -> list[Any]:
    nuevas: list[Any] = []
    ids_en_esta_revision: set[str] = set()
    for entrada in entradas:
        identificador = obtener_id(entrada)
        if identificador not in ids_enviados and identificador not in ids_en_esta_revision:
            nuevas.append(entrada)
            ids_en_esta_revision.add(identificador)

    # Los feeds suelen traer lo más reciente primero: se envía de antigua a nueva.
    return list(reversed(nuevas))


def ejecutar() -> None:
    token, canal_id, rss_url = obtener_configuracion()
    estado = cargar_estado()
    entradas = obtener_entradas(rss_url)

    if not estado["initialized"]:
        estado["sent_ids"] = [obtener_id(entrada) for entrada in entradas]
        guardar_estado(estado)
        logger.info(
            "Primera ejecución: se registraron %d entrada(s) y no se envió contenido antiguo.",
            len(entradas),
        )
        return

    ids_enviados = set(estado["sent_ids"])
    nuevas = publicaciones_nuevas(entradas, ids_enviados)
    if not nuevas:
        logger.info("RSS revisado: no hay publicaciones nuevas.")
        return

    for entrada in nuevas:
        identificador = obtener_id(entrada)
        try:
            enviar_publicacion(token, canal_id, entrada)
            estado["sent_ids"].append(identificador)
            guardar_estado(estado)
            logger.info("Publicación enviada: %s", obtener_enlace(entrada))
        except (requests.RequestException, RuntimeError) as error:
            # No se guarda el ID: GitHub Actions lo reintentará más adelante.
            logger.error("No se pudo enviar %s: %s", obtener_enlace(entrada), error)
            break


def main() -> int:
    try:
        ejecutar()
        return 0
    except (requests.RequestException, RuntimeError) as error:
        logger.error("Error: %s", error)
        return 1
    except Exception:
        logger.exception("Error inesperado; estado.json se conserva sin cambios.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
