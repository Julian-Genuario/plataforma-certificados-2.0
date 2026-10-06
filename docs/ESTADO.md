# Estado del proyecto (para retomar desde cualquier PC)

Actualizado: 2026-09-09. Este archivo resume lo que no se deduce del código.
No contiene claves: las credenciales viven solo en `/etc/certificados.env` del VPS.

## Producción

- URL: https://srv1812254.hstgr.cloud (VPS Hostinger KVM, IP 179.197.65.129, Ubuntu 24.04).
- Stack: Nginx → Gunicorn (12 workers, graceful-timeout 10 s, backlog 4096; systemd `certificados`) → Django en `/opt/certificados`
  (user `certif`), SQLite en WAL. Backups diarios 06:30 UTC en `/var/backups/certificados` (14 días).
- Timers systemd: `certificados-watchdog` (cada 1 min), `certificados-backup` (diario),
  `certificados-mailer` (cada 1 min, envía correos pendientes).
- Acceso: SSH como root con la clave `~/.ssh/hostinger_cert_vps` (existe solo en la PC
  original; desde otra PC hay que copiar la clave o cargar una nueva en el panel de Hostinger).
- Deploy: one-liner del README (sección "Deploy en el VPS"): termina en `systemctl reload certificados`
  (recarga SIN corte; `restart` solo si cambió el .service o el env). Después de CADA deploy correr
  `/opt/certificados/deploy/verificar.sh` → debe terminar en `RESULTADO: PASS`.
- Panel: `/panel/`, usuario `admin` (contraseña la tiene Julián).

## Evento real

- `congreso-test` = **X Congreso Brisa de Enfermería Latinoamérica 2026** (29-30 oct 2026).
  El slug NO se cambia: el iframe ya está pegado en brisaplus.com.
- 14.863 inscriptos cargados desde el export de brisaplus (formato .xls-HTML). 59 filas
  "sospechosas" en cola de revisión (Panel → Inscriptos → Revisar).
- Límite: 1 descarga por persona. Inscriptos de prueba: `juli@test.com` (Juli Test) y
  `julian.genuario@istea.com.ar` (Julián Genuario). Para re-probar, borrar su DownloadLog.
- Snippet para Brisa (sin link de respaldo):
  `<iframe src="https://srv1812254.hstgr.cloud/e/congreso-test/?embed=1" width="480" height="760" style="border:0;max-width:100%;" title="Descargar certificado"></iframe>`
  La página de Brisa todavía muestra el `<p>` viejo "Descargar el certificado desde este link":
  lo tiene que sacar Brisa.

## Flujo público (vigente desde 2026-09-09)

1. Form (nombre, email, casilla "Enviarme también una copia por email" marcada).
2. Si está en la lista → pantalla "Certificado listo" (vista previa JPEG + botón PDF +
   "Guardar en el teléfono"). Igual por iframe o por link directo.
3. El link del PDF es de UN solo uso (`DownloadLog.delivered_at`). Segundo toque → vuelve al
   form con "el link es de un solo uso". La gracia de 10 min solo re-ofrece si no se entregó.
4. Al descargar/compartir → "¡Felicitaciones! Descarga finalizada" y a los 15 s navega la
   ventana de arriba a https://www.brisaplus.com (constantes `POST_DOWNLOAD_REDIRECT_*` en views.py).
5. Copia por email: se encola `EmailDelivery` al email de la LISTA; el worker la manda en el
   minuto siguiente. No consume la descarga única.

## Correo (Opción A) — estado

- Implementado completo: cola, worker con reintentos (1/5/15/60/180 min), topes
  `MAIL_RATE_PER_MINUTE`=30 y `MAIL_DAILY_CAP`=280, Panel → Correos, Apariencia → bloque
  "Correo con el certificado" + correo de prueba. Diseño: `docs/superpowers/specs/`, plan:
  `docs/superpowers/plans/`.
