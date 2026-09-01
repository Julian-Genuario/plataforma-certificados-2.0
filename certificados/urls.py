from django.urls import path
from . import views

urlpatterns = [
    path("<slug:slug>/", views.event_page, name="event_page"),
    path("<slug:slug>/download/", views.download_certificate, name="download_certificate"),
    path("<slug:slug>/descargar/<str:token>/", views.download_token, name="download_token"),
    path("<slug:slug>/imagen/<str:token>/", views.download_image_token, name="download_image_token"),
]