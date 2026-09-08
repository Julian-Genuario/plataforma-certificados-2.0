# Envío del certificado por email (Opción A)

Fecha: 2026-09-08. Estado: aprobado en chat por Julián para arrancar con Gmail.

## Objetivo

Que la persona que descarga su certificado pueda pedir, además, una copia por
correo. El envío lo dispara la persona desde el formulario público, solo si
figura en la lista de inscriptos del evento. Volumen esperado: hasta 20.000
correos repartidos en las semanas del congreso (a pedido, no en masa).

## Decisiones tomadas

- **Disparo:** la persona, desde el formulario. No hay envío masivo desde el panel.
- **Flujo:** Opción A. "Descargar certificado" sigue siendo el botón principal;
  casilla "Enviarme también una copia por email", marcada por defecto.
- **Destinatario:** SIEMPRE el email que figura en la lista de inscriptos
  (`Attendee.email`), nunca el tipeado. Así nadie desvía certificados ajenos.
- **Un solo uso:** el correo NO consume la descarga única. Un inscripto recibe
  como máximo un correo por evento, salvo reenvío manual desde el panel.
- **Proveedor:** primera etapa con una casilla Gmail de prueba (SMTP + contraseña
  de aplicación, tope 500/día). Para el congreso, recomendación: **Postmark**
  (alternativa: Resend). El código es agnóstico: todo sale por SMTP configurado
  en `/etc/certificados.env`; cambiar de proveedor = cambiar 4 variables + restart.
- **Requisito no negociable para producción:** remitente en un dominio de Brisa
  con SPF, DKIM y DMARC cargados en su DNS. Sin eso, una parte importante va a spam.

## Flujo para la persona

1. Completa nombre, email y deja marcada la casilla. Toca "Descargar certificado".
2. Si está en la lista: se registra la descarga (como hoy) y, si la casilla
   estaba marcada, se encola un correo para `attendee.email`.
3. La pantalla "Certificado listo" muestra: "Te enviamos una copia a
   nombre@email.com. Si no llega, revisar correo no deseado." La pantalla no
   espera al envío: responde al instante.
4. Si ya existía un correo encolado/enviado para ese inscripto y evento, no se
   crea otro; la pantalla dice "Ya te enviamos una copia a ...".
5. Flujo directo (sin iframe): mismo comportamiento; el aviso no se ve porque
   la respuesta es el PDF, pero el correo sale igual.

## Componentes

### Modelo `EmailDelivery` (migración nueva)

| campo | tipo | notas |
|---|---|---|
| event | FK Event | |
| attendee | FK Attendee, null | SET_NULL si se reimporta la lista |
| to_email | EmailField | copia del email de la lista al momento de encolar |
| full_name | CharField | nombre que va en el PDF y en el saludo |
| status | `pending` / `sending` / `sent` / `failed` | índice |
| attempts | PositiveIntegerField | |
| last_error | TextField | mensaje SMTP resumido |
| created_at, sent_at, next_attempt_at | DateTime | |
| download_log | FK DownloadLog, null | trazabilidad |

Restricción: un solo registro `pending/sending/sent` por (event, attendee).
El reenvío manual crea uno nuevo marcando el anterior como `superseded`.

### Cola y worker

- Comando `manage.py send_certificate_emails --max N --workers 1`: toma hasta N
  `pending` con `next_attempt_at <= now` (SELECT ... FOR UPDATE SKIP LOCKED no
  existe en SQLite: se reserva con `UPDATE ... WHERE status='pending'` atómico
  por fila), genera el PDF con `build_pdf_bytes`, arma el correo y lo envía con
  `django.core.mail.EmailMultiAlternatives` sobre una conexión SMTP reutilizada.
- systemd `certificados-mailer.timer` cada 1 minuto (misma familia que watchdog
  y backup; unidades reproducibles en `deploy/`).
- Reintentos: 5 intentos con espera 1, 5, 15, 60, 180 minutos; después `failed`.
- Tope por minuto configurable (`MAIL_RATE_PER_MINUTE`, default 60) y tope
  diario (`MAIL_DAILY_CAP`, default 450 con Gmail) para no pasar el límite del
  proveedor; al llegar al tope, los pendientes esperan al día siguiente.
- Una entrega `sending` con más de 10 minutos se considera colgada y vuelve a
  `pending` (proceso muerto a mitad).

### El correo

- Remitente, nombre visible y asunto editables en Panel → Apariencia
  (`SiteSettings`: `mail_from_name`, `mail_from_email`, `mail_subject`,
  `mail_body`). Defaults: "Brisa Enfermeros" / asunto "Tu certificado del
  {evento}".
- Cuerpo HTML + texto plano: saludo con el nombre, nombre del evento, una
  línea de contexto, firma. Adjunto `certificado-<slug>.pdf` (~371 KB).
- Sin links de descarga en el correo: el adjunto es la entrega.
- `Reply-To` configurable (default: el remitente).

### Panel

- Sección nueva "Correos": lista con filtros por evento y estado, buscador
  por nombre/email, columna de último error, botón "Reenviar" (crea nueva
  entrega), export CSV.
- Dashboard: tarjeta "Correos enviados" + "fallidos".
- Apariencia: bloque "Correo" con remitente/asunto/cuerpo + botón "Enviar
  correo de prueba a ..." que encola una entrega al email que se indique.

### Configuración (env)

```
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=1
EMAIL_HOST_USER=<casilla>@gmail.com
EMAIL_HOST_PASSWORD=<contraseña de aplicación>
MAIL_RATE_PER_MINUTE=30
MAIL_DAILY_CAP=450
```

Para Postmark: host `smtp.postmarkapp.com`, usuario/clave = token del server,
`MAIL_DAILY_CAP=0` (sin tope). Nada más cambia.

### Protecciones y observabilidad

- Todo error SMTP queda en `last_error` y en el journal (`django.request` +
  logger `certificados.mail`).
- `verificar.sh` suma: timer del mailer activo y `Finished`; cero entregas
  `pending` con más de 30 minutos; cero `sending` con más de 10 minutos.
- Backup diario ya cubre la tabla nueva (misma DB).
- Modo mantenimiento: el worker sigue enviando (no depende del público).

## Fuera de alcance (por ahora)

- Envío masivo desde el panel.
- Procesar rebotes automáticamente (con Postmark se ve en su panel).
- Cambiar el proveedor: es configuración, no código.

## Pruebas

- Unitarias: encolado solo si está en lista y casilla marcada; destinatario =
  email de la lista; dedupe por (event, attendee); reintentos y backoff; tope
  diario; recuperación de `sending` colgados; comando con backend `locmem`.
- Integración en el VPS: envío real a 3 casillas (Gmail, Outlook, Yahoo) y
  verificación de que el adjunto abre y el nombre es correcto.
- Carga: encolar 500 y medir que el worker sostiene el tope por minuto sin
  errores; DB en WAL sin bloqueos.
