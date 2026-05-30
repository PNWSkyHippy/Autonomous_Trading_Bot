"""
=============================================================
  CLAUDE AUTONOMOUS SIGNAL REVIEWER
  intelligence/claude_reviewer.py

  Sits between the strategy engine and the trade executor.
  Every signal passes through here before execution.

  Three layers:
    1. Morning Briefing  — market context built once at open
    2. Signal Review     — per-signal approve/reject/skip
    3. Pre-execution     — final position size confirmation

  FALLBACK PHILOSOPHY (non-negotiable):
    Missing a trade costs nothing.
    Executing a bad trade autonomously costs real money.
    Therefore: ANY failure, timeout, or ambiguity = SKIP.
    The only path to APPROVE is an explicit, confident response.

  Level 2 autonomy: Claude can VETO signals but cannot
  INITIATE trades. Bot mechanical execution runs underneath.
=============================================================
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List

import requests
from dotenv import load_dotenv

import config
from data.database import db

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
CLAUDE_MODEL         = "claude-sonnet-4-5"
REVIEW_TIMEOUT_SEC   = 12
BRIEFING_TIMEOUT_SEC = 30
MAX_TOKENS_REVIEW    = 512
MAX_TOKENS_BRIEFING  = 1024
BRIEFING_CACHE_MIN   = 120

# ── Decision constants ────────────────────────────────────────────────────────
APPROVE = "APPROVE"
REJECT  = "REJECT"
SKIP    = "SKIP"


# ===========================================================================
#  DATA CLASSES
# ===========================================================================

@dataclass
class MarketContext:
    """
    Output of the morning briefing — stored and reused all session.
    All fields default to conservative values so a partial briefing
    failure still produces safe defaults.
    """
    timestamp:          datetime  = field(default_factory=datetime.now)
    allow_trading:      bool      = False
    conviction_score:   int       = 0
    market_sentiment:   str       = "UNKNOWN"
    breadth_health:     str       = "UNKNOWN"
    top_risk_score:     int       = 100
    hot_sectors:        List[str] = field(default_factory=list)
    cold_sectors:       List[str] = field(default_factory=list)
    active_themes:      List[str] = field(default_factory=list)
    high_impact_events: List[str] = field(default_factory=list)
    earnings_today:     List[str] = field(default_factory=list)
    max_exposure_pct:   float     = 0.0
    briefing_text:      str       = ""
    error:              bool      = True


@dataclass
class SignalDecision:
    """Output of per-signal review."""
    decision:           str   = SKIP
    confidence:         int   = 0
    reasoning:          str   = ""
    suggested_size_pct: Optional[float] = None
    warnings:           List[str] = field(default_factory=list)
    elapsed_ms:         int   = 0
    error:              bool  = True


# ===========================================================================
#  CLAUDE REVIEWER
# ===========================================================================

class ClaudeReviewer:
    """
    Autonomous signal review layer using Claude as the intelligence engine.
    Wraps every external call in conservative fallback logic.
    """

    def __init__(self):
        load_dotenv(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
        )
        self._api_key         = config.ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY", "")
        self._morning_context: Optional[MarketContext] = None
        self._briefing_time:  Optional[datetime]       = None

        # FIX: original code had `self.enabled = bool = False` which
        # hardcoded False by accidentally overwriting Python's built-in bool.
        # Correct form: evaluate whether API key exists.
        self.enabled = bool(self._api_key)

        if not self.enabled:
            logger.warning(
                "ClaudeReviewer: No API key found — reviewer disabled. "
                "All signals will be SKIPPED (conservative fallback). "
                "Add ANTHROPIC_API_KEY to .env to enable."
            )
        else:
            logger.info("ClaudeReviewer: API key found — reviewer enabled.")

    # =========================================================================
    #  PUBLIC API
    # =========================================================================

    def get_morning_context(self, force_refresh: bool = False) -> MarketContext:
        """
        Build or return cached morning market context.
        Cached for BRIEFING_CACHE_MIN minutes — no need to re-run every scan.
        Returns conservative MarketContext with allow_trading=False on ANY failure.
        """
        if not self.enabled:
            return self._safe_context("Reviewer disabled — API key missing")

        # Return cached briefing if fresh enough
        if (not force_refresh
                and self._morning_context
                and self._briefing_time
                and (datetime.now() - self._briefing_time).seconds < BRIEFING_CACHE_MIN * 60):
            logger.debug("Using cached morning briefing")
            return self._morning_context

        logger.info("ClaudeReviewer: Building morning market briefing...")
        context = self._run_morning_briefing()
        self._morning_context = context
        self._briefing_time   = datetime.now()

        if context.error:
            logger.warning(
                f"Morning briefing failed — trading conservatively blocked. "
                f"Reason: {context.briefing_text[:100]}"
            )
        else:
            logger.info(
                f"Morning briefing complete: conviction={context.conviction_score} "
                f"sentiment={context.market_sentiment} "
                f"allow_trading={context.allow_trading} "
                f"top_risk={context.top_risk_score}"
            )

        return context

    def review_signal(self, signal, market_context: MarketContext) -> SignalDecision:
        """
        Review a single trading signal. Returns APPROVE, REJECT, or SKIP.
        Conservative fallback on any failure.
        """
        start_ms = int(time.time() * 1000)

        if not self.enabled:
            return SignalDecision(decision="SKIP", reasoning="Reviewer disabled", error=True, elapsed_ms=0)

        if market_context.error:
            return SignalDecision(
                decision=SKIP,
                reasoning="Morning briefing failed — conservative skip",
                error=True,
                elapsed_ms=int(time.time()*1000) - start_ms
            )

        # Crypto trades 24/7 — never block on weekend/market-closed reasoning.
        # Only apply the allow_trading gate to stock signals.
        is_crypto = "/" in signal.symbol or signal.asset_class == "crypto"
        if not market_context.allow_trading and not is_crypto:
            return SignalDecision(
                decision=SKIP,
                reasoning=(
                    f"Trading blocked by morning briefing: "
                    f"conviction={market_context.conviction_score}, "
                    f"sentiment={market_context.market_sentiment}"
                ),
                error=False,
                elapsed_ms=int(time.time()*1000) - start_ms
            )

        symbol = signal.symbol.split("/")[0].split("-")[0]
        if symbol in market_context.earnings_today:
            return SignalDecision(
                decision=REJECT,
                reasoning=f"{symbol} reports earnings today — never trade through earnings",
                confidence=100,
                error=False,
                elapsed_ms=int(time.time()*1000) - start_ms
            )

        warnings = []
        if market_context.cold_sectors:
            warnings.append(
                f"Note: cold sectors today: {', '.join(market_context.cold_sectors[:3])}"
            )

        decision = self._call_signal_review(signal, market_context, warnings)
        decision.elapsed_ms = int(time.time()*1000) - start_ms

        logger.info(
            f"Signal review: {signal.symbol} {signal.direction} → "
            f"{decision.decision} (confidence={decision.confidence}, "
            f"{decision.elapsed_ms}ms) | {decision.reasoning[:80]}"
        )

        return decision

    def is_enabled(self) -> bool:
        return self.enabled

    # =========================================================================
    #  MORNING BRIEFING
    # =========================================================================

    def _run_morning_briefing(self) -> MarketContext:
        """
        Call Claude with a comprehensive morning briefing prompt.
        Returns conservative MarketContext on ANY failure.
        """
        summaries   = db.get_daily_summaries(5)
        capital_rec = db.get_latest_capital()
        capital     = capital_rec["total_capital"] if capital_rec else 0

        prompt = (
            f"Today is {datetime.now().strftime('%A, %B %d, %Y')}. "
            f"You are a trading bot morning briefing system. "
            f"Assess current US market conditions briefly, then respond with ONLY this JSON and nothing else:\n\n"
            f'{{"allow_trading": true, "conviction_score": 70, "market_sentiment": "BULLISH", '
            f'"breadth_health": "HEALTHY", "top_risk_score": 30, "hot_sectors": ["Technology"], '
            f'"cold_sectors": ["Utilities"], "active_themes": ["AI"], "high_impact_events": [], '
            f'"earnings_today": [], "max_exposure_pct": 60, "summary": "Brief summary."}}\n\n'
            f"Replace all values with your actual assessment. "
            f"Return ONLY the JSON object. No markdown. No headers. No explanation before or after."
        )
        try:
            response = self._call_claude(
                prompt    = prompt,
                timeout   = BRIEFING_TIMEOUT_SEC,
                max_tokens= MAX_TOKENS_BRIEFING
            )
            if response is None:
                return self._safe_context("API call failed or timed out")

            parsed = self._extract_json(response)
            if parsed is None:
                return self._safe_context("Could not parse JSON from briefing response")

            return MarketContext(
                timestamp         = datetime.now(),
                allow_trading     = bool(parsed.get("allow_trading", False)),
                conviction_score  = int(parsed.get("conviction_score", 0)),
                market_sentiment  = str(parsed.get("market_sentiment", "UNKNOWN")),
                breadth_health    = str(parsed.get("breadth_health", "UNKNOWN")),
                top_risk_score    = int(parsed.get("top_risk_score", 100)),
                hot_sectors       = list(parsed.get("hot_sectors", [])),
                cold_sectors      = list(parsed.get("cold_sectors", [])),
                active_themes     = list(parsed.get("active_themes", [])),
                high_impact_events= list(parsed.get("high_impact_events", [])),
                earnings_today    = [s.upper() for s in parsed.get("earnings_today", [])],
                max_exposure_pct  = float(parsed.get("max_exposure_pct", 0)),
                briefing_text     = str(parsed.get("summary", response[:200])),
                error             = False,
            )
        except Exception as e:
            logger.error(f"Morning briefing exception: {e}", exc_info=True)
            return self._safe_context(f"Exception: {e}")

    # =========================================================================
    #  SIGNAL REVIEW
    # =========================================================================

    def _call_signal_review(self, signal, context: MarketContext,
                             warnings: List[str]) -> SignalDecision:
        """
        Ask Claude to approve, reject, or skip a specific trading signal.
        Returns SKIP on ANY failure — never defaults to APPROVE.
        """
        indicators = signal.indicators or {}
        strategy   = indicators.get("strategy_name", "unknown")

        prompt = f"""You are reviewing a trading signal for an autonomous day trading bot.

