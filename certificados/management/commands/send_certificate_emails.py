"""Envía una tanda de correos pendientes con el certificado adjunto.

Lo lanza el timer systemd `certificados-mailer.timer` cada minuto. Cada
corrida respeta MAIL_RATE_PER_MINUTE y MAIL_DAILY_CAP (settings/env).
"""
from django.core.management.base import BaseCommand

from certificados.mailer import process_queue


class Command(BaseCommand):
    help = "Envía los correos pendientes con el certificado adjunto (una tanda)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max", type=int, default=None, help="Tope de correos en esta corrida."
        )

    def handle(self, *args, **opts):
        result = process_queue(max_items=opts["max"])
        self.stdout.write(" ".join(f"{k}={v}" for k, v in result.items()))
