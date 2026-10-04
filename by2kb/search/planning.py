from by2kb.integrations.hermes.search_planning import (
    PLAN_PROMPT, clean_query, fallback_plan, intent_score, validate_plan as _validate_plan,
)
from by2kb.errors import ConfigError


def validate_plan(value, topic):
    try:
        return _validate_plan(value, topic)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
