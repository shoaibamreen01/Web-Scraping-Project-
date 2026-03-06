# Road Accident Detection Project

A deep learning-based system for detecting and classifying road accidents in real-time using video streams and image processing.

## Features

- Real-time accident detection using trained deep learning model
- Camera integration for live detection
- Image classification for accident severity
- Pre-trained model weights included

## Project Structure

- `main.py` - Main application entry point
- `detection.py` - Core detection logic
- `camera.py` - Camera module for video input
- `accident-classification.ipynb` - Jupyter notebook for model training and analysis
- `model.json` - Model architecture
- `model_weights.keras` - Pre-trained model weights
- `accident_photos/` - Sample accident images for testing

## Usage

```bash
python main.py
```

## Requirements

- Python 3.x
- TensorFlow/Keras
- OpenCV
- NumPy

## Model

The project uses a Keras-based deep learning model trained to classify road accidents from video frames.

## License

This project is open source and available under the MIT License.
