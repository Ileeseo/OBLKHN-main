# **OBLKHN**
Optimized Bi-dimensional Large Kernel Hybrid Attention Mechanism for Digital Rock Image Super-Resolution
Yubo Zhang[^†], Junhao Bi, Chao Han,  Lei Xu, Haibin Xiang, Haihua Kong, Juanjuan Geng, Wanying Zhao 
[^†]: Corresponding author

## 💻Environment

- [PyTorch >= 2.1.0](https://pytorch.org/)
- [Python 3.11.0](https://www.python.org/downloads/)
- [Numpy](https://numpy.org/)
- [BasicSR >= 1.4.2](https://github.com/XPixelGroup/BasicSR)

## 🔧Installation

```python
pip install -r requirements.txt 
```

## 📜Data Preparation

The trainset uses the DeepRockSR2D (carbonate:3600,sandstone:3600). Each image is randomly cropped to a size of 64*64 and the dataloader will further randomly crop the images to the GT_size required for training. GT_size defaults to 128/256 (×2/×4).
