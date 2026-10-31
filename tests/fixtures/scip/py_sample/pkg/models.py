class User:
    def save(self) -> None:
        # persist the user
        ...


class Logger:
    # Same method name as User.save above. Two `save` definitions are the
    # collision that defeats name-based (tree-sitter) call resolution;
    # SCIP resolves each call site to exactly one of them.
    def save(self) -> None:
        # flush the log
        ...
