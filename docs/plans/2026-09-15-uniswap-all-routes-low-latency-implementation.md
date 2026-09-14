# Uniswap V2/V3/V4 Low-Latency Execution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable verified V2, V3, and V4 direct/single-bridge buy and sell execution with low signal-to-build latency and restart-safe state transitions.

**Architecture:** Keep the isolated trader service and native-ETH primary input. Extend the verified pool resolver and Uniswap adapter for V3 and V4 multihop, then remove worker polling and recovery deadlocks without adding trading strategy. All mainnet checks remain unsigned and non-broadcasting.

**Tech Stack:** Python 3.12, asyncio/aiohttp, eth-abi, eth-account, SQLite WAL, pytest, Ruff, Docker Compose.

---

### Task 1: V3 direct buy and sell

**Files:**
- Modify: `trader/uniswap.py`
- Modify: `tests/test_pools_uniswap.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/CONTRACTS.md`

**Steps:**
1. Add failing QuoterV2 exact-input and SwapRouter02 ETH buy/ERC-20 sell multicall tests.
2. Run the focused tests and confirm V3 currently fails closed.
3. Implement packed V3 paths, QuoterV2 decoding, exact-input calldata, native ETH payment and WETH unwrap on sell.
4. Run focused tests, Ruff and the complete suite.
5. Commit and push the independently verified V3 implementation.

### Task 2: V3 and V4 same-protocol single-bridge routes

**Files:**
- Modify: `trader/pools.py`
- Modify: `trader/uniswap.py`
- Modify: `tests/test_pools_uniswap.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/CONTRACTS.md`

**Steps:**
1. Add failing tests for V3 fee-tier bridge discovery and V4 `Initialize`-indexed bridge discovery.
2. Add failing quote/calldata tests for V3 packed multihop and V4 `SWAP_EXACT_IN` PathKey arrays.
3. Implement verified candidate discovery, best nonzero quote selection and reverse sell paths.
4. Run focused tests, Ruff and the complete suite.
5. Commit and push the route expansion.

### Task 3: Restart-safe order and approval state machine

**Files:**
- Modify: `trader/db.py`
- Modify: `trader/orders.py`
- Modify: `trader/positions.py`
- Modify: `trader/nonce.py`
- Modify: `trader/uniswap.py`
- Modify: `trader/worker.py`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_pools_uniswap.py`
- Modify: `docs/ARCHITECTURE.md`

**Steps:**
1. Add failing regressions for CREATED recovery, approval submission ambiguity, receipt-finalization crash boundaries, zero proceeds and multiple open positions.
2. Persist every signed operation before broadcast and distinguish approvals from BUY/SELL orders.
3. Make unsubmitted orders rebuildable, receipt finalization atomic and nonce reservations recoverable before signing.
4. Scan open positions fairly so one below-target position cannot starve later positions.
5. Run focused tests, Ruff and the complete suite; commit and push.

### Task 4: Event-driven low-latency dispatch

**Files:**
- Modify: `trader/api.py`
- Modify: `trader/main.py`
- Modify: `trader/worker.py`
- Modify: `trader/rpc.py`
- Modify: `tests/test_auth_api_db.py`
- Modify: `tests/test_worker.py`
- Modify: `docs/ARCHITECTURE.md`

**Steps:**
1. Add a failing test proving signal acceptance wakes an idle worker immediately.
2. Add a process-local wake event and bounded fallback polling.
3. Cache immutable contract-code checks and independently parallelize safe pool metadata reads.
4. Add phase timing facts without URLs, secrets or raw signed transactions.
5. Run focused/full verification; commit and push.

### Task 5: Real-chain unsigned route and runtime validation

**Files:**
- Create: `scripts/validate_routes.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/CONTRACTS.md`
- Modify: `docs/VALIDATION.md`

**Steps:**
1. Implement a read-only validator that loads recent signal fixtures, resolves routes, obtains quotes, builds calldata and uses `eth_call` without a private key or broadcast.
2. Validate public and Alchemy configuration selection without printing endpoint URLs.
3. Run Ruff, all tests, Docker build, dependency audit and the unsigned live-chain route validator.
4. Record route coverage and per-phase latency; keep `LIVE_TRADING_ENABLED=false`.
5. Commit and push the final validation artifacts.
