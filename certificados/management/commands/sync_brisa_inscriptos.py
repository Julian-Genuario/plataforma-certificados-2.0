"""Agrega a los eventos configurados los nuevos suscriptos de brisaplus.

Lo lanza `certificados-sync.path` cada vez que notif-pagos deja un export nuevo
(cada 30 min y solo si entraron pagos de suscripción). Solo
agrega (nunca borra); ver certificados/sync_brisa.py.
"""
import sys

from django.core.management.base import BaseCommand

from certificados.sync_brisa import SyncError, sincronizar


class Command(BaseCommand):
    help = "Agrega a los eventos los nuevos suscriptos del export de brisaplus (solo altas)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo cuenta, no agrega nada.")
        parser.add_argument("--file", default=None, help="Export a usar (default BRISA_EXPORT_PATH).")

    def handle(self, *args, **opts):
        try:
            resultado = sincronizar(path=opts["file"], dry_run=opts["dry_run"])
        except SyncError as exc:
            self.stderr.write(f"sync: sin cambios: {exc}")
            sys.exit(1)
        prefijo = "sync (prueba, no se agregó nada):" if opts["dry_run"] else "sync:"
        detalle = " ".join(f"{slug}=+{altas}/sospechosos {sos}" for slug, (altas, sos) in resultado.items())
        self.stdout.write(f"{prefijo} {detalle}")
