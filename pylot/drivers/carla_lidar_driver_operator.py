"""This module implements an operator acts like a LiDAR driver when
using the simulator.

The operator attaches a LiDAR sensor to the ego vehicle, receives point clouds
from the simulator, and sends them on its output stream.
"""

import pickle
import threading

import erdos
from erdos import ReadStream, WriteStream
from erdos.operator import OneInTwoOut
from erdos.context import OneInTwoOutContext

from pylot.perception.point_cloud import PointCloud
from pylot.simulation.utils import check_simulator_version, \
    get_vehicle_handle, get_world, set_simulation_mode


class CarlaLidarDriverOperator(OneInTwoOut):
    """Publishes Lidar point clouds onto a stream.

    This operator attaches a vehicle at the required position with respect to
    the vehicle, registers callback functions to retrieve the point clouds and
    publishes it to downstream operators.

    Args:
        flags (absl.flags): Object to be used to access absl flags.
    """
    def __init__(self, lidar_setup, flags):
        self._flags = flags
        self._logger = erdos.utils.setup_logging(self.config.name,
                                                 self.config.log_file_name)
        self._lidar_setup = lidar_setup
        # The hero vehicle actor object we obtain from the simulator.
        self._vehicle = None
        # Handle to the Lidar simulator actor.
        self._lidar = None
        self._pickle_lock = threading.Lock()
        self._pickled_messages = {}
        self._lock = threading.Lock()
        # If false then the operator does not send data until it receives
        # release data watermark. Otherwise, it sends as soon as it
        # receives it.
        self._release_data = False

    def process_point_clouds(self, simulator_pc, left_write_stream,
                             right_write_stream):
        """ Invoked when a point cloud is received from the simulator.
        """
        print('callback')
        game_time = int(simulator_pc.timestamp * 1000)
        timestamp = erdos.Timestamp(coordinates=[game_time])
        watermark_msg = erdos.WatermarkMessage(timestamp)
        with erdos.profile(self.config.name + '.process_point_clouds',
                           self,
                           event_data={'timestamp': str(timestamp)}):
            # Ensure that the code executes serially
            with self._lock:
                assert len(
                    simulator_pc.raw_data) > 0, 'Lidar did not send any points'
                # Include the transform relative to the vehicle.
                # simulator_pc.transform returns the world transform, but
                # we do not use it directly.
                data = PointCloud.from_simulator_point_cloud(
                    simulator_pc, self._lidar_setup)
                if self._release_data:
                    left_write_stream.send(erdos.Message(timestamp, data))
                    left_write_stream.send(watermark_msg)
                else:
                    # Pickle the data, and release it upon release msg receipt.
                    pickled_msg = pickle.dumps(
                        data, protocol=pickle.HIGHEST_PROTOCOL)
                    with self._pickle_lock:
                        self._pickled_messages[timestamp] = pickled_msg
                    right_write_stream.send(watermark_msg)

    def run(self, read_stream: ReadStream, left_write_stream: WriteStream,
            right_write_stream: WriteStream):
        # Read the vehicle id from the vehicle id stream
        vehicle_id_msg = read_stream.read()
        vehicle_id = vehicle_id_msg.data
        self._logger.debug(
            "The LidarDriverOperator received the vehicle id: {}".format(
                vehicle_id))

        # Connect to the world. We connect here instead of in the constructor
        # to ensure we're connected to the latest world.
        client, world = get_world(self._flags.simulator_host,
                                  self._flags.simulator_port,
                                  self._flags.simulator_timeout)
        simulator_version = client.get_client_version()
        set_simulation_mode(world, self._flags)

        self._vehicle = get_vehicle_handle(world, vehicle_id)

        if check_simulator_version(simulator_version,
                                   required_minor=9,
                                   required_patch=10):
            self._lidar_setup.set_legacy(False)

        # Install the Lidar.
        lidar_blueprint = world.get_blueprint_library().find(
            self._lidar_setup.lidar_type)
        lidar_blueprint.set_attribute('channels',
                                      str(self._lidar_setup.channels))
        if check_simulator_version(simulator_version,
                                   required_minor=9,
                                   required_patch=7):
            lidar_blueprint.set_attribute(
                'range', str(self._lidar_setup.get_range_in_meters()))
        else:
            lidar_blueprint.set_attribute('range',
                                          str(self._lidar_setup.range))
        lidar_blueprint.set_attribute('points_per_second',
                                      str(self._lidar_setup.points_per_second))
        lidar_blueprint.set_attribute(
            'rotation_frequency', str(self._lidar_setup.rotation_frequency))
        lidar_blueprint.set_attribute('upper_fov',
                                      str(self._lidar_setup.upper_fov))
        lidar_blueprint.set_attribute('lower_fov',
                                      str(self._lidar_setup.lower_fov))
        if self._flags.simulator_lidar_frequency == -1:
            lidar_blueprint.set_attribute('sensor_tick', '0.0')
        else:
            lidar_blueprint.set_attribute(
                'sensor_tick',
                str(1.0 / self._flags.simulator_lidar_frequency))

        transform = self._lidar_setup.get_transform().as_simulator_transform()

        self._logger.debug("Spawning a lidar: {}".format(self._lidar_setup))
        # NOTE: The LiDAR can be attached on a rigid or a spring arm. If the
        # LiDAR is attached too low, on a rigit, then the point cloud is empty.
        # Otherwise, if the LiDAR is attached on a SpringArm it won't provide
        # 360 degrees point clouds.
        self._lidar = world.spawn_actor(lidar_blueprint,
                                        transform,
                                        attach_to=self._vehicle)

        # Register the callback on the Lidar.
        def _process_point_clouds(simulator_pc):
            self.process_point_clouds(simulator_pc, left_write_stream,
                                      right_write_stream)

        self._lidar.listen(_process_point_clouds)

    def on_watermark(self, context: OneInTwoOutContext):
        print('on watermark lidar')
        if context.timestamp.is_top:
            # The operator can always send data ASAP.
            self._release_data = True
        else:
            watermark_msg = erdos.WatermarkMessage(context.timestamp)
            context.left_write_stream.send_pickled(
                context.timestamp, self._pickled_messages[context.timestamp])
            # Note: The operator is set not to automatically propagate
            # watermark messages received on input streams. Thus, we can
            # issue watermarks only after the simulator callback is invoked.
            context.left_write_stream.send(watermark_msg)
            with self._pickle_lock:
                del self._pickled_messages[context.timestamp]

    def destroy(self):
        print('Destroying lidar driver operator')
