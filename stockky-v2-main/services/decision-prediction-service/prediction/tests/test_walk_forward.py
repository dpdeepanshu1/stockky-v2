import pandas as pd
import numpy as np
from walk_forward import WalkForwardSplitter

def test_split_chronology():
    df = pd.DataFrame({'price': np.arange(500)})
    splitter = WalkForwardSplitter(train_window=100, val_window=20, step_size=20, embargo_days=5)
    folds = splitter.split(df)
    for fold in folds:
        assert fold.train_end < fold.embargo_start
        assert fold.embargo_end < fold.val_start
        assert fold.val_end < len(df)
    print("Chronology test passed.")

if __name__ == '__main__':
    test_split_chronology()