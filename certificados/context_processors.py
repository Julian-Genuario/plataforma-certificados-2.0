from .models import SiteSettings


def site_settings(request):
    """Inyecta la configuración del sitio en todos los templates como ``site``."""
    return {"site": SiteSettings.load()}