MORNING CONTEXT:
- Market sentiment: {context.market_sentiment}
- Conviction score: {context.conviction_score}/100
- Market breadth: {context.breadth_health}
- Top/Distribution risk: {context.top_risk_score}/100
- Hot sectors: {', '.join(context.hot_sectors) or 'none identified'}
- Cold sectors: {', '.join(context.cold_sectors) or 'none identified'}
- Active themes: {', '.join(context.active_themes) or 'none identified'}
- High impact events today: {', '.join(context.high_impact_events) or 'none'}
- Summary: {context.briefing_text}

SIGNAL TO REVIEW:
- Symbol:     {signal.symbol}
- Direction:  {signal.direction.upper()}
- Strategy:   {strategy}
- Score:      {signal.score:.3f} (min required: {config.MIN_SIGNAL_CONFIDENCE})
- Price:      ${signal.current_price:.4f}
- Asset:      {signal.asset_class}

KEY INDICATORS:
- RSI:          {indicators.get('rsi', 'N/A')}
- MACD hist:    {indicators.get('macd_hist', 'N/A')}
- Volume ratio: {indicators.get('volume_ratio', 'N/A')}
- BB%:          {indicators.get('bb_pct', 'N/A')}

RISK PARAMETERS:
- Stop loss:   {config.DEFAULT_STOP_LOSS_PCT}%
- Take profit: {config.DEFAULT_TAKE_PROFIT_PCT}%
- Position:    {config.MAX_POSITION_PCT}% of capital

