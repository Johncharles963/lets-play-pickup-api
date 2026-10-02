import uuid
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.errors import ApiError, api_error_handler, error_body, http_error_handler, validation_error_handler
from app.routers import events, notifications, social


settings = get_settings()
app = FastAPI(title="Let's Play Pickup API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "If-Match", "X-Request-ID"],
    expose_headers=["ETag", "X-Request-ID"],
)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logging.getLogger("pickup_api").exception(
        "Unhandled request error (request_id=%s)",
        getattr(request.state, "request_id", "unknown"),
    )
    return JSONResponse(
        status_code=500,
        content=error_body(request, "INTERNAL_ERROR", "An unexpected server error occurred."),
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID")
    if not request_id or len(request_id) > 128:
        request_id = f"req_{uuid.uuid4().hex}"
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(events.router)
app.include_router(social.router)
app.include_router(notifications.router)
