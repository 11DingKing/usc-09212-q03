"""领域错误类型，映射为 HTTP 状态码。"""


class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class NotFoundError(ApiError):
    status = 404
    code = "not_found"


class ConflictError(ApiError):
    status = 409
    code = "conflict"


class ValidationError(ApiError):
    status = 422
    code = "validation_error"
