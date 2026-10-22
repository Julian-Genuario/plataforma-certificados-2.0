"""Alta automática de inscriptos desde el export de usuarios de brisaplus.

notif-pagos baja el "EXPORTAR INSCRIPTOS" del back de Brisa+ cada 30 min y lo
deja en un archivo (BRISA_EXPORT_PATH). Acá se lee ese archivo y se AGREGAN a
los eventos configurados (BRISA_SYNC_EVENTS, slugs separados por coma) los
usuarios con "Activado Suscripción = SI" que todavía no están en la lista.

Nunca borra ni reemplaza: solo agrega. Mismas reglas que el import del panel
(_do_attendees_import): dedup por email normalizado y los nombres sospechosos
van a la cola de revisión en vez de crearse.
"""
import os
import time

from django.conf import settings
from django.db import transaction

from .attendees_io import ParseError, _FirstTableParser, _detect_subscription, _parse_rows, is_suspicious_name
from .models import Attendee, Event, SuspiciousAttendee, normalize_email, normalize_text


class SyncError(Exception):
    pass


def leer_export(path, max_age_min):
    """Devuelve la lista [(nombre, email)] de suscripciones activas del export."""
    try:
        antiguedad = (time.time() - os.path.getmtime(path)) / 60
    except OSError:
        raise SyncError(f"No existe el export {path}")
    if antiguedad > max_age_min:
        raise SyncError(f"El export tiene {antiguedad:.0f} min (máximo {max_age_min}); no se usa uno viejo")
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("windows-1252", "replace")
    parser = _FirstTableParser()
    parser.feed(text)
    if len(parser.rows) < 2:
        raise SyncError("El export vino sin filas")
    # Sin la columna de suscripción el filtro no se aplicaría y entraría todo el mundo.
    if _detect_subscription(parser.rows[0]) is None:
        raise SyncError("El export no tiene la columna 'Activado Suscripción'")
    try:
        clean, _errors, _inactivos = _parse_rows(parser.rows, only_active=True)
    except ParseError as exc:
        raise SyncError(str(exc))
    return clean


def agregar_inscriptos(event, filas, dry_run=False):
    """Agrega a `event` las filas cuyo email no está. Devuelve (altas, sospechosos)."""
    with transaction.atomic():
        existentes = set(event.attendees.values_list("email_normalized", flat=True))
        en_revision = {normalize_email(e) for e in event.suspicious_attendees.values_list("email", flat=True)}
        altas, sospechosos = [], []
        for nombre, email in filas:
            email_norm = normalize_email(email)
            if email_norm in existentes or email_norm in en_revision:
                continue
            es_sospechoso, motivo = is_suspicious_name(nombre)
            if es_sospechoso:
                en_revision.add(email_norm)
                sospechosos.append(SuspiciousAttendee(event=event, full_name=nombre, email=email, reason=motivo))
                continue
            existentes.add(email_norm)
            altas.append(Attendee(
                event=event,
                full_name=nombre,
                email=email,
                full_name_normalized=normalize_text(nombre),
                email_normalized=email_norm,
            ))
        if not dry_run:
            Attendee.objects.bulk_create(altas, batch_size=500)
            SuspiciousAttendee.objects.bulk_create(sospechosos, batch_size=500)
    return len(altas), len(sospechosos)


def sincronizar(path=None, slugs=None, dry_run=False):
    """Corre una pasada. Devuelve {slug: (altas, sospechosos)}.

    Si en un evento saldrían más altas que BRISA_SYNC_MAX_ALTAS, no se toca
    ninguno: un salto así indica un export raro y lo tiene que mirar alguien.
    """
    path = path or settings.BRISA_EXPORT_PATH
    slugs = slugs if slugs is not None else settings.BRISA_SYNC_EVENTS
    if not slugs:
        raise SyncError("No hay eventos configurados (BRISA_SYNC_EVENTS)")
    eventos = list(Event.objects.filter(slug__in=slugs))
    faltan = set(slugs) - {e.slug for e in eventos}
    if faltan:
        raise SyncError(f"Eventos inexistentes: {', '.join(sorted(faltan))}")
    filas = leer_export(path, settings.BRISA_EXPORT_MAX_AGE_MIN)

    previa = {e.slug: agregar_inscriptos(e, filas, dry_run=True) for e in eventos}
    tope = settings.BRISA_SYNC_MAX_ALTAS
    excedidos = [f"{slug} ({altas})" for slug, (altas, _s) in previa.items() if tope and altas > tope]
    if excedidos:
        raise SyncError(f"Demasiadas altas de una vez (tope {tope}): {', '.join(excedidos)}; no se agregó nada")
    if dry_run:
        return previa
    return {e.slug: agregar_inscriptos(e, filas) for e in eventos}
