"""SystemInformation for the Reachy Mini.

``robot_position`` reports where the robot faces: a Reachy Mini does not drive, so its
position is 0, 0 and the yaw of its body. ``engine_status`` reports the status of the
engine that hosts the component. While the robot's daemon does not answer, the
connection raises ERROR on this component as on the others, and the engine refuses its
queries.
"""

from __future__ import annotations

from datetime import UTC, datetime

from openrois.components.core import Component, component, query
from openrois.interfaces.components import SYSTEM_INFORMATION_PROFILE

from openrois.components.reachy_mini.connection import ReachyMiniConnection


@component(SYSTEM_INFORMATION_PROFILE)
class ReachyMiniSystemInformation(Component):
    """SystemInformation of one Reachy Mini.

    Args:
        connection: The connection to the robot, shared with its other components.
    """

    def __init__(self, connection: ReachyMiniConnection) -> None:
        self._connection = self.uses(connection)
        self._started = datetime.now(UTC)

    async def connect(self) -> None:
        self._started = datetime.now(UTC)

    @query("robot_position")
    async def robot_position(self) -> dict[str, object]:
        """``x,y,theta`` in meters and radians: 0, 0 and the yaw of the body."""
        mini = await self._connection.client()
        head_joints, _ = mini.get_current_joint_positions()
        body_yaw = float(head_joints[0]) if head_joints else 0.0
        return {
            "position_data": [f"0.0000,0.0000,{body_yaw:.4f}"],
            "robot_ref": [self.ref.split("/", 1)[0]],
            "timestamp": datetime.now(UTC),
        }

    @query("engine_status")
    async def engine_status(self) -> dict[str, object]:
        """The status of the engine, and when the component connected."""
        return {"operable_time": self._started, "status": self.engine_state()}