- SMTP actual: **Brevo** (plan gratis, 300/día) configurado en el env del VPS. Cuenta Brevo
  registrada por Julián con julian.genuario@istea.com.ar (único remitente verificado).
- **Problema abierto:** Gmail marca los correos como spam/phishing y bloquea el adjunto porque
  el remitente (dominio istea.com.ar) no autoriza a Brevo (sin SPF/DKIM alineados).
  Solución: autenticar un dominio de BRISA en Brevo (Remitentes y dominios → Dominios →
  cargar DKIM/DMARC en el DNS de Brisa) y usar un remitente de ese dominio; cambiar la
  casilla en Panel → Apariencia. Para el congreso se recomendó Postmark (o Resend): solo
  cambia el env, no el código.

## Capacidad (medido 2026-10-06, prueba de 5.000 personas a la vez)

- Prueba: 5.000 personas arrancando en el mismo segundo, flujo completo (formulario →
  POST → vista previa JPEG → PDF), desde una PC en Buenos Aires contra el servidor real,
  con 5.000 inscriptos de prueba (`@loadtest.invalid`, borrados después).
- **Antes (commit d6afc8a): 155/5.000 completaron, 6.129 errores 502.** Causas: el watchdog
  reinició la app en plena carga (healthz encolado >10 s), la cola del socket de gunicorn
  (4096) y la del 443 de nginx (511) se llenaron, y la vista previa JPEG costaba ~147 ms
  de CPU por persona.
- **Después (commits 0188ab1 + d2e8921): 5.000/5.000, 0 errores**, ~20.000 pedidos en
  ~60 s (~400 req/s con la CPU al 98%), mediana 52 s / p99 63 s en el peor caso de todos
  en el mismo segundo. Watchdog sin intervenir, sin reciclado de workers.
- Costo por persona (CPU, VPS): formulario 8 ms, POST 10 ms, vista previa 15 ms
  (antes ~150), PDF 6 ms. Ya no hay un paso dominante.
- Cambios: `render_certificate_jpeg` (fondo cacheado por worker + franja del nombre),
  watchdog que no reinicia si la app usa CPU, gunicorn `--backlog 16384`
  `--max-requests 2000`, `net.core.somaxconn=65535` (deploy/99-certificados-sysctl.conf),
  nginx `listen 443 ssl backlog=65535`, `worker_connections 16384`,
  `worker_rlimit_nofile 65535`. verificar.sh controla todo eso (26 controles).
- Gráfico antes/después: `vps_antes_despues_5000.png` (Escritorio de Julián).

## Pendientes (decisión de Julián, en pausa)

- Guardia en "Limpiar lista": hoy lista vacía + `free_download=False` sigue siendo descarga
  libre (riesgo real; pasó el 03-09). Tests ya escritos en
  `docs/superpowers/plans/2026-09-08-fixes-pendientes-tests.py.txt` (también cubren
  `Attendee.download_limit` por inscripto y "el form conserva lo tipeado tras un error").
- Borrar el repo vacío `Julian-Genuario/certificados-backups` en GitHub (quedó sin contenido).
- Revisar los 59 sospechosos. Avisar a Brisa por el texto "en calidad de conferencista" del
  template (es su PDF).
- Bajar la cuenta vieja de PythonAnywhere. Crear el evento Vacunología cuando llegue.

## Maquetas

- `docs/maquetas/opciones_descarga_vs_email.html`: comparativa Opción A / Opción B (elegida A).
  Publicada también en https://certificados-opciones.vercel.app (proyecto Vercel
  `certificados-opciones`, deploy manual).

## Reglas de trabajo acordadas

- Cero errores: `verificar.sh` al inicio y al final de cada sesión y tras cada deploy.
- No cambiar nada en producción sin OK previo de Julián; de a un cambio por vez.
- Nunca subir la base de inscriptos a GitHub (ni a repos privados).
- Los `.sh/.service/.timer` van con LF y ejecutables (`.gitattributes` lo fuerza).
