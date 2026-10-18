# Group 439: does the 52-week rule earn its place in weak markets?

18 Oct `legacy_gate_backtest.py hist400` (357 symbols, T+10, net of costs): `range_52w_legacy` and `range_52w_with_exemption` both read **hurts** on
every robust measure (median edge about -0.63 pp, clipped about -0.65 pp, day-cluster 90% CI [-0.91, -0.37]); the rule rejected the days that did
better. But that replay applies the rule in every market, while the live rule (`CANDIDATE_52W_REGIME_GATE=1`) applies it only when the market score
is below `CANDIDATE_52W_WEAK_REGIME_SCORE` (50). So the report could not say whether the rule hurts where it is actually switched on.

New: the report ends with a section "The 52-week rule by market regime": the same gates run separately on days when the Nifty close is below its
own 50-day average (weak) and on the other days (strong). The dashboard score cannot be rebuilt from daily bars, so this is a stand-in.
Nothing live changes. Switches that already exist: `CANDIDATE_52W_LOG_ONLY=1` (evaluate and log, never reject), `CANDIDATE_52W_RULE_ENABLED=0` (off).

Read it as: weak-regime row says `hurts` too -> the rule has no regime in which it helps in this history. Weak says `helps` or `no_edge` while strong says
`hurts` -> the regime gate is doing its job. Caveats: T+10 holds, while the bot exits within a day or two; the 400 stocks are today's universe
(survivorship); daily bars cannot see intraday stops.
