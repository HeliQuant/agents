"""Research Agent — sentiment + news summary for the target trading pair.

V1 stub: returns deterministic mock so the orchestration loop works end-to-end.
V2 (real): scrape X / news APIs / Reddit, summarize with Claude, return sentiment.
"""

from __future__ import annotations

import structlog

from firm.schemas import ResearchInput, ResearchOutput

log = structlog.get_logger()


async def run(inputs: ResearchInput) -> ResearchOutput:
    log.info("research.run", pair=inputs.pair, lookback_hours=inputs.lookback_hours)

    # TODO(v2): pull from news API + X timelines + Reddit, summarize with Claude.
    # For now return a neutral stub so the downstream Signal Agent has data.
    return ResearchOutput(
        pair=inputs.pair,
        sentiment_score=0.05,
        summary=(
            "Stub research output. Real implementation will scan X, news, Reddit, "
            "and Allora topic activity over the past lookback window and summarize "
            "with Claude."
        ),
        notable_events=[],
    )
