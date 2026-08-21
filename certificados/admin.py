from django.contrib import admin
from .models import Event, CertificateTemplate, DownloadLog, RejectedAttempt, Attendee, SuspiciousAttendee

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


@admin.register(RejectedAttempt)
class RejectedAttemptAdmin(admin.ModelAdmin):
    list_display = ("event", "name_entered", "email_entered", "reason", "created_at")
    list_filter = ("event", "reason", "created_at")
    search_fields = ("name_entered", "email_entered")


@admin.register(Attendee)
class AttendeeAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "event", "created_at")
    list_filter = ("event",)
    search_fields = ("full_name", "email")


@admin.register(SuspiciousAttendee)
class SuspiciousAttendeeAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "event", "reason", "created_at")
    list_filter = ("event", "reason")
    search_fields = ("full_name", "email")
