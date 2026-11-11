# PolyHFT - Polymarket High-Frequency Trading Bot

<div align="center">

![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-active-success.svg)

**An algorithmic trading bot for Polymarket prediction markets with 4 implemented trading strategies (paper trading)**

[Features](#-features) • [Quick Start](#-quick-start) • [Strategies](#-strategies) • [Documentation](#-documentation) • [Contributing](#-contributing)

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Features](#-features)
- [Trading Strategies](#-trading-strategies)
- [Quick Start](#-quick-start)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Risk Management](#-risk-management)
- [Architecture](#-architecture)
- [Documentation](#-documentation)
- [Contributing](#-contributing)
- [Disclaimer](#-disclaimer)

---

## 🎯 Overview

**PolyHFT** is a trading bot for Polymarket prediction markets. It runs 4 trading strategies against real market data with simulated (paper) fills by default. A gated live-order path exists (see below) but paper trading remains strongly recommended.

### Key Highlights

- 🚀 **4 Strategies**: single-market arbitrage, spread scalping, tail-end trading, and continuous market-making [BETA]
- 🛡️ **Risk Management**: position limits (per trade/market/strategy), exposure caps, stop-losses, drawdown protection — all applied against real filled positions
- 📊 **Paper Trading Mode** (default, and the only supported mode): orders fill against live market prices via a fill simulator; P&L is honest but no real money moves
- ⚡ **Batch market data**: orderbooks fetched in batches (POST /books), parallel price prefetch, 5s cache TTL
- 📱 **Real-time Notifications**: Telegram integration for instant trade alerts
- 🌐 **Web Dashboard**: local dashboard for status, metrics, positions and strategy toggles

> **Honest status**: this is a research/paper-trading system. Live order
> execution exists (signed EIP-712 orders via the official `polymarket-apis`
> SDK) but is behind a double gate — `paper_trading: false` AND
> `POLYMARKET_LIVE_TRADING=I_UNDERSTAND` in the environment. The strategies
> are NOT validated for profitability (tiny sample, see
> `scripts/audit_trades.py`); do not run live without understanding you can
> lose your full stake on a single tail-end trade.

---

## ✨ Features

### Core Capabilities

- **Multi-Strategy Execution**: Run multiple strategies simultaneously with independent risk controls
- **Intelligent Market Scanning**: Parallel market data fetching with 5-second cache TTL (90% API call reduction)
- **Advanced Order Management**: Coordinated order placement with conflict detection and position tracking
- **State Persistence**: Automatic state saving/restoration for seamless restarts
- **Comprehensive Logging**: Separate logs for trades, errors, and full activity with clean terminal output

### Risk Management

- Position size limits (per trade, per market, per strategy)
- Total exposure caps
- Stop-loss protection
- Maximum drawdown limits
- Open position limits
- Real-time P&L tracking

### Developer Experience

- Clean, modular architecture
- Unit tests for risk management, fills and strategies
- YAML-based configuration
- Environment variable support
- Comprehensive error handling
- Rate limiting and retry logic

---

## 📈 Trading Strategies

> **Honest status**: only the 4 strategies below are implemented and run against
> real market data with simulated (paper) fills. Everything in the
> [concept catalog](#concept-catalog-not-implemented) at the end of this section
> is a design idea that was pruned in Phase 0 and is **not implemented**.

### 1. Single-Market Arbitrage ⚖️
**Risk-free arbitrage within single markets**

- Finds markets where YES + NO < $1.00
- Buys all outcomes simultaneously
- Profit at resolution if both legs are filled (depends on fills and unambiguous resolution)
- Works with binary and multi-choice markets
- **Best for**: markets where YES+NO asks sum below $1
- **Example**: "Did it rain in London?" YES is trading at 40¢, NO is at 55¢. Total = 95¢. You buy 1000 shares of YES and 1000 shares of NO for $950 total. When market resolves, one side pays $1.00 (total $1000). Profit: $50 if both legs fill and resolution is unambiguous.

### 2. Spread Scalping ⚡
**High-frequency spread capture**

- Scans for liquid markets with wide spreads (3-5 cents)
- Places limit buy orders on likely outcome
- Automatically flips to sell to capture spread
- **Best for**: Active markets with decent volume
- **Example**: "Will Trump Win?" trading at 54¢. Bot sees order book imbalance and buy pressure. Buys at 54.2¢. Price ticks up to 55.5¢. Bot sells immediately. Profit: 1.3¢ per share (2.4% ROI in seconds).

### 3. Tail-End Trading 🏁
**High-probability outcomes near resolution**

- Buys outcomes priced >$0.93 near expiry (<7 days)
- Capitalizes on "certain" events offering 2-7% returns
- Protected by strict stop-loss and diversification
- **Best for**: Consistent, low-risk yield farming
- **Example**: "Will Taylor Swift announce album by Friday?" Thursday night, no news. YES is trading at 2¢, NO is at 96¢. Bot buys NO at 96¢. Friday passes, no album. Market resolves NO. Profit: 4¢ per share (4.1% ROI overnight).

### 4. Continuous Market Making 📊 [BETA]
**Band-based order management around market price**

⚠️ **BETA STATUS**: This strategy is under evaluation. Kept for the
liquidity-rewards (maker rebates) research line and disabled by default.
Profitability is NOT validated — paper fills cannot honestly model the
adverse selection that kills market makers. Requotes only on material mid
moves (`min_requote_move`); band amounts must fit `risk.max_position_size`.

- Maintains orders in configurable bands around market price
- Continuously adjusts orders as market moves (every second)
- Intelligent cancellation logic for inner/outer/middle bands
- Automatic order placement to maintain target amounts
- **Best for**: Advanced traders comfortable with continuous order management
- **Example**: "US GDP Growth > 2%". Price is 60¢. Bot maintains Buy orders at 58¢, 57¢, 56¢ and Sell orders at 62¢, 63¢, 64¢. As price fluctuates, it fills buys and sells, capturing small profits continuously.
### Concept catalog (NOT implemented)

These strategies were explored in early iterations and **pruned in Phase 0**
(no viable execution path in this codebase). They are kept here as design
ideas only — there is no code for them:

- **Hedging (cross-exchange)** — hedge Polymarket legs on Hyperliquid perpetuals. Hyperliquid client exists for reference but no hedging strategy runs.
- **Micro-Spread Farming** — HFT bid/ask capture in 5-10¢ markets.
- **Liquidity Provision** — spread tightening + Polymarket liquidity rewards.
- **Low-Volume High-Spread** — mint YES+NO pairs in new markets, work the legs.
- **Combinatorial Arbitrage** — logical contradictions across correlated markets.
- **Legged Arbitrage** — time-separated entry/hedge legs summing below $1.


---

## 🚀 Quick Start

### Prerequisites

- Python 3.8 or higher
- pip (Python package manager)
- Polymarket API credentials (optional for paper trading)

### 1. Clone the Repository

```bash
git clone https://github.com/Anmoldureha/polymarket-trading-bot-strategies.git
cd polymarket-trading-bot-strategies
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Run in Paper Trading Mode (Recommended First)

```bash
python main.py --paper
```

The bot will start scanning for opportunities using real market data but won't place real orders. Perfect for testing!

### 4. Configure for Live Trading (Optional)

1. Get Polymarket API credentials from [Polymarket](https://polymarket.com)
2. Set up configuration:
   ```bash
   # Copy example config file
   cp config/config.yaml.example config/config.yaml
   
   # Or create .env file (recommended for security)
   cp config/.env.example .env
   # Edit .env and add your credentials
   ```
3. Add your credentials to `.env` file:
   ```env
   POLYMARKET_API_KEY=your_api_key
   POLYMARKET_PRIVATE_KEY=your_private_key
   ```
4. Update `config/config.yaml`:
   ```yaml
   paper_trading: false
   ```
5. Start with small position sizes!

---

## 📦 Installation

### Standard Installation

```bash
# Clone repository
git clone https://github.com/Anmoldureha/polymarket-trading-bot-strategies.git
cd polymarket-trading-bot-strategies

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Verify Installation

```bash
# Run tests
pytest tests/ -v

# Run bot in paper mode
python main.py --paper
```

---

## ⚙️ Configuration

### Environment Variables

Create a `.env` file in the project root:

```env
# Polymarket API
POLYMARKET_API_KEY=your_api_key_here
POLYMARKET_PRIVATE_KEY=your_private_key_here

# Hyperliquid (for hedging strategy)
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_PRIVATE_KEY=0x...
```

### Configuration File (`config/config.yaml`)

Key settings:

```yaml
# General
paper_trading: true  # Set to false for live trading
polling_interval: 5.0  # Seconds between iterations

# Risk Management
risk:
  initial_capital: 100.0
  max_position_size: 10.0
  max_total_exposure: 100.0
  stop_loss_pct: 10.0
  max_drawdown_pct: 20.0

# Strategies
strategies:
  micro_spreads:
    enabled: true
    min_buy_price: 0.05
    max_buy_price: 0.10
    target_profit_pct: 120.0
  
  liquidity:
    enabled: true
    min_spread_pct: 2.0
  
  single_arbitrage:
    enabled: true
    max_total_price: 0.99
  
  # ... see config/config.yaml for full options
```

See [EXPLAINER.md](EXPLAINER.md) for detailed configuration documentation.

---

## 💻 Usage

### Basic Commands

```bash
# Paper trading (safe, no real money)
python main.py --paper

# Live trading (requires API credentials)
python main.py

# Custom config file
python main.py --config path/to/config.yaml

# Debug mode (verbose logging)
python main.py --paper --log-level DEBUG

# Watch trades in real-time
./scripts/run_and_watch.sh
```

### Running Strategies

Enable/disable strategies in `config/config.yaml`:

```yaml
strategies:
  micro_spreads:
    enabled: true  # Enable this strategy
  liquidity:
    enabled: false  # Disable this strategy
```

### Monitoring

The bot provides three log files:

- **`logs/trades.log`**: All executed trades (clean, readable)
- **`logs/errors.log`**: Errors and warnings only
- **`logs/bot.log`**: Complete activity log (everything)

Watch trades in real-time:
```bash
tail -f logs/trades.log
```

---

## 🛡️ Risk Management

PolyHFT includes comprehensive risk management:

### Position Limits
- **Max Position Size**: Maximum size per individual trade
- **Per-Market Exposure**: Maximum capital per market
- **Per-Strategy Exposure**: Maximum capital per strategy
- **Total Exposure**: Maximum total capital at risk

### Stop Losses
- Configurable stop-loss percentage per position
- Automatic position closure on stop-loss trigger

### Drawdown Protection
- Maximum drawdown percentage before trading stops
- Automatic shutdown if drawdown limit exceeded

### Best Practices

1. ✅ **Always start with paper trading** - Test for at least a few days
2. ✅ **Start small** - Use minimal position sizes initially
3. ✅ **Monitor closely** - Watch logs and Telegram notifications
4. ✅ **Understand strategies** - Read strategy documentation before enabling
5. ✅ **Set conservative limits** - Use default risk parameters initially

⚠️ **Never risk more than you can afford to lose**

---

## 🏗️ Architecture

```
PolyHFT/
├── src/
│   ├── api/              # API clients (Polymarket, Hyperliquid)
│   ├── strategies/       # Trading strategies (4 strategies)
│   ├── risk/             # Risk management system
│   ├── core/             # Core components (order coordinator, state manager)
│   ├── exchanges/        # Exchange adapters (modular design)
│   ├── utils/            # Utilities (logger, config, cache, telegram)
│   └── bot.py            # Main bot orchestrator
├── tests/                # Test suite
├── config/               # Configuration files
├── logs/                 # Log files
├── main.py              # Entry point
└── requirements.txt     # Dependencies
```

### Key Components

- **TradingBot**: Main orchestrator that coordinates strategies
- **RiskManager**: Centralized risk management and position tracking
- **OrderCoordinator**: Prevents conflicting orders and manages positions
- **MarketCache**: Reduces API calls by 90% with intelligent caching
- **Strategy Base Class**: Extensible architecture for adding new strategies

---

## 📚 Documentation

- **[EXPLAINER.md](EXPLAINER.md)**: Deep technical dive into strategies, architecture, and implementation
- **[QUICK_START.md](QUICK_START.md)**: Fastest way to get started
- **[TESTING_GUIDE.md](TESTING_GUIDE.md)**: Comprehensive testing instructions

### API Documentation

- [Polymarket API](https://docs.polymarket.com/)
- [Hyperliquid API](https://hyperliquid.gitbook.io/hyperliquid-docs/)

---

## 🔔 Telegram Notifications

Get real-time alerts on your phone:

1. Create a bot with [@BotFather](https://t.me/botfather)
2. Get your bot token
3. Send any message to your bot (e.g., `/start`)
4. Add to `config/config.yaml`:
   ```yaml
   telegram:
     bot_token: "your_bot_token_here"
   ```
5. The bot auto-detects your chat ID on first run

Notifications include:
- 🤖 Bot started/stopped
- 🎯 Trade executed
- ✅ Trade completed
- ⚠️ Errors and warnings

---

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_micro_spreads.py -v

# Run with coverage
pytest tests/ --cov=src --cov-report=html
```

See [TESTING_GUIDE.md](TESTING_GUIDE.md) for detailed testing instructions.

---

## 🤝 Contributing

Contributions are welcome! Please follow these guidelines:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass (`pytest tests/`)
6. Commit your changes (`git commit -m 'Add amazing feature'`)
7. Push to the branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

### Development Setup

```bash
# Install development dependencies
pip install -r requirements.txt
pip install pytest pytest-cov black flake8

# Run linter
flake8 src/

# Format code
black src/
```

---

## 📊 Performance

- **Market Fetching**: Batched orderbook requests with 5-second cache
- **Price Updates**: Up to 10 concurrent price requests
- **Strategy Execution**: Sequential strategy loop per iteration
- **Memory Usage**: Efficient caching with TTL-based invalidation

---

## 🔐 Security

- **Never commit API keys** - Use `.env` file (already in `.gitignore`)
- **Use environment variables** - Prefer over config file for sensitive data
- **Config file protection** - `config/config.yaml` is in `.gitignore` - use `config/config.yaml.example` as template
- **Paper trading first** - Always test before live trading
- **Start small** - Use minimal position sizes initially

---

## ⚠️ Disclaimer

**IMPORTANT RISK WARNING**

Trading involves significant risk of loss. This software is provided "as is" without warranty of any kind. By using this bot, you acknowledge that:

- You understand the risks involved in algorithmic trading
- You are solely responsible for your trading decisions
- Past performance does not guarantee future results
- You may lose all or more than your initial investment
- The authors are not responsible for any losses incurred

**Always:**
- Start with paper trading
- Use small position sizes
- Monitor the bot closely
- Understand each strategy before enabling
- Never risk more than you can afford to lose

---

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

---

## 🙏 Acknowledgments

- Polymarket for providing the prediction market platform
- Hyperliquid for perpetual futures integration
- The open-source community for various libraries and tools

---

## 📧 Support

- **Issues**: [GitHub Issues](https://github.com/Anmoldureha/polymarket-trading-bot-strategies/issues)
- **Discussions**: [GitHub Discussions](https://github.com/Anmoldureha/polymarket-trading-bot-strategies/discussions)

---

<div align="center">

**Made with ❤️ for the Polymarket trading community**

⭐ Star this repo if you find it useful!

</div>
