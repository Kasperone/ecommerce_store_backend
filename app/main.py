from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="E-commerce API built with FastAPI",
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "message": "E-commerce API is running",
        "version": settings.APP_VERSION,
        "status": "healthy"
    }

@app.get("/health")
async def health():
    """Health check for monitoring"""
    return {"status": "ok"}


# Import routers (to be added later)
# from app.api.v1 import products, auth, orders
# app.include_router(products.router, prefix="/api/v1", tags=["products"])
# app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
