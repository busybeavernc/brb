`selfie_segmenter_landscape.tflite` — MediaPipe Selfie Segmentation (landscape), by Google.
Apache License 2.0. Source: https://ai.google.dev/edge/mediapipe/solutions/vision/image_segmenter
Model card: https://storage.googleapis.com/mediapipe-assets/Model%20Card%20MediaPipe%20Selfie%20Segmentation.pdf

`blaze_face_short_range.tflite` — MediaPipe Face Detector (BlazeFace, short range), by Google. Finds faces and six
points on each (eyes, nose tip, mouth, ears); used for the face alignment guide. It doesn't recognise anyone.
Apache License 2.0. Source: https://ai.google.dev/edge/mediapipe/solutions/vision/face_detector

`kws/` — keyword spotter for Name alerts: a 3.3M-parameter zipformer trained on GigaSpeech, int8 ONNX, by the
k2-fsa / icefall project (via sherpa-onnx). Listens for the names you enter; it doesn't recognise who is speaking.
Apache License 2.0. Source: https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models
(sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01).

`asr/` — small English speech recogniser for Name alerts: zipformer (small) trained on LibriSpeech, int8 ONNX, by the
k2-fsa / icefall project (via sherpa-onnx). Transcribes only the few seconds around a heard name, on this computer.
Apache License 2.0. Source: https://huggingface.co/Zengwei/icefall-asr-librispeech-zipformer-small-2023-05-16
(sherpa-onnx-zipformer-small-en-2023-06-26).
