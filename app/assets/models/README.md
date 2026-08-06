# Face landmark model

`face_detection_yunet_2023mar.onnx` is the OpenCV Zoo YuNet face detector.
It supplies the five stable facial landmarks used by Retouch for eye, nose,
mouth, and geometry masks.

- Source: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
- License: MIT
- SHA-256: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`

The 2023 model is intentional: this project pins OpenCV 4.x, while the newer
dynamic-input 2026 export is intended for OpenCV 5.x's ONNX Runtime engine.
