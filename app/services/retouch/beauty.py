"""Face-aware, temporally stable beauty processing using OpenCV.

The detector locates faces; every cosmetic operation is restricted to soft
anatomical masks inside those faces. That avoids the classic fake-beauty bug
where teeth whitening also turns a white shirt blue or smoothing erases the
entire background.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2  # type: ignore
import numpy as np  # type: ignore

from .settings import effective_retouch_settings


Box = tuple[int, int, int, int]


def _cascade(filename: str, error: str):
    path = cv2.data.haarcascades + filename
    detector = cv2.CascadeClassifier(path)
    if detector.empty():
        raise RuntimeError(error)
    return detector


def _face_detector():
    return _cascade(
        "haarcascade_frontalface_default.xml",
        "OpenCV's frontal-face detector data is unavailable.",
    )


def _profile_detector():
    return _cascade(
        "haarcascade_profileface.xml",
        "OpenCV's profile-face detector data is unavailable.",
    )


def _eye_detector():
    return _cascade(
        "haarcascade_eye_tree_eyeglasses.xml",
        "OpenCV's eye detector data is unavailable.",
    )


def _smile_detector():
    return _cascade(
        "haarcascade_smile.xml",
        "OpenCV's smile detector data is unavailable.",
    )


def _yunet_detector():
    model = Path(__file__).resolve().parents[2] / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
    if not model.is_file():
        return None
    try:
        if hasattr(cv2, "FaceDetectorYN"):
            return cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.72, 0.3, 5000)
        if hasattr(cv2, "FaceDetectorYN_create"):
            return cv2.FaceDetectorYN_create(str(model), "", (320, 320), 0.72, 0.3, 5000)
    except cv2.error:
        # A mismatched or minimal OpenCV build should degrade to Haar instead
        # of making the entire render worker fail to boot.
        return None
    return None


@dataclass(frozen=True)
class FaceFeatures:
    """Normalized facial landmarks with safe anatomical fallbacks."""

    eyes: tuple[tuple[float, float], tuple[float, float]] = ((0.32, 0.40), (0.68, 0.40))
    nose: tuple[float, float] = (0.50, 0.56)
    mouth: tuple[float, float, float, float] = (0.50, 0.72, 0.22, 0.10)
    eyes_detected: bool = False
    nose_detected: bool = False
    mouth_detected: bool = False


@dataclass(frozen=True)
class FaceAnalysis:
    """A detected face and the anatomical regions safe to edit."""

    box: Box
    features: FaceFeatures


def _box_iou(first: Box, second: Box) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0, right - left) * max(0, bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union else 0.0


def _dedupe_boxes(boxes: list[Box]) -> list[Box]:
    kept: list[Box] = []
    for box in sorted(boxes, key=lambda item: item[2] * item[3], reverse=True):
        if all(_box_iou(box, existing) < 0.36 for existing in kept):
            kept.append(box)
    return kept


@dataclass
class BeautyState:
    yunet_detector: Any = field(default_factory=_yunet_detector)
    detector: Any = field(default_factory=_face_detector)
    profile_detector: Any = field(default_factory=_profile_detector)
    eye_detector: Any = field(default_factory=_eye_detector)
    smile_detector: Any = field(default_factory=_smile_detector)
    faces: list[Box] = field(default_factory=list)
    feature_faces: list[Box] = field(default_factory=list)
    feature_cache: list[FaceFeatures] = field(default_factory=list)
    landmark_faces: list[Box] = field(default_factory=list)
    landmark_cache: list[FaceFeatures] = field(default_factory=list)
    frame_index: int = 0
    missed_frames: int = 0

    def locate(self, frame: Any, target: str) -> list[Box]:
        height, width = frame.shape[:2]
        # Detection at a bounded size is both faster and less jittery on 4K.
        scale = min(1.0, 720.0 / max(height, width))
        probe = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
        should_detect = self.frame_index % 3 == 0 or not self.faces
        detected: list[Box] = []
        if should_detect:
            raw_boxes: list[Box] = []
            landmark_faces: list[Box] = []
            landmark_cache: list[FaceFeatures] = []
            if self.yunet_detector is not None:
                try:
                    observations = _detect_yunet(self.yunet_detector, probe, scale)
                    for face, features in observations:
                        raw_boxes.append(face)
                        landmark_faces.append(face)
                        landmark_cache.append(features)
                except cv2.error:
                    # Keep the render alive if a platform ships an incomplete
                    # DNN backend. Haar below remains a deterministic fallback.
                    self.yunet_detector = None
            if not raw_boxes:
                gray = cv2.cvtColor(probe, cv2.COLOR_BGR2GRAY)
                gray = cv2.equalizeHist(gray)
                raw = self.detector.detectMultiScale(
                    gray,
                    scaleFactor=1.09,
                    minNeighbors=5,
                    minSize=(max(36, int(52 * scale)), max(36, int(52 * scale))),
                )
                raw_boxes.extend(
                    (int(x / scale), int(y / scale), int(w / scale), int(h / scale))
                    for x, y, w, h in raw
                )
            # Profile detection on both orientations is the final no-model
            # fallback. It runs only when neither YuNet nor frontal Haar finds
            # a face, which avoids Haar false positives beside a valid face.
            if not raw_boxes:
                profile_kwargs = {
                    "scaleFactor": 1.08,
                    "minNeighbors": 4,
                    "minSize": (max(36, int(52 * scale)), max(36, int(52 * scale))),
                }
                profile = self.profile_detector.detectMultiScale(gray, **profile_kwargs)
                mirrored = self.profile_detector.detectMultiScale(cv2.flip(gray, 1), **profile_kwargs)
                raw_boxes.extend(
                    (int(x / scale), int(y / scale), int(w / scale), int(h / scale))
                    for x, y, w, h in profile
                )
                raw_boxes.extend(
                    (
                        int((probe.shape[1] - x - w) / scale),
                        int(y / scale),
                        int(w / scale),
                        int(h / scale),
                    )
                    for x, y, w, h in mirrored
                )
            detected = _dedupe_boxes(raw_boxes)
            self.landmark_faces = landmark_faces
            self.landmark_cache = landmark_cache
        self.frame_index += 1

        if detected:
            detected.sort(key=lambda box: box[2] * box[3], reverse=True)
            if self.faces:
                smoothed: list[Box] = []
                available = set(range(len(self.faces)))
                for candidate in detected:
                    cx = candidate[0] + candidate[2] / 2
                    cy = candidate[1] + candidate[3] / 2
                    previous_index = min(
                        available,
                        key=lambda index: (
                            self.faces[index][0] + self.faces[index][2] / 2 - cx
                        ) ** 2 + (
                            self.faces[index][1] + self.faces[index][3] / 2 - cy
                        ) ** 2,
                    ) if available else None
                    previous = self.faces[previous_index] if previous_index is not None else None
                    if (
                        previous is not None
                        and abs(previous[0] + previous[2] / 2 - cx) < candidate[2] * 0.7
                        and abs(previous[1] + previous[3] / 2 - cy) < candidate[3] * 0.7
                    ):
                        candidate = tuple(
                            int(round(old * 0.68 + new * 0.32))
                            for old, new in zip(previous, candidate)
                        )  # type: ignore[assignment]
                        available.discard(previous_index)
                    smoothed.append(candidate)
                self.faces = smoothed
            else:
                self.faces = detected
            self.missed_frames = 0
        elif should_detect:
            self.missed_frames += 1
            if self.missed_frames > 6:
                self.faces = []
                self.feature_faces = []
                self.feature_cache = []
                self.landmark_faces = []
                self.landmark_cache = []

        faces = self.faces[:1] if target == "primary" else self.faces[:8]
        return [_clamp_box(box, width, height) for box in faces]

    def features_for(self, frame: Any, faces: list[Box]) -> list[FaceFeatures]:
        features: list[FaceFeatures] = []
        previous_available = set(range(len(self.feature_faces)))
        landmark_available = set(range(len(self.landmark_faces)))
        for face in faces:
            previous: FaceFeatures | None = None
            if previous_available and len(self.feature_faces) == len(self.feature_cache):
                match_index = max(
                    previous_available,
                    key=lambda index: _box_iou(face, self.feature_faces[index]),
                )
                if _box_iou(face, self.feature_faces[match_index]) >= 0.18:
                    previous = self.feature_cache[match_index]
                    previous_available.discard(match_index)
            detected: FaceFeatures | None = None
            if landmark_available and len(self.landmark_faces) == len(self.landmark_cache):
                landmark_index = max(
                    landmark_available,
                    key=lambda index: _box_iou(face, self.landmark_faces[index]),
                )
                landmark_face = self.landmark_faces[landmark_index]
                if _box_iou(face, landmark_face) >= 0.18:
                    landmark_available.discard(landmark_index)
                    detected = _rebase_features(
                        self.landmark_cache[landmark_index],
                        landmark_face,
                        face,
                    )
            if detected is None:
                detected = _detect_features(frame, face, self.eye_detector, self.smile_detector)
            features.append(_stabilize_features(detected, previous))
        self.feature_faces = list(faces)
        self.feature_cache = features
        return features


def _detect_yunet(detector: Any, probe: Any, scale: float) -> list[tuple[Box, FaceFeatures]]:
    probe_height, probe_width = probe.shape[:2]
    detector.setInputSize((probe_width, probe_height))
    _, rows = detector.detect(probe)
    if rows is None:
        return []

    original_width = max(1, int(round(probe_width / scale)))
    original_height = max(1, int(round(probe_height / scale)))
    observations: list[tuple[Box, FaceFeatures]] = []
    for row in rows:
        # YuNet returns bbox, right/left eye, nose, right/left mouth corner,
        # then confidence. A small expansion includes forehead and jaw so the
        # normalized masks align with the full visible face rather than a
        # detector-tight crop.
        px, py, pw, ph = (float(value) for value in row[:4])
        box = _clamp_box(
            (
                int(round((px - pw * 0.05) / scale)),
                int(round((py - ph * 0.08) / scale)),
                int(round(pw * 1.10 / scale)),
                int(round(ph * 1.13 / scale)),
            ),
            original_width,
            original_height,
        )
        x, y, w, h = box

        def normalized(index: int) -> tuple[float, float]:
            return (
                _clamp((float(row[index]) / scale - x) / w, 0.04, 0.96),
                _clamp((float(row[index + 1]) / scale - y) / h, 0.06, 0.94),
            )

        eyes = tuple(sorted((normalized(4), normalized(6)), key=lambda point: point[0]))
        nose = normalized(8)
        mouth_corners = tuple(sorted((normalized(10), normalized(12)), key=lambda point: point[0]))
        mouth_x = (mouth_corners[0][0] + mouth_corners[1][0]) * 0.5
        mouth_y = (mouth_corners[0][1] + mouth_corners[1][1]) * 0.5
        mouth_ax = _clamp((mouth_corners[1][0] - mouth_corners[0][0]) * 0.58, 0.12, 0.28)
        features = FaceFeatures(
            eyes=eyes,  # type: ignore[arg-type]
            nose=nose,
            mouth=(mouth_x, mouth_y, mouth_ax, 0.085),
            eyes_detected=True,
            nose_detected=True,
            mouth_detected=True,
        )
        observations.append((box, features))
    return observations


def _rebase_features(features: FaceFeatures, source: Box, target: Box) -> FaceFeatures:
    sx, sy, sw, sh = source
    tx, ty, tw, th = target

    def point(value: tuple[float, float]) -> tuple[float, float]:
        return (
            _clamp((sx + value[0] * sw - tx) / tw, 0.04, 0.96),
            _clamp((sy + value[1] * sh - ty) / th, 0.06, 0.94),
        )

    eyes = tuple(sorted((point(features.eyes[0]), point(features.eyes[1])), key=lambda item: item[0]))
    mouth_x, mouth_y = point((features.mouth[0], features.mouth[1]))
    return FaceFeatures(
        eyes=eyes,  # type: ignore[arg-type]
        nose=point(features.nose),
        mouth=(
            mouth_x,
            mouth_y,
            _clamp(features.mouth[2] * sw / tw, 0.11, 0.30),
            _clamp(features.mouth[3] * sh / th, 0.05, 0.14),
        ),
        eyes_detected=features.eyes_detected,
        nose_detected=features.nose_detected,
        mouth_detected=features.mouth_detected,
    )


def _clamp_box(box: Box, width: int, height: int) -> Box:
    x, y, w, h = box
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    return (x, y, max(1, min(width - x, w)), max(1, min(height - y, h)))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _detect_features(frame: Any, face: Box, eye_detector: Any, smile_detector: Any) -> FaceFeatures:
    x, y, w, h = face
    roi = frame[y : y + h, x : x + w]
    if not roi.size or w < 24 or h < 24:
        return FaceFeatures()

    gray = cv2.equalizeHist(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY))
    upper_height = max(1, int(h * 0.64))
    eye_boxes = eye_detector.detectMultiScale(
        gray[:upper_height],
        scaleFactor=1.08,
        minNeighbors=5,
        minSize=(max(8, int(w * 0.08)), max(6, int(h * 0.045))),
    )
    eye_candidates: list[tuple[float, float, float]] = []
    for ex, ey, ew, eh in eye_boxes:
        cx, cy = (ex + ew * 0.5) / w, (ey + eh * 0.5) / h
        if 0.11 <= cx <= 0.89 and 0.18 <= cy <= 0.58:
            eye_candidates.append((float(cx), float(cy), float(ew * eh)))

    best_pair: tuple[tuple[float, float], tuple[float, float]] | None = None
    best_pair_score = -1.0
    for index, first in enumerate(eye_candidates):
        for second in eye_candidates[index + 1 :]:
            left, right = sorted((first, second), key=lambda item: item[0])
            separation = right[0] - left[0]
            vertical_delta = abs(right[1] - left[1])
            if separation < 0.20 or separation > 0.62 or vertical_delta > 0.14:
                continue
            # Prefer a wide, level pair and use area only as a light signal;
            # this rejects eyebrows and duplicate detections around one eye.
            score = separation * 4.0 - vertical_delta * 3.0 + min(left[2], right[2]) / max(w * h, 1)
            if score > best_pair_score:
                best_pair_score = score
                best_pair = ((left[0], left[1]), (right[0], right[1]))

    lower_top = max(0, min(h - 1, int(h * 0.46)))
    smile_boxes = smile_detector.detectMultiScale(
        gray[lower_top:],
        scaleFactor=1.25,
        minNeighbors=15,
        minSize=(max(12, int(w * 0.18)), max(6, int(h * 0.055))),
    )
    mouth: tuple[float, float, float, float] | None = None
    mouth_score = -1.0
    for mx, my, mw, mh in smile_boxes:
        cx = (mx + mw * 0.5) / w
        cy = (lower_top + my + mh * 0.5) / h
        if not (0.25 <= cx <= 0.75 and 0.58 <= cy <= 0.88):
            continue
        centered = 1.0 - abs(cx - 0.5)
        score = float(mw * mh) * centered
        if score > mouth_score:
            mouth_score = score
            mouth = (
                _clamp(float(cx), 0.30, 0.70),
                _clamp(float(cy), 0.60, 0.84),
                _clamp(float(mw) / w * 0.58, 0.14, 0.28),
                _clamp(float(mh) / h * 0.52, 0.06, 0.13),
            )

    return FaceFeatures(
        eyes=best_pair or FaceFeatures().eyes,
        mouth=mouth or FaceFeatures().mouth,
        eyes_detected=best_pair is not None,
        mouth_detected=mouth is not None,
    )


def _stabilize_features(current: FaceFeatures, previous: FaceFeatures | None) -> FaceFeatures:
    if previous is None:
        return current

    if current.eyes_detected:
        if previous.eyes_detected:
            eyes = tuple(
                (
                    old[0] * 0.72 + new[0] * 0.28,
                    old[1] * 0.72 + new[1] * 0.28,
                )
                for old, new in zip(previous.eyes, current.eyes)
            )
        else:
            eyes = current.eyes
        eyes_detected = True
    else:
        eyes = previous.eyes
        eyes_detected = previous.eyes_detected

    if current.nose_detected:
        nose = (
            (
                previous.nose[0] * 0.72 + current.nose[0] * 0.28,
                previous.nose[1] * 0.72 + current.nose[1] * 0.28,
            )
            if previous.nose_detected
            else current.nose
        )
        nose_detected = True
    else:
        nose = previous.nose
        nose_detected = previous.nose_detected

    if current.mouth_detected:
        mouth = (
            tuple(old * 0.72 + new * 0.28 for old, new in zip(previous.mouth, current.mouth))
            if previous.mouth_detected
            else current.mouth
        )
        mouth_detected = True
    else:
        mouth = previous.mouth
        mouth_detected = previous.mouth_detected

    return FaceFeatures(
        eyes=eyes,  # type: ignore[arg-type]
        nose=nose,
        mouth=mouth,  # type: ignore[arg-type]
        eyes_detected=eyes_detected,
        nose_detected=nose_detected,
        mouth_detected=mouth_detected,
    )


def _soft_ellipse(shape: tuple[int, int], center: tuple[float, float], axes: tuple[float, float], blur: float = 0.08):
    height, width = shape
    mask = np.zeros((height, width), np.uint8)
    cv2.ellipse(
        mask,
        (int(center[0]), int(center[1])),
        (max(1, int(axes[0])), max(1, int(axes[1]))),
        0,
        0,
        360,
        255,
        -1,
        cv2.LINE_AA,
    )
    sigma = max(1.0, min(axes) * blur)
    return cv2.GaussianBlur(mask, (0, 0), sigma).astype(np.float32) / 255.0


def _blend(base: Any, changed: Any, mask: Any, amount: float = 1.0):
    alpha = np.clip(mask * amount, 0.0, 1.0)[..., None]
    return np.clip(base.astype(np.float32) * (1 - alpha) + changed.astype(np.float32) * alpha, 0, 255).astype(np.uint8)


def _skin_mask(roi: Any, features: FaceFeatures):
    height, width = roi.shape[:2]
    ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    _, cr, cb = cv2.split(ycrcb)
    _, sat, value = cv2.split(hsv)
    fixed_color = (cr >= 130) & (cr <= 183) & (cb >= 72) & (cb <= 142) & (sat <= 205) & (value >= 35)

    # Fixed skin thresholds skew toward a narrow set of complexions and white
    # balances. Build a second mask from this face's own cheek/nose chroma so
    # darker skin, colored lighting, and warm cameras receive the same cleanup.
    left_eye, right_eye = features.eyes
    mouth_x, mouth_y, _, _ = features.mouth
    seed_y = min(0.68, (left_eye[1] + right_eye[1]) * 0.24 + mouth_y * 0.52)
    seed = np.maximum.reduce(
        [
            _soft_ellipse((height, width), (width * features.nose[0], height * features.nose[1]), (width * 0.10, height * 0.15), 0.08),
            _soft_ellipse((height, width), (width * (left_eye[0] - 0.015), height * seed_y), (width * 0.10, height * 0.085), 0.10),
            _soft_ellipse((height, width), (width * (right_eye[0] + 0.015), height * seed_y), (width * 0.10, height * 0.085), 0.10),
        ]
    )
    sample = (seed >= 0.54) & (value >= 22) & (sat <= 235)
    if int(sample.sum()) >= max(20, int(width * height * 0.002)):
        center_cr = float(np.median(cr[sample]))
        center_cb = float(np.median(cb[sample]))
        chroma_distance = ((cr.astype(np.float32) - center_cr) / 19.0) ** 2 + (
            (cb.astype(np.float32) - center_cb) / 16.0
        ) ** 2
        adaptive_color = (chroma_distance <= 1.0) & (value >= 22) & (sat <= 235)
        color = (fixed_color | adaptive_color).astype(np.uint8) * 255
    else:
        color = fixed_color.astype(np.uint8) * 255
    color = cv2.morphologyEx(color, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    color = cv2.morphologyEx(color, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    oval = _soft_ellipse((height, width), (width * 0.5, height * 0.52), (width * 0.47, height * 0.53), 0.06)
    # Preserve eyes, brows and mouth detail from smoothing.
    feature = np.zeros((height, width), np.float32)
    for cx, cy in features.eyes:
        feature = np.maximum(
            feature,
            _soft_ellipse(
                (height, width),
                (width * cx, height * cy),
                (width * 0.13, height * 0.085),
                0.12,
            ),
        )
    mouth_x, mouth_y, mouth_ax, mouth_ay = features.mouth
    feature = np.maximum(
        feature,
        _soft_ellipse(
            (height, width),
            (width * mouth_x, height * mouth_y),
            (width * mouth_ax, height * mouth_ay * 1.08),
            0.14,
        ),
    )
    return np.clip((color.astype(np.float32) / 255.0) * oval * (1.0 - feature * 0.92), 0, 1)


def _apply_geometry(frame: Any, face: Box, features: FaceFeatures, settings: dict[str, Any]):
    x, y, w, h = face
    slim = settings["faceSlim"] / 100.0
    jaw = settings["jawSculpt"] / 100.0
    nose = settings["noseSlim"] / 100.0
    eyes = settings["eyeSize"] / 100.0
    chin = settings["chinShape"] / 100.0
    if max(slim, jaw, nose, eyes, chin) <= 0:
        return frame

    height, width = frame.shape[:2]
    # Remap only a padded face tile. A full-frame displacement map costs well
    # over 100 MB at 4K and multiplies that cost for every detected face.
    pad_x, pad_y = int(w * 0.14), int(h * 0.12)
    left, top = max(0, x - pad_x), max(0, y - pad_y)
    right, bottom = min(width, x + w + pad_x), min(height, y + h + pad_y)
    tile = frame[top:bottom, left:right].copy()
    yy, xx = np.mgrid[0:tile.shape[0], 0:tile.shape[1]].astype(np.float32)
    global_x = xx + left
    global_y = yy + top
    map_x = xx.copy()
    map_y = yy.copy()
    cx, cy = x + w * 0.5, y + h * 0.52
    nx = (global_x - cx) / max(w * 0.52, 1)
    ny = (global_y - cy) / max(h * 0.58, 1)
    face_weight = np.clip(1 - nx * nx - ny * ny, 0, 1) ** 2
    lower = np.clip((ny + 0.05) / 0.9, 0, 1)
    side = np.sign(nx)
    map_x += side * w * face_weight * (slim * 0.032 + jaw * 0.038 * lower)

    nose_x = x + w * features.nose[0]
    nose_y = y + h * features.nose[1]
    nose_nx = (global_x - nose_x) / max(w * 0.23, 1)
    nose_ny = (global_y - nose_y) / max(h * 0.30, 1)
    nose_weight = np.exp(-(nose_nx**2 + nose_ny**2) * 2.2)
    map_x += np.sign(nose_nx) * w * nose_weight * nose * 0.016

    for eye_x, eye_y in features.eyes:
        eye_cx, eye_cy = x + w * eye_x, y + h * eye_y
        ex = (global_x - eye_cx) / max(w * 0.15, 1)
        ey = (global_y - eye_cy) / max(h * 0.105, 1)
        weight = np.clip(1 - ex * ex - ey * ey, 0, 1) ** 2
        map_x -= (global_x - eye_cx) * weight * eyes * 0.14
        map_y -= (global_y - eye_cy) * weight * eyes * 0.14

    chin_weight = np.exp(-((nx / 0.42) ** 2 + ((ny - 0.55) / 0.30) ** 2) * 2.0)
    map_y -= h * chin_weight * chin * 0.018
    frame[top:bottom, left:right] = cv2.remap(
        tile, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT101
    )
    return frame


def _process_face(frame: Any, face: Box, features: FaceFeatures, raw_settings: dict[str, Any]):
    x, y, w, h = face
    probe = frame[y : y + h, x : x + w]
    face_luma = float(cv2.cvtColor(probe, cv2.COLOR_BGR2LAB)[..., 0].mean()) if probe.size else 140.0
    settings = effective_retouch_settings(raw_settings, face_luma=face_luma)
    frame = _apply_geometry(frame, face, features, settings)
    roi = frame[y : y + h, x : x + w].copy()
    if not roi.size:
        return frame
    skin = _skin_mask(roi, features)

    smooth = settings["skinSmooth"] / 100.0
    blemish = settings["blemishRemoval"] / 100.0
    even = settings["evenTone"] / 100.0
    detail = settings["detailProtection"] / 100.0
    if smooth > 0 or blemish > 0:
        bilateral = cv2.bilateralFilter(roi, 0, 18 + smooth * 42, 5 + smooth * 14)
        median = cv2.medianBlur(roi, 5 if blemish < 0.65 else 7)
        softened = cv2.addWeighted(bilateral, 1 - blemish * 0.35, median, blemish * 0.35, 0)
        # Restore a controlled amount of high-frequency detail: smoothing skin
        # should reduce texture, not make a wax model.
        high = roi.astype(np.float32) - cv2.GaussianBlur(roi, (0, 0), 1.2).astype(np.float32)
        softened = np.clip(softened.astype(np.float32) + high * detail * 0.55, 0, 255).astype(np.uint8)
        roi = _blend(roi, softened, skin, min(0.88, smooth * 0.72 + blemish * 0.34))
    if even > 0:
        lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        low = cv2.GaussianBlur(l, (0, 0), max(3.0, min(w, h) * 0.045))
        target = cv2.addWeighted(l, 1 - even * 0.35, low, even * 0.35, 0)
        toned = cv2.cvtColor(cv2.merge((target, a, b)), cv2.COLOR_LAB2BGR)
        roi = _blend(roi, toned, skin, even * 0.72)

    brighten = settings["skinBrighten"] / 100.0
    glow = settings["glow"] / 100.0
    if brighten > 0 or glow > 0:
        lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB).astype(np.float32)
        lab[..., 0] = np.clip(lab[..., 0] + brighten * 18.0, 0, 255)
        lit = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)
        if glow > 0:
            bloom = cv2.GaussianBlur(lit, (0, 0), max(1.5, min(w, h) * 0.018))
            lit = cv2.addWeighted(lit, 1.0, bloom, glow * 0.13, 0)
        roi = _blend(roi, lit, skin, min(1.0, brighten * 0.9 + glow * 0.55))

    # Eye regions: gentle local contrast plus under-eye luminance correction.
    eye_bright = settings["eyeBrighten"] / 100.0
    dark = settings["darkCircles"] / 100.0
    for eye_x, eye_y in features.eyes:
        cx, cy = w * eye_x, h * eye_y
        eye_mask = _soft_ellipse((h, w), (cx, cy), (w * 0.13, h * 0.07), 0.16)
        if eye_bright > 0:
            sharp = cv2.addWeighted(roi, 1.0 + eye_bright * 0.55, cv2.GaussianBlur(roi, (0, 0), 1.2), -eye_bright * 0.55, eye_bright * 5)
            roi = _blend(roi, sharp, eye_mask, eye_bright * 0.8)
        if dark > 0:
            under = _soft_ellipse((h, w), (cx, min(h * 0.62, cy + h * 0.085)), (w * 0.15, h * 0.065), 0.20)
            lifted = cv2.convertScaleAbs(cv2.bilateralFilter(roi, 7, 28, 9), alpha=1.0, beta=dark * 10)
            roi = _blend(roi, lifted, under * skin, dark * 0.72)

    mouth_x, mouth_y, mouth_ax, mouth_ay = features.mouth
    mouth_mask = _soft_ellipse(
        (h, w),
        (w * mouth_x, h * mouth_y),
        (w * mouth_ax, h * mouth_ay),
        0.18,
    )
    teeth = settings["teethWhiten"] / 100.0
    if teeth > 0:
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV).astype(np.float32)
        candidate = ((hsv[..., 1] < 115) & (hsv[..., 2] > 75)).astype(np.float32) * mouth_mask
        hsv[..., 1] *= 1 - candidate * teeth * 0.72
        hsv[..., 2] = np.clip(hsv[..., 2] + candidate * teeth * 36, 0, 255)
        whitened = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        roi = _blend(roi, whitened, candidate)

    smile = settings["smileLines"] / 100.0
    if smile > 0:
        line_offset = max(0.17, mouth_ax * 1.05)
        line_y = max(0.54, mouth_y - mouth_ay * 0.45)
        line_mask = np.maximum(
            _soft_ellipse(
                (h, w),
                (w * _clamp(mouth_x - line_offset, 0.16, 0.42), h * line_y),
                (w * 0.07, h * 0.14),
                0.2,
            ),
            _soft_ellipse(
                (h, w),
                (w * _clamp(mouth_x + line_offset, 0.58, 0.84), h * line_y),
                (w * 0.07, h * 0.14),
                0.2,
            ),
        )
        softened = cv2.bilateralFilter(roi, 9, 38, 11)
        roi = _blend(roi, softened, line_mask * skin, smile * 0.65)

    lip = settings["lipColor"] / 100.0
    if lip > 0:
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV).astype(np.float32)
        lip_pixels = ((hsv[..., 0] < 14) | (hsv[..., 0] > 168)).astype(np.float32) * (hsv[..., 1] > 45) * mouth_mask
        tint = roi.astype(np.float32)
        tint[..., 2] = np.clip(tint[..., 2] + 42, 0, 255)
        tint[..., 0] = np.clip(tint[..., 0] + 8, 0, 255)
        roi = _blend(roi, tint.astype(np.uint8), lip_pixels, min(0.8, lip))

    blush = settings["blush"] / 100.0
    if blush > 0:
        left_eye, right_eye = features.eyes
        cheek_y = min(0.69, (left_eye[1] + right_eye[1]) * 0.25 + mouth_y * 0.5)
        cheek = np.maximum(
            _soft_ellipse(
                (h, w),
                (w * _clamp(left_eye[0] - 0.045, 0.16, 0.42), h * cheek_y),
                (w * 0.13, h * 0.075),
                0.3,
            ),
            _soft_ellipse(
                (h, w),
                (w * _clamp(right_eye[0] + 0.045, 0.58, 0.84), h * cheek_y),
                (w * 0.13, h * 0.075),
                0.3,
            ),
        )
        rosy = roi.astype(np.float32)
        rosy[..., 2] = np.clip(rosy[..., 2] + 25, 0, 255)
        rosy[..., 0] = np.clip(rosy[..., 0] + 6, 0, 255)
        roi = _blend(roi, rosy.astype(np.uint8), cheek * skin, blush * 0.55)

    frame[y : y + h, x : x + w] = roi
    return frame


def analyze_faces(
    frame: Any,
    state: BeautyState | None = None,
    *,
    target: str = "all",
) -> list[FaceAnalysis]:
    """Detect editable facial regions once so preview and UI share a result."""
    state = state or BeautyState()
    faces = state.locate(frame, target)
    return [
        FaceAnalysis(box=face, features=features)
        for face, features in zip(faces, state.features_for(frame, faces))
    ]


def serialize_face_analysis(frame: Any, detections: list[FaceAnalysis]) -> dict[str, Any]:
    """Return normalized viewer geometry plus part-level control capabilities."""
    height, width = frame.shape[:2]
    safe_width, safe_height = max(1, width), max(1, height)
    rows: list[dict[str, Any]] = []
    for index, detection in enumerate(detections):
        x, y, box_width, box_height = detection.box
        features = detection.features
        rows.append(
            {
                "id": f"face-{index + 1}",
                "kind": "face",
                "box": {
                    "x": x / safe_width,
                    "y": y / safe_height,
                    "width": box_width / safe_width,
                    "height": box_height / safe_height,
                },
                "parts": {
                    "eyes": features.eyes_detected,
                    "nose": features.nose_detected,
                    "mouth": features.mouth_detected,
                },
                "landmarks": {
                    "eyes": [
                        {"x": x / safe_width + eye_x * box_width / safe_width, "y": y / safe_height + eye_y * box_height / safe_height}
                        for eye_x, eye_y in features.eyes
                    ],
                    "nose": {
                        "x": x / safe_width + features.nose[0] * box_width / safe_width,
                        "y": y / safe_height + features.nose[1] * box_height / safe_height,
                    },
                    "mouth": {
                        "x": x / safe_width + features.mouth[0] * box_width / safe_width,
                        "y": y / safe_height + features.mouth[1] * box_height / safe_height,
                    },
                },
            }
        )
    return {
        "width": width,
        "height": height,
        "detections": rows,
        "capabilities": {
            "face": bool(rows),
            "skin": bool(rows),
            "eyes": any(row["parts"]["eyes"] for row in rows),
            "nose": any(row["parts"]["nose"] for row in rows),
            "mouth": any(row["parts"]["mouth"] for row in rows),
            "multipleFaces": len(rows) > 1,
        },
    }


def beautify_frame(
    frame: Any,
    settings: dict[str, Any],
    state: BeautyState | None = None,
    *,
    detections: list[FaceAnalysis] | None = None,
):
    state = state or BeautyState()
    sanitized = effective_retouch_settings(settings)
    if not sanitized["enabled"]:
        return frame.copy()
    detected = detections
    if detected is None:
        detected = analyze_faces(frame, state, target=sanitized["targetFaces"])
    elif sanitized["targetFaces"] == "primary":
        detected = detected[:1]
    output = frame.copy()
    for detection in detected:
        output = _process_face(output, detection.box, detection.features, settings)
    return output


def encode_preview_png(frame: Any, settings: dict[str, Any]) -> tuple[bytes, int, int, int]:
    state = BeautyState()
    result = beautify_frame(frame, settings, state)
    ok, encoded = cv2.imencode(".png", result, [cv2.IMWRITE_PNG_COMPRESSION, 4])
    if not ok:
        raise RuntimeError("Could not encode the retouch preview.")
    height, width = frame.shape[:2]
    return encoded.tobytes(), width, height, len(state.faces)
