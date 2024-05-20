from fastapi import APIRouter

from .routes import users, projects
# ,  videos, comments

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(projects.router)
# api_router.include_router(videos.router)
# api_router.include_router(comments.router)