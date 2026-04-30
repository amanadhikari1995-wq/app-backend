"""
analyze.py — LangGraph multi-agent bot & API analysis system

Architecture:
  Code Input
      │
      ▼
  ┌───────────────────┐
  │  detect_bot node  │  ← Agent 1: Claude identifies bot type & sub-label
  └────────┬──────────┘
           │
           ▼
  ┌───────────────────┐
  │  detect_apis node │  ← Agent 2: Claude detects every API / service used
  └────────┬──────────┘
           │
           ▼
     Final JSON Response

Both agents use Claude Haiku (fast + cheap) with structured JSON outputs.
"""

import os
import json
import logging
from typing import TypedDict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from langchain_anthropic import ChatAnthropic
from langgraph.graph import StateGraph, START, END

logger = logging.getLogger(__name__)

# ── Router ─────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/analyze", tags=["analyze"])

# ── LLM setup (Claude Haiku — fast, accurate, cheap) ──────────────────────────
_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

def _get_llm() -> ChatAnthropic:
    if not _ANTHROPIC_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured in backend .env")
    return ChatAnthropic(
        model="claude-haiku-4-5-20251001",
        api_key=_ANTHROPIC_KEY,
        temperature=0,
        max_tokens=2048,
    )

# ── LangGraph state ────────────────────────────────────────────────────────────
class AnalysisState(TypedDict):
    code: str
    # Bot detection
    bot_type: str
    bot_sublabel: str
    bot_confidence: float
    bot_reasoning: str
    # API detection
    detected_apis: List[dict]


# ── Node 1: Bot Type Detector agent ───────────────────────────────────────────
async def detect_bot_node(state: AnalysisState) -> dict:
    """
    Agent 1 — Analyzes the code structure, imports, and patterns to determine
    exactly what type of bot is being implemented.
    """
    llm = _get_llm()
    code_snippet = state["code"][:10000]  # cap to avoid token overload

    prompt = f"""You are a specialized bot-type detection agent. Your job is to analyze Python bot code and determine EXACTLY what type of bot it is with high precision.

VALID BOT TYPES — pick the single most accurate one:
- telegram      → Uses Telegram Bot API (telebot, python-telegram-bot, pyrogram, telethon, Bot(token=), @bot.message_handler)
- discord       → Uses Discord API (discord.py, commands.Bot, discord.Client, @bot.command, on_ready, on_message)
- twitter       → Uses Twitter/X API (tweepy, twitter, create_tweet, update_status)
- slack         → Uses Slack API (slack_sdk, WebClient(token=), slack_bolt, @app.command)
- arbitrage     → Cross-exchange price arbitrage (arbitrage, arb_profit, cross_exchange, triangular_arb)
- dca           → Dollar Cost Averaging (dollar_cost_averaging, dca_bot, periodic_buy, accumulate_position)
- grid          → Grid trading (grid_bot, grid_trading, grid_levels, grid_step, num_grids)
- market_maker  → Market making (market_maker, bid_ask, liquidity_provider, best_bid, best_ask)
- trading       → General crypto/stock trading (ccxt, binance, bybit, okx, create_order, fetch_balance)
- ai_agent      → AI/LLM agent or chatbot (openai.OpenAI(), anthropic.Anthropic(), chat_history, messages=[])
- prediction    → ML prediction bot (.predict(), .fit(), RandomForest, XGBoost, neural_network)
- scraper       → Web scraping (BeautifulSoup, scrapy, selenium, playwright, find_all, driver.get)
- news          → News aggregation or sentiment (newsapi, feedparser, sentiment_analysis, vader, textblob)
- alert         → Price monitoring / alert bot (price_alert, price_threshold, notify_when, monitor_price)
- notification  → General notifications (webhook, smtp, send_email, send_notification)
- generic       → None of the above

PRIORITY RULES (check in this order):
1. If code uses Telegram SDK → "telegram" (even if it also trades)
2. If code uses Discord SDK → "discord"
3. If code uses Twitter/X SDK → "twitter"
4. If code uses Slack SDK → "slack"
5. Check for specialized trading strategies (arbitrage, dca, grid, market_maker)
6. If general exchange usage → "trading"
7. If heavy LLM usage with conversation → "ai_agent"
8. Otherwise use purpose-based types

CODE TO ANALYZE:
```python
{code_snippet}
```

Respond ONLY with valid JSON (no markdown, no explanation outside JSON):
{{
  "bot_type": "<one type from list above>",
  "bot_sublabel": "<specific human-readable name e.g. 'Telegram Binance Trading Bot', 'Discord Music Bot', 'Bybit Futures Bot'>",
  "confidence": <0.0-1.0>,
  "reasoning": "<1-2 sentence explanation of what signals led to this classification>"
}}"""

    try:
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        text = response.content.strip()

        # Strip markdown code fences if present
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()

        result = json.loads(text)
        return {
            "bot_type":       str(result.get("bot_type", "generic")),
            "bot_sublabel":   str(result.get("bot_sublabel", "Custom Bot")),
            "bot_confidence": float(result.get("confidence", 0.8)),
            "bot_reasoning":  str(result.get("reasoning", "")),
        }
    except Exception as exc:
        logger.warning("Bot detection node failed: %s", exc)
        return {
            "bot_type": "generic",
            "bot_sublabel": "Custom Bot",
            "bot_confidence": 0.5,
            "bot_reasoning": f"Analysis error: {exc}",
        }


