"""Side-effect-free bundled proof fixture for the capability lifecycle kernel."""

DISPOSAL_ORDER: list[str] = []


def base_value() -> str:
    return "capability-fixture-base"


def dependent_value() -> dict[str, object]:
    return {
        "result": "capability-fixture-ok",
        "disposal_order": tuple(DISPOSAL_ORDER),
    }


def dispose_base() -> bool:
    DISPOSAL_ORDER.append("homie.fixture.base")
    return True


def dispose_dependent() -> bool:
    DISPOSAL_ORDER.append("homie.fixture.dependent")
    return True


def register(registrar) -> None:
    registrar.publish(
        "homie.fixture.base",
        base_value,
        disposer=dispose_base,
        depends_on=(),
    )
    registrar.publish(
        "homie.fixture.dependent",
        dependent_value,
        disposer=dispose_dependent,
        depends_on=("homie.fixture.base",),
    )
