"""Scripted world timelines.

Used two ways: by ``DIRTY_SCENARIO=<name>`` to drive the mock hardware while
looking at the dashboard, and by the scenario test harness to assert on control
behaviour (docs/sop/SOP-003).

A scenario is a list of ``(at_seconds, description, apply)`` steps. Time is the
daemon's clock, so scenario tests run instantly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dirty.atomics.model import Mode
from dirty.logging import get_logger
from dirty.probe import Observation, ScriptedProber

log = get_logger("scenario")


@dataclass(frozen=True, slots=True)
class Step:
    at_s: float
    description: str
    apply: Callable[[ScenarioRunner], None]


class ScenarioRunner:
    def __init__(self, daemon, steps: list[Step]):
        self.daemon = daemon
        self.steps = sorted(steps, key=lambda s: s.at_s)
        self._done = 0
        self._start = daemon.clock.now()

    @property
    def prober(self) -> ScriptedProber:
        prober = self.daemon.prober
        if not isinstance(prober, ScriptedProber):
            raise TypeError("scenarios require a ScriptedProber")
        return prober

    def observe(self, atomic_id: str, **kwargs) -> None:
        self.prober.set(Observation(atomic_id=atomic_id, **kwargs))

    def find(self, label_fragment: str) -> str | None:
        """Atomic id by label fragment — scenarios read better than raw ids."""
        for atomic in self.daemon.registry.all():
            if label_fragment.lower() in atomic.label.lower():
                return atomic.id
        return None

    def set_mode(self, label_fragment: str, mode: Mode) -> None:
        atomic_id = self.find(label_fragment)
        if atomic_id:
            self.daemon.registry.set_mode(atomic_id, mode)

    def pump(self) -> None:
        """Apply any steps that have come due. Call once per daemon tick."""
        elapsed = self.daemon.clock.now() - self._start
        while self._done < len(self.steps) and self.steps[self._done].at_s <= elapsed:
            step = self.steps[self._done]
            self._done += 1
            log.info(
                "Scenario step",
                extra={
                    "workflow": "scenario",
                    "state": "processing",
                    "intent": "drive the control loop through scripted conditions",
                    "at_s": step.at_s,
                    "step": step.description,
                },
            )
            step.apply(self)


def moving_van() -> list[Step]:
    """The scenario the product exists to survive.

    Wi-Fi as NORMAL, phone tether as BACKUP. The Wi-Fi degrades badly, then
    disappears, then returns. Critical must stay usable, best-effort must absorb
    the damage, and BACKUP must stay at zero until critical genuinely cannot be
    met.
    """

    def setup(run: ScenarioRunner) -> None:
        run.daemon.discover_once()
        run.set_mode("Hotel", Mode.NORMAL)
        run.set_mode("Phone", Mode.BACKUP)
        hotel = run.find("Hotel")
        phone = run.find("Phone")
        if hotel:
            run.observe(hotel, down_bps=14_000_000, up_bps=3_000_000, rtt_ms=38, saturated=True)
        if phone:
            run.observe(phone, down_bps=20_000_000, up_bps=8_000_000, rtt_ms=45, saturated=True)
        run.daemon.demand.set("Stable_critical", down_bps=2_000_000, up_bps=500_000)
        run.daemon.demand.set("Stable_besteffort", down_bps=9_000_000, up_bps=1_000_000)

    def congest(run: ScenarioRunner) -> None:
        hotel = run.find("Hotel")
        if hotel:
            run.observe(
                hotel,
                down_bps=1_400_000,
                up_bps=200_000,
                rtt_ms=820,
                jitter_ms=210,
                loss_pct=17.0,
                bloat_ms=780,
                saturated=True,
            )

    def demand_rises(run: ScenarioRunner) -> None:
        run.daemon.demand.set("Stable_critical", down_bps=3_100_000, up_bps=800_000)
        run.daemon.demand.set(
            "Stable_besteffort", down_bps=20_000_000, up_bps=2_000_000, constrained=True
        )

    def recover(run: ScenarioRunner) -> None:
        hotel = run.find("Hotel")
        if hotel:
            run.observe(hotel, down_bps=14_000_000, up_bps=3_000_000, rtt_ms=40, saturated=True)
        run.daemon.demand.set("Stable_critical", down_bps=2_000_000, up_bps=500_000)

    return [
        Step(0, "hotel wifi NORMAL, phone BACKUP, everything healthy", setup),
        Step(60, "wifi congests: 800 ms RTT, 17% loss, capacity collapses", congest),
        Step(90, "critical demand rises above what NORMAL can carry", demand_rises),
        Step(600, "wifi recovers", recover),
    ]


SCENARIOS: dict[str, Callable[[], list[Step]]] = {
    "moving_van": moving_van,
}


def install(daemon, name: str) -> ScenarioRunner:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; have {sorted(SCENARIOS)}")

    runner = ScenarioRunner(daemon, SCENARIOS[name]())
    original_tick = daemon.tick

    def tick_with_scenario() -> None:
        runner.pump()
        original_tick()

    daemon.tick = tick_with_scenario  # type: ignore[method-assign]
    log.info(
        "Scenario installed",
        extra={
            "workflow": "scenario_init",
            "state": "completed",
            "intent": "exercise the control loop against realistic conditions",
            "scenario": name,
            "steps": len(runner.steps),
        },
    )
    return runner
