from collections import deque

import erdos
from erdos.operator import OneInOneOut
from erdos.context import OneInOneOutContext

from pylot.perception.detection.utils import get_precision_recall_at_iou
from pylot.utils import time_epoch_ms


class DetectionDecayOperator(OneInOneOut):
    """Operator that computes timely accuracy metrics.

    Args:
        flags (absl.flags): Object to be used to access absl flags.
    """
    def __init__(self, flags):
        self._logger = erdos.utils.setup_logging(self.config.name,
                                                 self.config.log_file_name)
        self._csv_logger = erdos.utils.setup_csv_logging(
            self.config.name + '-csv', self.config.csv_log_file_name)
        self._flags = flags
        self._ground_bboxes = deque()
        self._iou_thresholds = [0.1 * i for i in range(1, 10)]

    def on_data(self, context: OneInOneOutContext, data: erdos.Message):
        # Ignore the first several seconds of the simulation because the car is
        # not moving at the beginning.
        assert len(context.timestamp.coordinates) == 1
        game_time = context.timestamp.coordinates[0]
        bboxes = []
        # Select the person bounding boxes.
        for obstacle in data['obstacles']:
            if obstacle.is_person():
                bboxes.append(obstacle.bounding_box)

        # Remove the buffered bboxes that are too old.
        while (len(self._ground_bboxes) > 0
               and game_time - self._ground_bboxes[0][0] >
               self._flags.decay_max_latency):
            self._ground_bboxes.popleft()

        sim_time = context.timestamp.coordinates[0]
        for (old_game_time, old_bboxes) in self._ground_bboxes:
            # Ideally, we would like to take multiple precision values at
            # different recalls and average them, but we can't vary model
            # confidence, so we just return the actual precision.
            if (len(bboxes) > 0 or len(old_bboxes) > 0):
                latency = game_time - old_game_time
                precisions = []
                for iou in self._iou_thresholds:
                    (precision,
                     _) = get_precision_recall_at_iou(bboxes, old_bboxes, iou)
                    precisions.append(precision)
                self._logger.info("Precision {}".format(precisions))
                avg_precision = float(sum(precisions)) / len(precisions)
                self._logger.info(
                    "The latency is {} and the average precision is {}".format(
                        latency, avg_precision))
                self._csv_logger.info('{},{},{},{},{:.4f}'.format(
                    time_epoch_ms(), sim_time, self.config.name, latency,
                    avg_precision))
                context.write_stream.send(
                    erdos.Message(context.timestamp, (latency, avg_precision)))

        # Buffer the new bounding boxes.
        self._ground_bboxes.append((game_time, bboxes))
