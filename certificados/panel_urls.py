from django.urls import path
from . import panel_views as v

urlpatterns = [
    path("login/", v.panel_login, name="panel_login"),
    path("logout/", v.panel_logout_view, name="panel_logout"),
    path("", v.panel_dashboard, name="panel_dashboard"),

    # Events
    path("eventos/", v.panel_events, name="panel_events"),
    path("eventos/crear/", v.panel_event_form, name="panel_event_create"),
    path("eventos/<int:pk>/editar/", v.panel_event_form, name="panel_event_edit"),
    path("eventos/<int:pk>/toggle/", v.panel_event_toggle, name="panel_event_toggle"),
    path("eventos/<int:pk>/eliminar/", v.panel_event_delete, name="panel_event_delete"),

    # Templates
    path("templates/", v.panel_templates, name="panel_templates"),
    path("templates/crear/", v.panel_template_form, name="panel_template_create"),
    path("templates/<int:pk>/editar/", v.panel_template_form, name="panel_template_edit"),
    path("templates/<int:pk>/eliminar/", v.panel_template_delete, name="panel_template_delete"),
    path("templates/<int:pk>/preview/", v.panel_template_preview, name="panel_template_preview"),

    # Manual generation
    path("generar/", v.panel_generate, name="panel_generate"),
    path("generar/masivo/", v.panel_generate_bulk, name="panel_generate_bulk"),

    # Attendees (global)
    path("inscriptos/", v.panel_attendees_all, name="panel_attendees_all"),
    path("inscriptos/importar/", v.panel_attendees_import_all, name="panel_attendees_import_all"),

    # Attendees (per event)
    path("eventos/<int:event_pk>/inscriptos/", v.panel_attendees, name="panel_attendees"),
    path("eventos/<int:event_pk>/inscriptos/importar/", v.panel_attendees_import, name="panel_attendees_import"),
    path("eventos/<int:event_pk>/inscriptos/limpiar/", v.panel_attendees_clear, name="panel_attendees_clear"),
    path("eventos/<int:event_pk>/inscriptos/<int:pk>/eliminar/", v.panel_attendee_delete, name="panel_attendee_delete"),

    # Logs
    path("descargas/", v.panel_logs, name="panel_logs"),
    path("descargas/exportar/", v.panel_logs_export, name="panel_logs_export"),

    # Users
    path("usuarios/", v.panel_users, name="panel_users"),
    path("usuarios/crear/", v.panel_user_form, name="panel_user_create"),
    path("usuarios/<int:pk>/editar/", v.panel_user_form, name="panel_user_edit"),
    path("usuarios/<int:pk>/eliminar/", v.panel_user_delete, name="panel_user_delete"),
]
