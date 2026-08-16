"""
Parameter-level learning and market regime analysis.
[reference:23][reference:24]
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Any

class InsightGenerator:
    def __init__(self, predictions_df: pd.DataFrame):
        self.df = predictions_df

    def analyze_parameter_performance(self, parameter: str) -> Dict:
        """
        Analyze how a specific parameter affects prediction success.
        [reference:25]
        """
        if parameter not in self.df.columns:
            return {'error': f'Parameter {parameter} not found'}

        # Group by parameter ranges
        if self.df[parameter].dtype in ['float64', 'int64']:
            bins = pd.qcut(self.df[parameter], q=4, duplicates='drop')
            grouped = self.df.groupby(bins)
        else:
            grouped = self.df.groupby(parameter)

        results = {}
        for name, group in grouped:
            success_rate = group['t1_success'].mean() if 't1_success' in group.columns else 0
            results[str(name)] = {
                'count': len(group),
                'success_rate': success_rate,
                'avg_return': group.get('t1_return', 0).mean()
            }

        return results

    def analyze_market_regime_performance(self) -> Dict:
        """
        Analyze prediction performance across different market regimes.
        [reference:26]
        """
        if 'market_regime' not in self.df.columns:
            return {'error': 'Market regime column not found'}

        regimes = self.df['market_regime'].unique()
        results = {}

        for regime in regimes:
            regime_df = self.df[self.df['market_regime'] == regime]
            results[regime] = {
                'count': len(regime_df),
                't1_success_rate': regime_df['t1_success'].mean() if 't1_success' in regime_df.columns else 0,
                't5_success_rate': regime_df['t5_success'].mean() if 't5_success' in regime_df.columns else 0,
                'avg_t1_return': regime_df.get('t1_return', 0).mean(),
                'avg_t5_return': regime_df.get('t5_return', 0).mean()
            }

        return results

    def generate_insights(self) -> List[Dict]:
        """
        Generate data-derived learning insights.
        [reference:27]
        """
        insights = []

        # Check if certain RSI ranges perform better
        if 'rsi' in self.df.columns and 't1_success' in self.df.columns:
            rsi_performance = self.analyze_parameter_performance('rsi')
            for rsi_range, data in rsi_performance.items():
                if data['success_rate'] > 0.7 and data['count'] > 10:
                    insights.append({
                        'insight': f'RSI {rsi_range} setups show {data["success_rate"]*100:.1f}% T+1 success rate',
                        'sample_size': data['count'],
                        'confidence': 'high' if data['count'] > 50 else 'medium',
                        'active': True
                    })

        # Check market regime impact
        if 'market_regime' in self.df.columns:
            regime_perf = self.analyze_market_regime_performance()
            for regime, data in regime_perf.items():
                if data['t1_success_rate'] > 0.7 and data['count'] > 10:
                    insights.append({
                        'insight': f'{regime} market regime: {data["t1_success_rate"]*100:.1f}% T+1 success rate',
                        'sample_size': data['count'],
                        'confidence': 'high' if data['count'] > 50 else 'medium',
                        'active': True
                    })

        return insights