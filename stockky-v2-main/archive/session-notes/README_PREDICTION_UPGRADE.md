# Stockky Upgrade Pack (complete + news/event/training UI)

## Download
prediction-model-upgrade.zip

## All files (correct paths)

```
services/analysis-intelligence-service/fundamental/
  peer_multi_quarter.py
  peer_ranking.py
  wire_peer_multi_quarter.py
  INTEGRATION_SNIPPET.md

services/analysis-intelligence-service/news/
  news_quality.py              ← PWL aliases, 5–10 sources, filter, summary
  NEWS_INTEGRATION.md

services/analysis-intelligence-service/event/
  event_depth.py               ← results/bulk/insider + clean summary
  EVENT_INTEGRATION.md

services/decision-prediction-service/prediction/
  pred_features.py
  main.py

services/decision-prediction-service/training/
  feature_builder.py
  build_dataset_with_fund_news.py
  universe_ingest.py
  universe_routes.py

frontend/src/
  api_universe.ts
  components/ScanPanel_UniverseButton_Snippet.tsx
  components/TrainingProgressPanel.tsx   ← stages + remaining time
  components/TRAINING_PROGRESS_INTEGRATION.md

.github/workflows/
  overnight-universe-training.yml
```

## This pack fixes
1. News quality (PWL-type keywords, multi-source, summary)
2. Event depth (results / bulk / insider + summary)
3. Training progress UI (stages + elapsed + remaining time)
4. Peer ranking + multi-quarter
5. Prediction features (tech+fund+news+peer+consistency)
6. Universe → Training button + overnight job + 24–48h storage

## Install
Copy folders into stockky-v2 keeping paths. Follow each *_INTEGRATION.md.
