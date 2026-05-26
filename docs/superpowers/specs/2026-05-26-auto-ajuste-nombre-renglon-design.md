# Auto-ajuste del nombre al renglón + posición manual

Fecha: 2026-05-26

## Problema
El nombre del participante se posiciona con coordenadas fijas (x, y, tamaño de
fuente, alineación). Como el tamaño es fijo, los nombres largos se desbordan del
renglón y los cortos quedan chicos: "no caen automáticamente en el renglón para
completar".

## Objetivo
Que el nombre caiga centrado y prolijo en el renglón sin tocar nada por persona,
manteniendo además la opción de posicionarlo a mano como hasta ahora.

## Solución
Agregar un ancho de renglón opcional al template. El tamaño de fuente cargado
pasa a ser el **máximo**; si el nombre no entra en el ancho, la fuente se reduce
automáticamente hasta que entra.

- `max_width = 0` → comportamiento actual (tamaño fijo, posición manual).
- `max_width > 0` → auto-ajuste: el nombre se reduce para entrar en ese ancho.

Ambos modos coexisten: las coordenadas, alineación y click-para-ubicar siguen
funcionando igual.

## Cambios
1. **Modelo** `CertificateTemplate`: nuevo campo `max_width` (FloatField,
   default 0). Migración.
2. **`views.fit_font_size(text, font_name, base_size, max_width, min_size=6.0)`**:
   helper puro. Devuelve `base_size` si `max_width <= 0` o el texto ya entra;
   si no, `base_size * (max_width / text_width)`, con piso `min_size`.
3. **Render coherente en 3 lugares** usando el tamaño ajustado:
   - `build_pdf_bytes` (PDF final).
   - `panel_template_preview` (PNG/PDF del servidor).
   - `drawOverlay` (preview JS en vivo), que además dibuja el recuadro del ancho.
4. **`template_form.html`**: input "Ancho del renglón (0 = fijo)" + botón
   "Centrar en la página" (setea x = ancho_pdf/2 y alineación centro).
5. **`panel_template_form`**: lee/guarda `max_width`; expone `tpl_max_width`.

## No incluye / no rompe
- El modo "campo rellenable" (field) no cambia.
- Default `max_width=0` ⇒ plantillas y certificados existentes se comportan igual.

## Testing
- `fit_font_size`: nombre largo achica por debajo del base; nombre corto queda
  en base; `max_width=0` devuelve base sin tocar; nunca por debajo de `min_size`.
- Los tests de descarga existentes siguen pasando.
