from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from certificados import views as cert_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("panel/", include("certificados.panel_urls")),
    path("", cert_views.home_page, name="home"),
    path("download/", cert_views.download_from_home, name="download_from_home"),
    path("e/", include("certificados.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
