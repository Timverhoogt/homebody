# Runtime-downloaded model notices

## sherpa-onnx KWS Zipformer GigaSpeech 3.3M

The app downloads the English open-vocabulary keyword-spotting model from the official sherpa-onnx release:

- Source: https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models
- Archive: `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2`
- Upstream model metadata declares: **Apache License 2.0**
- Training data: GigaSpeech XL

The model is cached in the user's application-data directory and is not committed to this repository.

## Bundled Reachy Mini Home Assistant HaGRID gesture models

The wheel bundles two ONNX models from the Apache-2.0 Reachy Mini Home Assistant app:

- Source: https://huggingface.co/spaces/djhui5710/reachy_mini_home_assistant
- Reference commit: `c5fd1f522ab44e8e9feb2897d4018027a8afb063`
- `hand_detector.onnx` SHA-256: `a8ef73d466b61a8e8677be9c47008b217a11d1b265d95e36bf2521ff93329af6`
- `crops_classifier.onnx` SHA-256: `12a02344f63a7c4f2a2ca90f8740ca10a08c17b683b5585d73c3e88323056762`
- Upstream package license: **Apache License 2.0**

Hermes verifies both checksums before loading the models. Camera frames are processed locally in memory and are not retained by the gesture loop.
