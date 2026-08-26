"""Video propagation seeds the tracker with the exact composed preview mask."""

from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import torch

from app.services.segmentation import video_backend


class _Predictor:
    def __init__(self):
        self.received_mask = None
        self.init_kwargs = None
        self.reset = False

    def init_state(self, **kwargs):
        self.init_kwargs = kwargs
        return {"video_height": 16, "video_width": 24}

    def add_new_mask(self, *, mask, **_kwargs):
        self.received_mask = mask.detach().clone()
        logits = (mask.float() * 2 - 1)[None, None]
        return 0, [1], logits

    def propagate_in_video(self, *_args, **_kwargs):
        return iter(())

    def reset_state(self, _state):
        self.reset = True


def test_composed_anchor_mask_seeds_video_tracking(tmp_path: Path):
    frames = tmp_path / "frames"
    output = tmp_path / "masks"
    frames.mkdir()
    cv2.imwrite(str(frames / "000000.jpg"), np.zeros((16, 24, 3), dtype=np.uint8))

    anchor = np.zeros((8, 12), dtype=np.uint8)
    anchor[2:6, 3:9] = 255
    predictor = _Predictor()

    with (
        mock.patch.object(video_backend, "is_installed", return_value=True),
        mock.patch.object(video_backend, "_predictor", return_value=predictor),
        mock.patch.object(video_backend, "_device", return_value="cpu"),
    ):
        result = video_backend.propagate_masks(
            frames,
            anchor_frame=0,
            anchor_mask=anchor,
            quality="faster",
            output_directory=output,
        )

    assert result.frame_count == 1
    assert predictor.received_mask is not None
    assert predictor.received_mask.shape == (16, 24)
    assert predictor.received_mask.dtype == torch.float32
    assert predictor.received_mask.device.type == "cpu"
    assert torch.all((predictor.received_mask == 0) | (predictor.received_mask == 1))
    assert predictor.received_mask.any().item()
    assert predictor.init_kwargs is not None
    assert predictor.init_kwargs["async_loading_frames"] is True
    assert predictor.reset
    stored = cv2.imread(str(output / "000000.png"), cv2.IMREAD_GRAYSCALE)
    assert stored is not None
    assert stored.shape == (16, 24)
    assert stored.max() == 255


def test_mps_uses_float32_synchronous_frame_loader(tmp_path: Path):
    frames = tmp_path / "frames"
    output = tmp_path / "masks"
    frames.mkdir()
    cv2.imwrite(str(frames / "000000.jpg"), np.zeros((16, 24, 3), dtype=np.uint8))
    predictor = _Predictor()
    anchor = np.zeros((16, 24), dtype=np.uint8)
    anchor[4:12, 6:18] = 255

    with (
        mock.patch.object(video_backend, "is_installed", return_value=True),
        mock.patch.object(video_backend, "_predictor", return_value=predictor),
        mock.patch.object(video_backend, "_device", return_value="mps"),
    ):
        video_backend.propagate_masks(
            frames,
            anchor_frame=0,
            anchor_mask=anchor,
            quality="faster",
            output_directory=output,
        )

    assert predictor.init_kwargs is not None
    assert predictor.init_kwargs["async_loading_frames"] is False
