"""
Financial loss functions (kept for potential deep learning).
"""
import torch
import torch.nn as nn

class DirectionalLoss(nn.Module):
    def __init__(self, alpha=2.0):
        super().__init__()
        self.alpha = alpha
    def forward(self, pred, true):
        mse = (pred - true) ** 2
        penalty = torch.where(pred * true < 0, self.alpha, 1.0)
        return torch.mean(mse * penalty)