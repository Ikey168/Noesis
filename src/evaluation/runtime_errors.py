"""Stable errors for optional execution without leaking provider payloads."""


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