# ── Node 2: API Detector agent ─────────────────────────────────────────────────
async def detect_apis_node(state: AnalysisState) -> dict:
    """
    Agent 2 — Scans every line of the code to detect ALL external APIs,
    services, SDKs, and data sources — nothing gets missed.
    """
    llm = _get_llm()
    code_snippet = state["code"][:10000]

    prompt = f"""You are a specialized API detection agent. Your job is to find EVERY SINGLE external API, service, library, or data source used in the Python code below — no matter how it's imported, accessed, or written.

DETECTION TARGETS — scan for ALL of these:
1. Import statements: `import telegram`, `from binance import`, `import ccxt`, `from openai import`, etc.
2. API base URLs in strings: `https://api.binance.com`, `https://api.telegram.org`, etc.
3. Environment variables: `TELEGRAM_BOT_TOKEN`, `BINANCE_API_KEY`, `OPENAI_API_KEY`, etc.
4. Client instantiation: `openai.OpenAI()`, `anthropic.Anthropic()`, `Bot(token=...)`, `ccxt.binance()`, etc.
5. API method calls: `client.chat.completions.create()`, `exchange.fetch_balance()`, `bot.send_message()`, etc.
6. WebSocket URLs: `wss://stream.binance.com`, `wss://ws.kraken.com`, etc.
7. SDK-specific patterns: `TeleBot()`, `commands.Bot()`, `tweepy.Client()`, `TradingClient()`, etc.

KNOWN API ICONS & COLORS:
- Telegram: ✈️ #3b82f6
- Discord: 🎮 #818cf8
- Twitter/X: 🐦 #3b82f6
- Slack: 💬 #f59e0b
- Binance: 🟡 #f59e0b
- Bybit: 🔶 #f97316
- OKX: ⭕ #06b6d4
- KuCoin: 🟢 #22c55e
- Kraken: 🐙 #7c3aed
- Coinbase: 🔵 #3b82f6
- Bitget: 💎 #0ea5e9
- Gate.io: 🚪 #8b5cf6
- BitMEX: ⚡ #ef4444
- Huobi/HTX: 🔥 #ef4444
- MEXC: 💠 #2dd4bf
- OpenAI: 🤖 #22c55e
- Anthropic: 🧠 #f59e0b
- Groq: ⚡ #f43f5e
- Google Gemini: ✨ #4ade80
- Mistral: 💫 #818cf8
- Cohere: 🌊 #818cf8
- CoinGecko: 🦎 #22c55e
- CoinMarketCap: 🪙 #f59e0b
- Yahoo Finance: 📊 #6b21a8
- Alpha Vantage: 📉 #22c55e
- Polygon.io: 🔷 #7c3aed
- Finnhub: 📡 #22c55e
- Alpaca: 🦙 #f59e0b
- OpenWeatherMap: 🌤️ #f59e0b
- NewsAPI: 📰 #f59e0b
- Reddit: 🤖 #f97316
- GitHub: 🐙 #94a3b8
- Stripe: 💳 #818cf8
- Twilio: 📱 #f43f5e
- SendGrid: 📧 #22c55e
- Etherscan: ⛓️ #3b82f6
- Notion: 📝 #94a3b8
- Airtable: 📋 #22c55e
- Spotify: 🎵 #22c55e
- requests (HTTP): 🌐 #64748b
- ccxt: 🔷 #00f5ff
- Kalshi: 🎯 #00f5ff
- Polymarket: 📈 #6366f1

CODE TO ANALYZE:
```python
{code_snippet}
```

Respond ONLY with a valid JSON array (empty array [] if none found). Each item MUST have all fields:
[
  {{
    "name": "Full API Name (e.g. 'Binance API', 'Telegram Bot API', 'OpenAI API')",
    "base_url": "https://api.example.com",
    "icon": "single emoji only",
    "color": "#hexcolor",
    "needs_secret": true,
    "description": "What credentials are needed (e.g. 'API Key + Secret Key required')",
    "matched_pattern": "Exact import/URL/variable that revealed this API"
  }}
]

BE THOROUGH. If the code does `import requests` + calls a specific URL, list BOTH the requests library AND the target service. Never miss an API."""

    try:
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        text = response.content.strip()

        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()

        apis = json.loads(text)
        if not isinstance(apis, list):
            apis = []

        # Normalise field names to match frontend DetectedApi interface
        normalized = []
        for api in apis:
            normalized.append({
                "name":           str(api.get("name", "Unknown API")),
                "baseUrl":        str(api.get("base_url", api.get("baseUrl", ""))),
                "icon":           str(api.get("icon", "🔑")),
                "color":          str(api.get("color", "#94a3b8")),
                "needsSecret":    bool(api.get("needs_secret", api.get("needsSecret", True))),
                "description":    str(api.get("description", "API credentials required")),
                "matchedPattern": str(api.get("matched_pattern", api.get("matchedPattern", ""))),
                "variableName":   str(api.get("variable_name", api.get("variableName", ""))),
            })

        return {"detected_apis": normalized}

    except Exception as exc:
        logger.warning("API detection node failed: %s", exc)
        return {"detected_apis": []}


