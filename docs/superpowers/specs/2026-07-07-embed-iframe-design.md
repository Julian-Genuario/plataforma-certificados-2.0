# Embeber el formulario público por iframe — Diseño

Fecha: 2026-07-07

## Objetivo

Permitir pegar el **formulario público de descarga** de certificados en otra web
mediante un `<iframe>`. Solo lo público (front); el panel/admin siguen protegidos.

## Alcance

- Embebible: `event_page` (por evento) y `home` (con selector). Panel/admin NO.
- Quién puede embeber: cualquier sitio (form público sin login; riesgo mínimo).
  Lista blanca de dominios queda como mejora futura.

## Diseño

### 1. Modo embed (`?embed=1`)
Las páginas públicas detectan `request.GET.embed`. En modo embed:
- `body`: sin centrado full-height, `min-height:0`, fondo transparente, sin padding
  exterior → el form se apoya en el fondo del sitio anfitrión.
- Se oculta el `.footer`.
- El recuadro negro del form se mantiene (es lo que se embebe).
La versión standalone queda igual.

### 2. Permitir framing solo en lo público
`@xframe_options_exempt` en `home_page`, `event_page`, `download_from_home`,
`download_certificate`. El resto (panel/admin) conserva `X-Frame-Options: DENY`
vía `XFrameOptionsMiddleware`.

### 3. Cookies para iframe cross-domain
En `if not DEBUG` de settings: `CSRF_COOKIE_SAMESITE="None"` y
`SESSION_COOKIE_SAMESITE="None"` (ya hay `*_COOKIE_SECURE=True`). Necesario para
que el CSRF y la descarga funcionen desde un iframe en otro dominio con HTTPS.
En dev (http) no se toca SameSite (None requiere Secure) → cross-domain se prueba
en el VPS; en local se prueba el render standalone del modo embed.

### 4. Generador de snippet en el panel
- `panel_event_form` (editar evento existente): arma URL absoluta
  `request.build_absolute_uri(reverse("event_page", slug) + "?embed=1")` y la
  pasa al template. `event_form.html` muestra un `<textarea readonly>` con el
  `<iframe …>` + botón **Copiar** (JS `navigator.clipboard`).
- `panel_site_settings` (Apariencia): igual pero para el home (`/?embed=1`).

Snippet generado:
```html
<iframe src="https://HOST/e/<slug>/?embed=1" width="480" height="640"
        style="border:0;max-width:100%;" title="Descargar certificado"></iframe>
```

## Archivos
- Modifico: `certificados/views.py` (decoradores + embed en contexto no hace falta,
  se usa request.GET en template), `config/settings.py`, `certificados/panel_views.py`,
  `certificados/templates/certificados/home.html`,
  `certificados/templates/certificados/event_page.html`,
  `certificados/templates/panel/event_form.html`,
  `certificados/templates/panel/site_settings.html`.

## Tests
- GET `/e/<slug>/?embed=1` → 200 y **sin** header `X-Frame-Options`.
- GET `/panel/login/` → conserva `X-Frame-Options`.
- Modo embed oculta el footer / aplica clase embed.
