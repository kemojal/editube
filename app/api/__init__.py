from fastapi import APIRouter

from .routes import users, projects, videos, comments, notifications, activity_feeds, upload
# , 
# analytics
# google_account_integration

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(videos.router)
api_router.include_router(comments.router)
api_router.include_router(notifications.router)
api_router.include_router(activity_feeds.router)
api_router.include_router(upload.router)
# api_router.include_router(analytics.router)
# api_router.include_router(google_account_integration.router)