WARNINGS: {'; '.join(warnings) if warnings else 'none'}

Respond in this EXACT JSON format:
{{"decision": "APPROVE" or "REJECT" or "SKIP", "confidence": 0-100, "reasoning": "one clear sentence", "suggested_size_pct": null or number, "warnings": []}}

RULES:
- APPROVE only if genuinely confident this is a good trade
- REJECT if specific reasons to believe this trade will lose
- SKIP if uncertain, missing data, or conditions are ambiguous
- Never APPROVE during high-impact economic events
- Never APPROVE if market_sentiment is BEARISH and direction is long
- When in doubt: SKIP"""

        try:
            response = self._call_claude(
                prompt    = prompt,
                timeout   = REVIEW_TIMEOUT_SEC,
                max_tokens= MAX_TOKENS_REVIEW
            )

            if response is None:
                return SignalDecision(
                    decision=SKIP,
                    reasoning="API timeout or failure — conservative skip",
                    error=True
                )

            parsed = self._extract_json(response)
            if parsed is None:
                return SignalDecision(
                    decision=SKIP,
                    reasoning="Could not parse Claude response — conservative skip",
                    error=True
                )

            raw_decision = str(parsed.get("decision", "SKIP")).upper()
            if raw_decision not in (APPROVE, REJECT, SKIP):
                return SignalDecision(
                    decision=SKIP,
                    reasoning=f"Unrecognised decision '{raw_decision}' — conservative skip",
                    error=True
                )

            # Safety overrides
            if raw_decision == APPROVE:
                if context.market_sentiment == "BEARISH" and signal.direction == "long":
                    return SignalDecision(
                        decision=REJECT,
                        reasoning="Safety override: bearish market + long signal rejected",
                        confidence=90,
                        error=False
                    )
                if context.top_risk_score > 80:
                    return SignalDecision(
                        decision=SKIP,
                        reasoning=f"Safety override: distribution risk {context.top_risk_score}/100 too high",
                        confidence=80,
                        error=False
                    )

            return SignalDecision(
                decision           = raw_decision,
                confidence         = int(parsed.get("confidence", 0)),
                reasoning          = str(parsed.get("reasoning", "")),
                suggested_size_pct = parsed.get("suggested_size_pct"),
                warnings           = list(parsed.get("warnings", [])),
                error              = False,
            )

        except Exception as e:
            logger.error(f"Signal review exception for {signal.symbol}: {e}")
            return SignalDecision(
                decision=SKIP,
                reasoning=f"Exception during review — conservative skip: {e}",
                error=True
            )

    # =========================================================================
    #  CLAUDE API WRAPPER
    # =========================================================================

    def _call_claude(self, prompt: str, timeout: int,
                     max_tokens: int) -> Optional[str]:
        """
        Make a single Claude API call with timeout enforcement.
        Returns None on ANY failure — caller handles the conservative fallback.
        """
        if not self._api_key:
            logger.warning("Claude API call attempted with no API key")
            return None

        try:
            start    = time.time()
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      CLAUDE_MODEL,
                    "max_tokens": max_tokens,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=timeout
            )
            elapsed = time.time() - start

            if elapsed > timeout * 0.9:
                logger.warning(
                    f"Claude response was slow ({elapsed:.1f}s) — "
                    f"approaching timeout ({timeout}s)"
                )

            response.raise_for_status()
            data = response.json()
            text = data["content"][0]["text"]
            logger.debug(f"Claude API response ({elapsed:.1f}s): {text[:100]}...")
            return text

        except requests.exceptions.Timeout:
            logger.warning(f"Claude API timed out after {timeout}s — returning None")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning("Claude API connection error — returning None")
            return None
        except requests.exceptions.HTTPError as e:
            try:
                error_body = e.response.json()
                logger.error(f"Claude API HTTP error body: {error_body}")
            except Exception:
                logger.error(f"Claude API HTTP error: {e}")
            return None
        except Exception as e:
            logger.error(f"Claude API unexpected error: {e}")
            return None

    # =========================================================================
    #  HELPERS
    # =========================================================================

    def _extract_json(self, text: str) -> Optional[Dict]:
        """
        Extract and parse the first JSON object from Claude's response.
        Returns None if no valid JSON found.
        """
        matches = re.findall(r'\{.*?\}', text, re.DOTALL)
        for match in matches:
            try:
                parsed = json.loads(match)
                if any(k in parsed for k in (
                    "allow_trading", "decision", "conviction_score"
                )):
                    return parsed
            except json.JSONDecodeError:
                continue

        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        logger.warning(f"No valid JSON found in Claude response: {text[:200]}")
        return None

    def _safe_context(self, reason: str) -> MarketContext:
        """Return a maximally conservative MarketContext."""
        return MarketContext(
            allow_trading    = False,
            conviction_score = 0,
            market_sentiment = "UNKNOWN",
            top_risk_score   = 100,
            max_exposure_pct = 0.0,
            briefing_text    = reason,
            error            = True,
        )

    def get_status(self) -> Dict:
        """Return reviewer status for dashboard display."""
        return {
            "enabled":          self.enabled,
            "has_briefing":     self._morning_context is not None,
            "briefing_error":   self._morning_context.error if self._morning_context else True,
            "allow_trading":    self._morning_context.allow_trading if self._morning_context else False,
            "conviction":       self._morning_context.conviction_score if self._morning_context else 0,
            "sentiment":        self._morning_context.market_sentiment if self._morning_context else "UNKNOWN",
            "briefing_age_min": (
                int((datetime.now() - self._briefing_time).seconds / 60)
                if self._briefing_time else None
            ),
        }


# ── Singleton ────────────────────────────────────────────────────────────────
claude_reviewer = ClaudeReviewer()
