
cd c:\users\linda\trading_bot_v2
rg "EXIT|STOP|HALT" logs\bot.log | tail -30
rg "perf_time|early_loss|hard_time" logs\bot.log
rg "grid_bot|TIME STOP EXEMPT" logs\bot.log | tail -20