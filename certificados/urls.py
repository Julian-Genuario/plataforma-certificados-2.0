from django.urls import path
from . import views

urlpatterns = [
    path("<slug:slug>/", views.event_page, name="event_page"),
    path("<slug:slug>/download/", views.download_certificate, name="download_certificate"),
]