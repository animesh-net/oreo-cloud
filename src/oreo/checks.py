"""Result type shared by every check."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def all_ok(checks: list[Check]) -> bool:
    return all(check.ok for check in checks)
