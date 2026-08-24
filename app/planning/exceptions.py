class UnsupportedQuerySemanticsError(ValueError):
    """Expected fail-closed result for user meaning we cannot safely execute."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(dict.fromkeys(reasons))
        super().__init__("unsupported query semantics")
