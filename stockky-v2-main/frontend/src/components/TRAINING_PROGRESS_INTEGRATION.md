# Training progress UI

1. Copy `TrainingProgressPanel.tsx` into `frontend/src/components/`.

2. In `Training.tsx`:

```tsx
import TrainingProgressPanel from "./TrainingProgressPanel";

// inside render, near the Trigger Training button:
<TrainingProgressPanel
  stage={trainProgress?.stage}
  detail={trainProgress?.detail}
  elapsedSec={elapsed}
  estimatedTotalSec={210}
  active={training || !!status?.training_in_progress}
  title="Training pipeline"
/>
```

3. For T+1 / T+5 buttons, reuse the same panel with:
   - title="T+1 evaluation" / "T+5 evaluation"
   - stage="evaluating_t1" or "evaluating_t5" while running


# Training progress UI

1. Copy `TrainingProgressPanel.tsx` into `frontend/src/components/`.

2. In `Training.tsx`:

```tsx
import TrainingProgressPanel from "./TrainingProgressPanel";

// inside render, near the Trigger Training button:
<TrainingProgressPanel
  stage={trainProgress?.stage}
  detail={trainProgress?.detail}
  elapsedSec={elapsed}
  estimatedTotalSec={210}
  active={training || !!status?.training_in_progress}
  title="Training pipeline"
/>
```

3. For T+1 / T+5 buttons, reuse the same panel with:
   - title="T+1 evaluation" / "T+5 evaluation"
   - stage="evaluating_t1" or "evaluating_t5" while running
