"""Named model registrations and deterministic model selection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from gca.providers.base import LLMProvider

DEFAULT_CAPABILITIES = frozenset({"planning", "coding", "review", "tool_use"})
_VALID_STRATEGIES = {"efficient", "strongest"}


class ModelSelectionError(ValueError):
    """Raised when no registered model satisfies a routing request."""


@dataclass(frozen=True)
class ModelProfile:
    """A configured provider plus comparable routing metadata.

    Scores use a one-to-five scale. Higher ``strength`` and ``speed`` are better;
    lower ``cost`` is cheaper.
    """

    name: str
    provider: LLMProvider
    strength: int = 3
    speed: int = 3
    cost: int = 3
    capabilities: frozenset[str] = field(default_factory=lambda: DEFAULT_CAPABILITIES)
    model_id: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("model name must not be empty")
        if not isinstance(self.provider, LLMProvider):
            raise TypeError("provider must implement LLMProvider")
        for field_name in ("strength", "speed", "cost"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
                raise ValueError(f"{field_name} must be an integer from 1 to 5")
        if isinstance(self.capabilities, (str, bytes)):
            raise TypeError("capabilities must be a collection of names")
        capabilities = frozenset(self.capabilities)
        if not all(isinstance(capability, str) and capability for capability in capabilities):
            raise TypeError("capabilities must contain non-empty strings")
        object.__setattr__(self, "capabilities", capabilities)

    def supports(self, capability: str) -> bool:
        """Return whether this model can serve ``capability``."""

        return "*" in self.capabilities or capability in self.capabilities

    def metadata(self) -> dict[str, object]:
        """Return stable, non-secret metadata suitable for persistence."""

        return {
            "name": self.name,
            "strength": self.strength,
            "speed": self.speed,
            "cost": self.cost,
            "capabilities": sorted(self.capabilities),
            "model_id": self.model_id,
        }


class ModelRegistry:
    """A name-indexed collection of configured model providers."""

    def __init__(self) -> None:
        self._models: dict[str, ModelProfile] = {}

    def register(self, profile: ModelProfile) -> None:
        """Register ``profile``, replacing an earlier entry with the same name."""

        self._models[profile.name] = profile

    def get(self, name: str) -> ModelProfile | None:
        """Return a named profile, if registered."""

        return self._models.get(name)

    def names(self) -> list[str]:
        """Return registered names in deterministic order."""

        return sorted(self._models)

    def profiles(self) -> list[ModelProfile]:
        """Return profiles in deterministic name order."""

        return [self._models[name] for name in self.names()]

    def select(
        self,
        *,
        capability: str,
        strategy: str,
        min_strength: int = 1,
        preferred: str | None = None,
        additional_capabilities: frozenset[str] = frozenset(),
    ) -> ModelProfile:
        """Select a model for a role.

        An explicit ``preferred`` name wins. Otherwise ``strongest`` maximizes
        strength, while ``efficient`` favors lower cost and then higher speed.
        """

        if strategy not in _VALID_STRATEGIES:
            raise ValueError(f"unknown model selection strategy: {strategy}")
        if not 1 <= min_strength <= 5:
            raise ValueError("min_strength must be from 1 to 5")

        required_capabilities = {capability, *additional_capabilities}

        if preferred:
            profile = self.get(preferred)
            if profile is None:
                available = ", ".join(self.names()) or "none"
                raise ModelSelectionError(
                    f"unknown preferred model '{preferred}' (available: {available})"
                )
            missing = sorted(
                required for required in required_capabilities if not profile.supports(required)
            )
            if missing:
                raise ModelSelectionError(
                    f"model '{preferred}' does not support: {', '.join(missing)}"
                )
            if profile.strength < min_strength:
                raise ModelSelectionError(
                    f"model '{preferred}' has strength {profile.strength}, below "
                    f"the required minimum {min_strength}"
                )
            return profile

        candidates = [
            profile
            for profile in self._models.values()
            if profile.strength >= min_strength
            and all(profile.supports(required) for required in required_capabilities)
        ]
        if not candidates:
            available = ", ".join(self.names()) or "none"
            raise ModelSelectionError(
                f"no model supports {', '.join(sorted(required_capabilities))} "
                f"with strength >= {min_strength} "
                f"(available: {available})"
            )

        if strategy == "strongest":
            return min(
                candidates,
                key=lambda profile: (
                    -profile.strength,
                    profile.cost,
                    -profile.speed,
                    profile.name,
                ),
            )
        return min(
            candidates,
            key=lambda profile: (
                profile.cost,
                -profile.speed,
                -profile.strength,
                profile.name,
            ),
        )

    def fingerprint(self) -> str:
        """Return a stable fingerprint of registered routing metadata."""

        encoded = json.dumps(
            [profile.metadata() for profile in self.profiles()],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def __len__(self) -> int:
        return len(self._models)

    def __contains__(self, name: object) -> bool:
        return name in self._models
