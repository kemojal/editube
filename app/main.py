from fastapi import FastAPI
from .api import api_router

app = FastAPI()

app.include_router(api_router)

@app.get("/")
async def read_item():
    return {"hello word"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=True)