"""Notifications et prochaine relance à préparer."""
from fastapi import APIRouter

from .. import services
from .deps import ReadIn

router = APIRouter(tags=["notifications"])


@router.get("/api/notifications")
def list_notifications():
    return services.list_notifications()


@router.post("/api/notifications/read")
def mark_notifications_read(body: ReadIn):
    services.mark_notifications_read(body.ids)
    return services.list_notifications()


@router.get("/api/next-followup")
def next_followup():
    return {"item": services.next_followup()}
