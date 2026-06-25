# Diseño: límite de descargas configurable, mensaje editable e informe PDF

**Fecha:** 2026-06-25
**Estado:** aprobado para implementación

## Contexto

Hoy la plataforma de certificados bloquea cada certificado tras **una sola**
descarga pública por persona (`already_downloaded`, booleano, en
`views.py:194-214`) y muestra un mensaje **fijo en código** (`DUPLICATE_MESSAGE`,
`views.py:24-27`). El dashboard ya exporta CSV de descargas y de intentos
rechazados, pero no hay un informe-resumen presentable.

Tres mejoras pedidas, todas operables desde el panel:

1. Elegir cuántas veces puede descargar una persona antes del bloqueo.
2. Editar el texto que se muestra al llegar al límite.
3. Exportar un informe PDF del dashboard.

## 1. Límite de descargas configurable por evento

**Modelo.** Campo nuevo en `Event`:

```python
download_limit = models.PositiveIntegerField(
    default=1,
    help_text="Cuántas descargas públicas puede hacer la misma persona "
              "antes de bloquearse. 0 = sin límite.",
)
```

- `1` (default) = comportamiento actual.
- `0` = ilimitado (nunca bloquea).
- `N` = bloquea cuando ya existen N descargas públicas de esa identidad.

**Lógica** (`_build_certificate_response` en `views.py`). Se reemplaza el
booleano `already_downloaded` por un **conteo** sobre el mismo queryset de
identidad que ya se arma (FK + email/nombre normalizado). El bloqueo aplica
solo si `event.download_limit != 0 and prior_count >= event.download_limit`.
Las descargas manuales (`manual=True`) siguen excluidas del conteo, igual que
hoy.

## 2. Mensaje de límite alcanzado, editable

**Modelo.** Campo nuevo en `Event`:

```python
duplicate_message = models.TextField(
    blank=True,
    default="",
    help_text="Mensaje que se muestra cuando la persona alcanza el límite "
              "de descargas. Vacío = texto por defecto.",
)
```

**Uso.** La constante `DUPLICATE_MESSAGE` pasa a ser el **default**: en el
`_fail` por motivo `duplicate` se usa `event.duplicate_message or DUPLICATE_MESSAGE`.
No se cambia el `reason="duplicate"` ni las métricas del dashboard.

## 3. Informe PDF del dashboard

**Vista nueva** `panel_report_pdf` (`@login_required`), ruta
`/panel/informe.pdf`, botón "Exportar informe" en `dashboard.html`.

Genera un PDF con **reportlab** (ya es dependencia) que contiene:

- Encabezado: título "Informe de certificados", fecha/hora de generación.
- Bloque de totales: eventos (totales/activos), descargas (totales/hoy/manuales),
  rechazados (total/hoy), duplicados.
- Tabla de los **últimos 7 días** (fecha → descargas), reusando el cálculo que
  ya hace `panel_dashboard`.
- Tabla **por evento**: nombre, descargas, rechazados, inscriptos.

El cálculo de métricas se extrae a un helper compartido para no duplicar lógica
entre `panel_dashboard` y `panel_report_pdf`.

## UI del panel

- `event_form.html`: agregar input numérico `download_limit` y `<textarea>`
  `duplicate_message`, con sus labels y ayudas.
- `panel_event_form` (POST): leer y guardar ambos campos (parseo seguro del
  entero, default 1 si inválido).
- `dashboard.html`: botón/enlace "Exportar informe" → `panel_report_pdf`.

## Migración

Una sola migración (`0008`) con los dos campos nuevos de `Event`. Defaults
seguros, sin backfill. **Recordatorio de deploy:** en PythonAnywhere falta
`git pull` + `python manage.py migrate` + Reload (y todavía está pendiente
deployar 0007/valign del commit anterior).

## Testing

- `download_limit`: con límite 1 bloquea en la 2ª descarga; con 2 permite 2 y
  bloquea la 3ª; con 0 nunca bloquea; manuales no cuentan.
- `duplicate_message`: vacío → texto por defecto; con valor → texto custom en el
  `messages.error`.
- `panel_report_pdf`: responde 200, `content_type=application/pdf`, no rompe sin
  datos.

## Fuera de alcance (YAGNI)

- Límite por IP o ventana temporal (solo por identidad nombre/email).
- Personalizar otros mensajes de rechazo (solo el de límite).
- Gráficos en el PDF (tablas alcanzan para el resumen).
