from django.shortcuts import render

from .models import SiteSettings

# Prefijos que NUNCA se cortan por mantenimiento: el panel para poder
# apagarlo, el admin de Django, los archivos estáticos/media y el healthcheck
# (si se cortara, el watchdog reiniciaría el servicio en loop durante un
# mantenimiento manual).
_EXCLUDED_PREFIXES = ("/panel/", "/admin/", "/static/", "/media/", "/healthz")


class MaintenanceModeMiddleware:
    """Si el modo mantenimiento está activo, corta las páginas públicas con 503.

    Deja pasar el panel y el admin para poder desactivarlo.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if not path.startswith(_EXCLUDED_PREFIXES):
            settings = SiteSettings.load()
            if settings.mantenimiento:
                response = render(
                    request,
                    "errors/maintenance.html",
                    {"mensaje": settings.mensaje_mantenimiento, "site": settings},
                )
                response.status_code = 503
                return response
        return self.get_response(request)