# ── Build LangGraph workflow ───────────────────────────────────────────────────
def _build_graph():
    workflow = StateGraph(AnalysisState)

    workflow.add_node("detect_bot",  detect_bot_node)
    workflow.add_node("detect_apis", detect_apis_node)

    # Sequential: bot type first → then API scan
    # (gives ~5-8s total — two sequential Claude Haiku calls)
    workflow.add_edge(START,        "detect_bot")
    workflow.add_edge("detect_bot", "detect_apis")
    workflow.add_edge("detect_apis", END)

    return workflow.compile()


_graph = None

def get_graph():
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


# ── Request / Response schemas ─────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    code: str


class DetectedApiOut(BaseModel):
    name:           str
    baseUrl:        str = ""
    icon:           str = "🔑"
    color:          str = "#94a3b8"
    needsSecret:    bool = True
    description:    str = ""
    matchedPattern: str = ""
    variableName:   str = ""


class AnalyzeResponse(BaseModel):
    bot_type:       str
    bot_sublabel:   str
    bot_confidence: float
    bot_reasoning:  str
    detected_apis:  list[DetectedApiOut]
    powered_by:     str = "LangChain + LangGraph + Claude Haiku"


# ── Endpoint ───────────────────────────────────────────────────────────────────
@router.post("/", response_model=AnalyzeResponse)
async def analyze_code(req: AnalyzeRequest):
    """
    AI-powered code analysis using a LangGraph multi-agent workflow.

    Agent 1 (detect_bot):  Identifies bot type + specific sub-label
    Agent 2 (detect_apis): Detects every API / service / credential used

    Takes ~5-8 seconds. Returns structured JSON consumed by the frontend.
    """
    code = req.code.strip()
    if not code:
        raise HTTPException(400, "No code provided")

    if len(code) < 10:
        raise HTTPException(400, "Code too short to analyze")

    try:
        graph = get_graph()
        initial_state: AnalysisState = {
            "code":           code,
            "bot_type":       "generic",
            "bot_sublabel":   "Custom Bot",
            "bot_confidence": 0.5,
            "bot_reasoning":  "",
            "detected_apis":  [],
        }

        result = await graph.ainvoke(initial_state)

        return AnalyzeResponse(
            bot_type       = result.get("bot_type",       "generic"),
            bot_sublabel   = result.get("bot_sublabel",   "Custom Bot"),
            bot_confidence = result.get("bot_confidence", 0.5),
            bot_reasoning  = result.get("bot_reasoning",  ""),
            detected_apis  = result.get("detected_apis",  []),
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("LangGraph analysis failed: %s", exc, exc_info=True)
        raise HTTPException(500, f"Analysis failed: {exc}")
