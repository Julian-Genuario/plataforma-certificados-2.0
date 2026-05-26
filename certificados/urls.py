from django.urls import path
from . import views

urlpatterns = [
    path("e/<slug:slug>/", views.event_page, name="event_page"),
    path("e/<slug:slug>/download/", views.download_certificate, name="download_certificate"),
]