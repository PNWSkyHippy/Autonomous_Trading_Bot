    strategy_names = ["ALL strategies", "original_scorer", "rsi_momentum", "bollinger_breakout", "ema_crossover", "mean_reversion", "scalp_master", "swing_trader", "grid_bot", "dca_accumulator", "vwap_momentum", "hammer_reversal"]
    bt_strategy = st.selectbox("Strategy", strategy_names, index=0)
