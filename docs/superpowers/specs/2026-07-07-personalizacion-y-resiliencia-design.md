# Personalización de la web + resiliencia — Diseño

Fecha: 2026-07-07

## Objetivo

Cuatro cambios sobre la plataforma de certificados, antes de migrarla:

1. Look estándar claro (fondo blanco, mensaje en color) **editable desde el panel**.
2. Formulario con recuadro/bordes negros y errores de validación en rojo.
3. Lenguaje 100% neutro (infinitivo, sin voseo).
4. Mensaje ante servidor caído/sobrepasado: página de error + modo mantenimiento manual.

## 1. Apariencia editable

### Modelo `SiteSettings` (singleton)
Una sola fila (pk=1). Acceso vía `SiteSettings.load()` (get_or_create). Campos:

- `color_fondo` (`CharField`, default `#ffffff`) — fondo de las páginas públicas.
- `color_mensaje` (`CharField`, default `#1d4ed8`) — color del título/mensaje/acento.
- `titulo` (`CharField`, default `"Descargar certificado"`).
- `mensaje` (`TextField`) — subtítulo/instrucción del home.
- `mantenimiento` (`BooleanField`, default `False`).
- `mensaje_mantenimiento` (`TextField`, default el texto de "muchas visitas").

### Context processor
`certificados.context_processors.site_settings` inyecta `site` en todos los templates. Registrado en `TEMPLATES.OPTIONS.context_processors`.

### Panel
- Ruta `/panel/apariencia/` → `panel_site_settings` (login_required), patrón de `panel_event_form`.
- Template `panel/site_settings.html` con color pickers (`<input type=color>`) + textos + toggle mantenimiento.
- Link en `panel/base.html`, sección "Sistema", `active_page == 'apariencia'`.

## 2. Formulario: recuadro negro + errores rojos

En `home.html` y `event_page.html` (tema claro):
- Card/form dentro de recuadro `border: 2px solid #000`.
- Inputs/select con `border: 1.5px solid #000`.
- Validación: `input:user-invalid` → borde rojo `#dc2626`; mensajes de error del server ya renderizados en rojo (se mantienen). El título/mensaje usan `var(--color-mensaje)` desde `site`.

## 3. Lenguaje neutro

Reemplazar voseo por infinitivo/neutro en templates públicos y en los strings de `views.py`:
- "Elegí… ingresá…" → "Seleccionar… ingresar…"
- "Tenés que ingresar tu email." → "Ingresar el email."
- "No te encontramos en la lista… Verificá los datos." → "No figura en la lista de inscriptos. Verificar los datos."
- Duplicado y demás mensajes revisados uno por uno.

## 4. Servidor caído / sobrepasado

### (a) Página de error 500
- `handler500 = "certificados.views.server_error"` → renderiza `errors/500.html` con texto "Estamos recibiendo muchas visitas. Volver a intentar en unos minutos." (render manual, sin depender de context processors). Requiere `DEBUG=False`.

### (b) Modo mantenimiento manual
- Middleware `certificados.middleware.MaintenanceModeMiddleware`: si `SiteSettings.load().mantenimiento` está activo, responde HTTP 503 con `errors/maintenance.html` para rutas públicas. **Excluye** `/panel/` y `/admin/` (para poder apagarlo) y `/static/`, `/media/`.

### Límite conocido
Si el proceso está totalmente caído (no arranca), ni Django ni el middleware corren: eso requiere una página estática a nivel del hosting, a cablear al migrar. (a) y (b) son agnósticas del host.

## Archivos

**Nuevos:** `certificados/context_processors.py`, `certificados/middleware.py`, `certificados/migrations/000X_sitesettings.py`, `templates/panel/site_settings.html`, `templates/errors/500.html`, `templates/errors/maintenance.html`.

**Modificados:** `certificados/models.py`, `certificados/panel_views.py`, `certificados/panel_urls.py`, `certificados/templates/panel/base.html`, `certificados/templates/certificados/home.html`, `certificados/templates/certificados/event_page.html`, `certificados/views.py`, `config/settings.py`, `config/urls.py`.

## Tests
- `SiteSettings.load()` devuelve/crea singleton.
- Middleware: mantenimiento ON → 503 en `/`, 200/redirect en `/panel/login/`.
- Home renderiza con color de fondo del settings.
