from .models import Event, SiteSettings


def site_settings(request):
    """Inyecta la configuración del sitio en todos los templates como ``site``
    y, para el panel, los eventos con el tilde de modo prueba activo."""
    ctx = {"site": SiteSettings.load()}
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        ctx["events_in_test_mode"] = list(Event.objects.filter(test_mode=True).values_list("name", flat=True))
    return ctx
