// Mock data for the trading bot dashboard
const MOCK_OPEN_POSITIONS = [
  { opened: '05/06 09:03 PT', symbol: 'OKB/USD',    type: 'Crypto', dir: 'SHORT', entry: 87.3100, current: 87.3100, sl: 88.0085, tp: 86.0003, pnlD: 0.00,    pnlP: 0.00,   toSL: 0.80, toTP: 1.50 },
  { opened: '05/06 09:02 PT', symbol: 'DASH/USD',   type: 'Crypto', dir: 'LONG',  entry: 55.3370, current: 55.0400, sl: 54.5069, tp: 56.9971, pnlD: -10.73, pnlP: -0.54, toSL: 0.97, toTP: 3.56 },
  { opened: '05/06 08:53 PT', symbol: 'ETH/USD',    type: 'Crypto', dir: 'SHORT', entry: 2388.5100, current: 2391.2600, sl: 2407.6181, tp: 2352.6824, pnlD: -2.29, pnlP: -0.12, toSL: 0.68, toTP: 1.61 },
  { opened: '05/06 08:53 PT', symbol: 'ZRO/USD',    type: 'Crypto', dir: 'SHORT', entry: 1.4860, current: 1.4660, sl: 1.4870, tp: 1.4637, pnlD: 26.72,   pnlP: 1.36,  toSL: 1.43, toTP: 0.16 },
  { opened: '05/06 08:48 PT', symbol: 'AAVE/USD',   type: 'Crypto', dir: 'SHORT', entry: 95.4300, current: 95.4700, sl: 96.1934, tp: 93.9985, pnlD: -0.83, pnlP: -0.04, toSL: 0.76, toTP: 1.54 },
  { opened: '05/06 08:44 PT', symbol: 'NOS/USD',    type: 'Crypto', dir: 'LONG',  entry: 0.2653, current: 0.2644, sl: 0.2613, tp: 0.2733, pnlD: -6.78,  pnlP: -0.34, toSL: 1.17, toTP: 3.35 },
  { opened: '05/06 08:43 PT', symbol: 'REZ/USD',    type: 'Crypto', dir: 'SHORT', entry: 0.0055, current: 0.0055, sl: 0.0055, tp: 0.0054, pnlD: 0.00,   pnlP: 0.00,  toSL: 0.80, toTP: 1.49 },
  { opened: '05/06 08:41 PT', symbol: 'BMB/USD',    type: 'Crypto', dir: 'LONG',  entry: 0.0035, current: 0.0035, sl: 0.0034, tp: 0.0036, pnlD: 0.00,   pnlP: 0.00,  toSL: 1.49, toTP: 3.00 },
  { opened: '05/06 08:40 PT', symbol: 'ARKM/USD',   type: 'Crypto', dir: 'LONG',  entry: 0.1318, current: 0.1327, sl: 0.1311, tp: 0.1358, pnlD: 13.66,  pnlP: 0.68,  toSL: 1.21, toTP: 2.30 },
  { opened: '05/06 08:38 PT', symbol: 'ATOM/USD',   type: 'Crypto', dir: 'SHORT', entry: 1.9609, current: 1.9621, sl: 1.9766, tp: 1.9315, pnlD: -1.22,  pnlP: -0.06, toSL: 0.74, toTP: 1.56 },
  { opened: '05/06 08:35 PT', symbol: 'FLOW/USD',   type: 'Crypto', dir: 'SHORT', entry: 0.0403, current: 0.0403, sl: 0.0405, tp: 0.0397, pnlD: 0.00,   pnlP: 0.00,  toSL: 0.50, toTP: 1.50 },
  { opened: '05/06 08:31 PT', symbol: 'RAVE/USD',   type: 'Crypto', dir: 'LONG',  entry: 0.0535, current: 0.0629, sl: 0.0524, tp: 0.0683, pnlD: 22.55,  pnlP: 1.13,  toSL: 1.44, toTP: 3.82 },
  { opened: '05/06 08:28 PT', symbol: 'MELANIA/USD', type: 'Crypto', dir: 'SHORT', entry: 0.1082, current: 0.1078, sl: 0.1098, tp: 0.1018, pnlD: 7.36,   pnlP: 0.37,  toSL: 1.11, toTP: 4.05 },
  { opened: '05/06 08:25 PT', symbol: 'REN/USD',    type: 'Crypto', dir: 'LONG',  entry: 0.0033, current: 0.0034, sl: 0.0034, tp: 0.0035, pnlD: 53.92,  pnlP: 2.72,  toSL: 0.20, toTP: 2.04 },
  { opened: '05/06 08:22 PT', symbol: 'W/USD',      type: 'Crypto', dir: 'SHORT', entry: 0.0140, current: 0.0140, sl: 0.0141, tp: 0.0138, pnlD: -12.67, pnlP: -4.43, toSL: 0.85, toTP: 1.42 },
  { opened: '05/06 08:18 PT', symbol: 'LDO/USD',    type: 'Crypto', dir: 'SHORT', entry: 0.3830, current: 0.3870, sl: 0.3820, tp: 0.3700, pnlD: -20.70, pnlP: -1.04, toSL: 0.27, toTP: 3.40 },
];

