"""
Per‑fold scaling using RobustScaler – no leakage.
"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler, StandardScaler
from typing import Union, Optional

class TimeAwareScaler:
    def __init__(self, scaler_type='RobustScaler'):
        if scaler_type == 'RobustScaler':
            self.scaler = RobustScaler()
        elif scaler_type == 'StandardScaler':
            self.scaler = StandardScaler()
        else:
            raise ValueError("Unsupported scaler")
        self.fitted = False

    def fit(self, X):
        self.scaler.fit(X)
        self.fitted = True
        return self

    def transform(self, X):
        if not self.fitted:
            raise RuntimeError("Not fitted")
        return self.scaler.transform(X).astype(np.float32)

    def fit_transform(self, X):
        self.fit(X)
        return self.transform(X)