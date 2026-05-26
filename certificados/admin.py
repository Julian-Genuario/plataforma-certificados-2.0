from django.contrib import admin
from .models import Event, CertificateTemplate, DownloadLog, Attendee

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "active")
    prepopulated_fields = {"slug": ("name",)}

@admin.register(CertificateTemplate)
class CertificateTemplateAdmin(admin.ModelAdmin):
    list_display = ("event", "mode", "pdf")

@admin.register(DownloadLog)
class DownloadLogAdmin(admin.ModelAdmin):
    list_display = ("event", "name_entered", "manual", "created_at")
    list_filter = ("event", "manual", "created_at")
    search_fields = ("name_entered",)


@admin.register(Attendee)
class AttendeeAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "event", "created_at")
    list_filter = ("event",)
    search_fields = ("full_name", "email")
