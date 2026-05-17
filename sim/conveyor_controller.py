# -*- coding: utf-8 -*-
"""
Simple kinematic conveyor belt adapter for CoppeliaSim.

The current scene does not contain a physical conveyor controller. This module
makes conveyor operations visible by moving selected parts along one axis while
the simulation is in stepping mode.
"""

import time


class ConveyorBeltController:
    """Move a configured list of scene objects as a logical conveyor pulse."""

    AXIS_INDEX = {
        "x": 0,
        "y": 1,
        "z": 2,
    }

    def __init__(
        self,
        sim,
        name,
        part_names,
        *,
        axis="y",
        delta=0.16,
        speed=0.10,
        dt=0.05,
        render_delay=0.01,
    ):
        self.sim = sim
        self.name = name
        self.part_names = list(part_names)
        self.axis = axis.lower()
        self.delta = float(delta)
        self.speed = float(speed)
        self.dt = float(dt)
        self.render_delay = float(render_delay)
        self.active = False

        if self.axis not in self.AXIS_INDEX:
            raise ValueError(f"Unsupported conveyor axis: {axis}")

    def forward(self):
        """Move parts in the positive configured direction."""
        self.active = True
        self._pulse(+1.0)

    def backward(self):
        """Move parts in the negative configured direction."""
        self.active = True
        self._pulse(-1.0)

    def stop(self):
        """Stop command placeholder; advances a few frames for visibility."""
        self.active = False
        self._wait_steps(5)

    def _pulse(self, direction):
        handles = self._resolve_part_handles()
        if not handles:
            print(f"    [WARN] [{self.name}] no configured parts found")
            self._wait_steps(8)
            return

        axis_index = self.AXIS_INDEX[self.axis]
        total_delta = direction * self.delta
        steps = max(1, int(abs(total_delta) / max(self.speed, 1e-6) / self.dt))

        starts = []
        for handle in handles:
            starts.append((handle, self.sim.getObjectPosition(handle, -1)))

        for step in range(1, steps + 1):
            t = step / steps
            for handle, start_pos in starts:
                pos = list(start_pos)
                pos[axis_index] = start_pos[axis_index] + total_delta * t
                self.sim.setObjectPosition(handle, -1, pos)
            self._step()

    def _resolve_part_handles(self):
        handles = []
        for name in self.part_names:
            try:
                handles.append(self.sim.getObject(f"/{name}"))
            except Exception:
                pass
        return handles

    def _wait_steps(self, steps):
        for _ in range(steps):
            self._step()

    def _step(self):
        self.sim.step()
        if self.render_delay > 0:
            time.sleep(self.render_delay)
