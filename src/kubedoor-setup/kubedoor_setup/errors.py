"""Public errors never include upstream response bodies or credentials."""


class SetupError(Exception):
    def __init__(self, message, status=400, code='setup_error'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
