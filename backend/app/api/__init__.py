from fastapi import APIRouter

from app.api import (
    auth,
    certifications,
    equipment,
    help,
    helpdesk,
    incoming,
    misc,
    movement,
    orders,
    plans,
    realtime,
    search,
    service_notes,
    substitutions,
    tasks,
    users,
    vshk,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(incoming.router)
api_router.include_router(movement.router)
api_router.include_router(equipment.router)
api_router.include_router(certifications.router)
api_router.include_router(users.self_router)  # после основного: /users/ad/* и /users/{id} матчатся первыми
api_router.include_router(orders.router)
api_router.include_router(tasks.router)
api_router.include_router(plans.router)
api_router.include_router(vshk.router)
api_router.include_router(service_notes.router)
api_router.include_router(substitutions.router)
api_router.include_router(help.router)
api_router.include_router(helpdesk.router)
api_router.include_router(search.router)
api_router.include_router(misc.notifications_router)
api_router.include_router(misc.audit_router)
api_router.include_router(misc.dashboard_router)
api_router.include_router(misc.meta_router)
api_router.include_router(misc.health_router)
api_router.include_router(misc.system_router)
api_router.include_router(realtime.router)

__all__ = ["api_router"]