const MOCK_TRADE_LOG = [
  { opened: '05/06 04:43 PT', closed: '05/05 22:39 PT', dur: '-364m', symbol: 'CRV/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 0.2465, exit: 0.2427, reason: 'Take Profit',    pnlD: 30.73,  pnlP: 1.54,  result: 'Win'  },
  { opened: '05/06 04:44 PT', closed: '05/05 22:32 PT', dur: '-371m', symbol: 'ZRO/USD',    dir: 'LONG',  strategy: 'grid_bot', entry: 1.4090, exit: 1.3990, reason: 'Pivot Break S1', pnlD: -14.13, pnlP: -0.71, result: 'Loss' },
  { opened: '05/06 04:43 PT', closed: '05/05 21:44 PT', dur: '-419m', symbol: 'ZRX/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 0.1136, exit: 0.1149, reason: 'Stop Loss',      pnlD: -22.79, pnlP: -1.14, result: 'Loss' },
  { opened: '05/06 03:12 PT', closed: '05/05 21:04 PT', dur: '-368m', symbol: 'CAKE/USD',   dir: 'SHORT', strategy: 'grid_bot', entry: 1.5190, exit: 1.5300, reason: 'Pivot Break R1', pnlD: -14.44, pnlP: -0.72, result: 'Loss' },
  { opened: '05/06 00:59 PT', closed: '05/05 21:00 PT', dur: '-239m', symbol: 'AAVE/USD',   dir: 'SHORT', strategy: 'grid_bot', entry: 93.7700, exit: 94.1600, reason: 'Stale No Trend', pnlD: -8.31,  pnlP: -0.42, result: 'Loss' },
  { opened: '05/06 03:28 PT', closed: '05/05 20:58 PT', dur: '-389m', symbol: 'POPCAT/USD', dir: 'SHORT', strategy: 'grid_bot', entry: 0.0631, exit: 0.0637, reason: 'Stop Loss',      pnlD: -18.95, pnlP: -0.95, result: 'Loss' },
  { opened: '05/06 02:05 PT', closed: '05/05 20:56 PT', dur: '-308m', symbol: 'TNSR/USD',   dir: 'SHORT', strategy: 'grid_bot', entry: 0.0407, exit: 0.0410, reason: 'Pivot Break R1', pnlD: -14.71, pnlP: -0.74, result: 'Loss' },
  { opened: '05/06 02:05 PT', closed: '05/05 20:32 PT', dur: '-333m', symbol: 'PYTH/USD',   dir: 'SHORT', strategy: 'grid_bot', entry: 0.0504, exit: 0.0506, reason: 'Pivot Break R1', pnlD: -9.11,  pnlP: -0.46, result: 'Loss' },
  { opened: '05/06 02:05 PT', closed: '05/05 20:32 PT', dur: '-333m', symbol: 'SEI/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 0.0605, exit: 0.0606, reason: 'Pivot Break R1', pnlD: -1.65,  pnlP: -0.08, result: 'Loss' },
  { opened: '05/06 01:55 PT', closed: '05/05 20:31 PT', dur: '-323m', symbol: 'APT/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 1.0072, exit: 1.0199, reason: 'Stop Loss',      pnlD: -25.08, pnlP: -1.26, result: 'Loss' },
  { opened: '05/06 03:11 PT', closed: '05/05 20:25 PT', dur: '-411m', symbol: 'LDO/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 0.3780, exit: 0.3820, reason: 'Stop Loss',      pnlD: -21.14, pnlP: -1.06, result: 'Loss' },
  { opened: '05/06 02:00 PT', closed: '05/05 20:24 PT', dur: '-335m', symbol: 'CRV/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 0.2434, exit: 0.2434, reason: 'Pivot Break R1', pnlD: 0.00,   pnlP: 0.00,  result: 'Loss' },
  { opened: '05/06 01:55 PT', closed: '05/05 20:08 PT', dur: '-346m', symbol: 'ZK/USD',     dir: 'SHORT', strategy: 'grid_bot', entry: 0.0174, exit: 0.0175, reason: 'Pivot Break R1', pnlD: -6.86,  pnlP: -0.34, result: 'Loss' },
  { opened: '05/06 02:10 PT', closed: '05/05 20:02 PT', dur: '-367m', symbol: 'RLC/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 0.4692, exit: 0.4736, reason: 'Stop Loss',      pnlD: -18.71, pnlP: -0.94, result: 'Loss' },
  { opened: '05/06 02:05 PT', closed: '05/05 20:01 PT', dur: '-364m', symbol: 'EGLD/USD',   dir: 'SHORT', strategy: 'grid_bot', entry: 4.2700, exit: 4.2800, reason: 'Pivot Break R1', pnlD: -4.67,  pnlP: -0.23, result: 'Loss' },
  { opened: '05/06 01:50 PT', closed: '05/05 19:59 PT', dur: '-350m', symbol: 'W/USD',      dir: 'SHORT', strategy: 'grid_bot', entry: 0.0140, exit: 0.0140, reason: 'Pivot Break R1', pnlD: -5.71,  pnlP: -0.29, result: 'Loss' },
  { opened: '05/06 02:00 PT', closed: '05/05 19:36 PT', dur: '-384m', symbol: 'INJ/USD',    dir: 'SHORT', strategy: 'grid_bot', entry: 3.8420, exit: 3.8920, reason: 'Stop Loss',      pnlD: -25.94, pnlP: -1.30, result: 'Loss' },
];

const MOCK_DAILY_PERF = [
  { date: '05/05', trades: 28, wins: 8, losses: 20, winRate: 28.6, pnlD: -284.12, pnlP: -0.28, capital: 99534.75 },
  { date: '05/04', trades: 31, wins: 11, losses: 20, winRate: 35.5, pnlD: -156.40, pnlP: -0.16, capital: 99818.87 },
  { date: '05/03', trades: 22, wins: 9, losses: 13, winRate: 40.9, pnlD: 224.61, pnlP: 0.23, capital: 99975.27 },
  { date: '05/02', trades: 35, wins: 14, losses: 21, winRate: 40.0, pnlD: 412.85, pnlP: 0.41, capital: 99750.66 },
  { date: '05/01', trades: 19, wins: 6, losses: 13, winRate: 31.6, pnlD: -89.30, pnlP: -0.09, capital: 99337.81 },
  { date: '04/30', trades: 27, wins: 10, losses: 17, winRate: 37.0, pnlD: 178.42, pnlP: 0.18, capital: 99427.11 },
  { date: '04/29', trades: 24, wins: 12, losses: 12, winRate: 50.0, pnlD: 521.18, pnlP: 0.52, capital: 99248.69 },
];

// Generate 30-day capital growth curve
const MOCK_CAPITAL_HISTORY = (() => {
  const out = [];
  let cap = 100000;
  const now = new Date('2026-05-06T02:05:39');
  for (let i = 30; i >= 0; i--) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    cap += (Math.random() - 0.48) * 800;
    out.push({ date: d, capital: cap });
  }
  // Force end value to match KPI
  out[out.length - 1].capital = 99203.01;
  return out;
})();

const MOCK_INJECTED = {
  stocks: ['STX', 'EBAY', 'TSN', 'CBOE'],
  crypto: ['TON/USD', 'TRX/USD', 'LINK/USD', 'PI/USD', 'HTX/USD', 'MN13/USD'],
};

const STRATEGIES = [
  'ALL strategies',
  'original_scorer',
  'rsi_momentum',
  'bollinger_breakout',
  'bollinger_squeeze',
  'ema_crossover',
  'mean_reversion',
  'scalp_master',
  'swing_trader',
  'grid_bot',
  'dca_accumulator',
  'vwap_momentum',
  "vwap_confirmed_orb",
  'hammer_reversal',
  'orb_breakout',
  'adaptive_regime',
  'ecb_strategy',
  'vdmr_strategy',
  'rsi_dip_spike_v4',
];
const TIMEFRAMES = ['5m', '1h', '1d'];
const DURATIONS = ['30d', '60d', '90d (3mo)', '180d (6mo)', '365d (1y)', '730d (2y)'];
const ASSET_CLASSES = ['Stocks', 'Crypto', 'Futures', 'Both'];
const BROKERS = ['ibkr', 'alpaca', 'coinbase', 'kraken', 'binance'];
const DIRECTIONS = ['long', 'short'];

window.MOCK = {
  OPEN_POSITIONS: MOCK_OPEN_POSITIONS,
  TRADE_LOG: MOCK_TRADE_LOG,
  DAILY_PERF: MOCK_DAILY_PERF,
  CAPITAL_HISTORY: MOCK_CAPITAL_HISTORY,
  INJECTED: MOCK_INJECTED,
  STRATEGIES, TIMEFRAMES, DURATIONS, ASSET_CLASSES, BROKERS, DIRECTIONS,
};
