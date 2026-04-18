import sys
from app.main import app
for route in app.routes:
    if hasattr(route, "path") and "markers" in route.path:
        print(route.path, route.methods)
