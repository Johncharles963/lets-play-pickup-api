from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_body(
    request: Request,
    code: str,
    message: str,
    details: dict | list[dict] | None = None,
) -> dict:
    error = {
        "code": code,
        "message": message,
        "requestId": getattr(request.state, "request_id", "unknown"),
    }
    if details:
        error["details"] = details
    return {"error": error}


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(request, exc.code, exc.message, exc.details),
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
    message = str(exc.detail) if isinstance(exc.detail, str) else "The request could not be completed."
    return JSONResponse(status_code=exc.status_code, content=error_body(request, code, message))


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"field": ".".join(str(part) for part in issue["loc"]), "message": issue["msg"]}
        for issue in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=error_body(request, "VALIDATION_ERROR", "Request validation failed.", details),
    )
