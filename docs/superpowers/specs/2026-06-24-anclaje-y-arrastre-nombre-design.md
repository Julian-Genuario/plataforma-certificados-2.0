# Posicionamiento del nombre: arrastre, flechas y anclaje vertical

**Fecha:** 2026-06-24
**Estado:** Aprobado, pendiente de implementación

## Problema

En el editor de templates (`template_form.html`) el nombre del asistente se
posiciona sólo con un clic en el PDF, que setea X/Y. Es engorroso (se clickea a
tientas) y el resultado queda feo respecto del renglón impreso.

Raíz técnica: `c.drawString(draw_x, y, full_name)` ancla el texto por su
**baseline**. El clic define la base de las letras, no el centro ni el tope, así
que sobre un renglón el nombre queda flotando o pisando la línea, y no hay forma
de empujarlo con precisión ni de cambiar el anclaje.

## Alcance (combo A aprobado)

Tres mejoras. Nada más (sin snap automático, sin regla/guías, sin presets de
centrado vertical).

### 1. Arrastrar el nombre (vista previa en vivo)

El overlay (`#overlayCanvas`) pasa de escuchar `click` a un gesto de arrastre:

- `mousedown` sobre el PDF agarra el texto y setea X/Y en ese punto.
- `mousemove` (con botón presionado) actualiza X/Y en vivo y redibuja.
- `mouseup` suelta.
- Un clic sin desplazamiento = comportamiento actual (saltar a ese punto).

Cursor: `grab` en reposo, `grabbing` durante el arrastre.

### 2. Flechas del teclado (empuje fino)

Con el editor de previa enfocado, las flechas mueven el punto:

- ←/→ ajustan X; ↑/↓ ajustan Y.
- En coordenadas PDF (origen abajo-izquierda): ↑ **incrementa** Y, ↓ lo decrementa.
- Paso normal: 1 pt. Con `Shift`: 10 pt.
- Se previene el scroll de la página (`preventDefault`) mientras se nudgea.

### 3. Anclaje vertical

Campo nuevo en el modelo `CertificateTemplate`:

```python
valign = models.CharField(max_length=10, default="baseline")  # baseline / middle / top
```

Migración `0007`. Default `baseline` = comportamiento actual (no rompe templates
existentes).

Semántica: qué parte del texto cae sobre el punto Y.

- `baseline`: Y es la base de las letras (actual).
- `middle`: Y es el centro vertical del texto.
- `top`: Y es el tope del texto.

UI: un `<select>` nuevo ("Anclaje vertical") en el formulario, junto a la
alineación horizontal. Dispara `renderPreview()` al cambiar.

## Cálculo del offset de baseline

`drawString` siempre dibuja en el baseline. Para anclar arriba/centro se corre el
Y de dibujo usando las métricas de la fuente (Helvetica). Con
`reportlab.pdfbase.pdfmetrics.getFont(font).face`:

- `ascent_pt  = face.ascent  / 1000 * font_size`
- `descent_pt = face.descent / 1000 * font_size`  (descent es negativo)

`baseline_y` (Y que se pasa a `drawString`) según `valign`:

- `baseline`: `y`
- `top`:      `y - ascent_pt`
- `middle`:   `y - ascent_pt / 2`  (aprox. centro óptico usando ascent)

Helper Python compartido:

```python
def baseline_offset(font_name, font_size, valign):
    """Devuelve cuánto restar al Y para anclar el texto (0 = baseline)."""
```

En JS se replica la misma fórmula con constantes de Helvetica
(`ascent ≈ 0.718`, `descent ≈ -0.207` por unidad de em) para que la previa en
vivo coincida con el PDF.

## Lugares que tocar (consistencia previa = PDF real)

El nombre se dibuja en tres lados; los tres aplican el mismo offset:

1. `certificados/views.py` → `build_pdf_bytes` (certificado real). Usa
   `baseline_offset`.
2. `certificados/panel_views.py` → previa server-side (PNG/PDF de prueba). Usa
   `baseline_offset`. También: leer/guardar `valign` del POST y pasar
   `tpl_valign` al contexto.
3. `certificados/templates/panel/template_form.html` → previa en vivo (canvas):
   drag, flechas, select de anclaje, y offset en `drawOverlay()`.

## Modelo / migración

- Campo `valign` en `CertificateTemplate` (default `baseline`).
- Migración `0007_certificatetemplate_valign`.

## Testing

- `baseline_offset`: `baseline` → 0; `top` → `ascent_pt`; `middle` →
  `ascent_pt/2`; tamaños distintos escalan lineal.
- `build_pdf_bytes`: genera sin error con cada `valign`; el Y efectivo de
  `drawString` cambia según el anclaje (verificable mockeando o midiendo).
- Migración aplica y los templates existentes quedan en `baseline`.

## Fuera de alcance

Snap automático al renglón, regla/guías con coordenadas en mm, presets de
centrado vertical. Quedan para una iteración futura si se piden.
