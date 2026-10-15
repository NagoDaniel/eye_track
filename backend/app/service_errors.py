"""Errors raised by the admin services; the routes turn them into HTTP responses."""


class AdminError(Exception):
    status_code = 400


class NotFoundError(AdminError):
    status_code = 404


class ConflictError(AdminError):
    status_code = 409


class TooLargeError(AdminError):
    status_code = 413


class InvalidError(AdminError):
    status_code = 422
