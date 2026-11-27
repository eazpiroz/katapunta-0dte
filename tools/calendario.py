"""Sesiones cortas de NYSE/Cboe (cierre 13:00 ET = 12:00 CDMX). Lo usan el recolector y el bot de papel.

Agregar aquí cada media sesión del año (día después de Acción de Gracias, Nochebuena, a veces 3 de julio),
según el calendario publicado por NYSE. Una fecha que falte se trata como sesión completa.
"""

MEDIAS_SESIONES = {
    "2026-11-27",   # viernes después de Acción de Gracias
    "2026-12-24",   # Nochebuena
}

CIERRE_CORTO = 12 * 60      # CDMX (13:00 ET)
CIERRE_NORMAL = 15 * 60     # CDMX (16:00 ET)


def media_sesion(fecha):
    return str(fecha)[:10] in MEDIAS_SESIONES


def cierre_cdmx(fecha):
    """Minuto CDMX del cierre de la sesión de esa fecha."""
    return CIERRE_CORTO if media_sesion(fecha) else CIERRE_NORMAL
