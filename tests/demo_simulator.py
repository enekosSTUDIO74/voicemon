"""Demo simulator — generates realistic voice call telemetry.

Simulates multiple concurrent voice sessions with realistic latency
distributions to populate the observability stack with demo data.

Usage:
    python -m tests.demo_simulator
    python -m tests.demo_simulator --sessions 50 --agent-id demo-agent
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time

from voicemon.core.collector import VoiceMonCollector
from voicemon.exporters.base import ConsoleExporter

logger = logging.getLogger("voicemon.demo")

# Realistic latency distributions (mean, stdev) in ms
PROFILES = {
    "good": {"stt": (150, 30), "llm": (300, 80), "tts": (120, 25)},
    "degraded": {"stt": (400, 100), "llm": (800, 200), "tts": (300, 80)},
    "bad": {"stt": (800, 200), "llm": (2000, 500), "tts": (600, 150)},
}

STT_PROVIDERS = ["deepgram", "google", "azure"]
LLM_MODELS = ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet"]
TTS_PROVIDERS = ["elevenlabs", "azure", "google"]

SAMPLE_TRANSCRIPTS = [
    "Hi, I'd like to check my account balance",
    "Can you help me schedule an appointment?",
    "What are your business hours?",
    "I need to report a problem with my order",
    "Tell me about your return policy",
    "I want to speak to a manager",
    "How do I reset my password?",
    "Can you transfer me to billing?",
    "I'd like to make a reservation for two people",
    "What's the status of my delivery?",
]


async def simulate_session(
    collector: VoiceMonCollector,
    *,
    agent_id: str = "demo-agent",
    profile: str = "good",
    framework: str = "livekit",
) -> str:
    """Simulate a single voice session with multiple turns."""
    latencies = PROFILES[profile]
    num_turns = random.randint(2, 8)

    session_id = collector.start_session(
        agent_id=agent_id,
        agent_version="0.1.0",
        framework=framework,
        metadata={"profile": profile, "simulated": True},
    )

    # Simulate infrastructure metrics
    collector.record_infra(
        session_id=session_id,
        jitter_ms=random.gauss(10, 5),
        packet_loss_pct=max(0, random.gauss(0.5, 0.3)),
        mos_score=min(5.0, max(1.0, random.gauss(4.2, 0.3))),
    )

    for turn_num in range(num_turns):
        collector.start_turn(session_id=session_id, speaker="user")

        # STT
        stt_latency = max(50, random.gauss(*latencies["stt"]))
        stt_confidence = min(1.0, max(0.1, random.gauss(0.92, 0.06)))
        collector.record_stt(
            session_id=session_id,
            provider=random.choice(STT_PROVIDERS),
            model="nova-2",
            latency_ms=stt_latency,
            confidence=stt_confidence,
            transcript=random.choice(SAMPLE_TRANSCRIPTS),
            is_final=True,
        )
        await asyncio.sleep(0.01)

        # LLM
        llm_ttft = max(50, random.gauss(*latencies["llm"]))
        input_tokens = random.randint(50, 300)
        output_tokens = random.randint(20, 150)
        collector.record_llm(
            session_id=session_id,
            provider="openai",
            model=random.choice(LLM_MODELS),
            ttft_ms=llm_ttft,
            total_ms=llm_ttft * random.uniform(1.5, 3.0),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await asyncio.sleep(0.01)

        # Occasional tool call
        if random.random() < 0.3:
            tool_name = random.choice(["lookup_account", "check_inventory", "schedule_appointment"])
            collector.record_llm(
                session_id=session_id,
                provider="tool_call",
                model=tool_name,
                ttft_ms=0,
                total_ms=random.uniform(50, 500),
                metadata={"function_name": tool_name, "success": random.random() > 0.1},
            )

        # TTS
        tts_ttfb = max(30, random.gauss(*latencies["tts"]))
        collector.record_tts(
            session_id=session_id,
            provider=random.choice(TTS_PROVIDERS),
            ttfb_ms=tts_ttfb,
            total_ms=tts_ttfb * random.uniform(2, 5),
            voice_id="EXAVITQu4vr4xnSDS" if random.random() > 0.5 else "pNInz6obpgDQGcFm",
        )

        # UX: E2E latency
        e2e = stt_latency + llm_ttft + tts_ttfb
        collector.record_ux(
            session_id=session_id,
            event_type="latency_snapshot",
            e2e_latency_ms=e2e,
        )

        # Random interruption
        if random.random() < 0.15:
            collector.record_ux(
                session_id=session_id,
                event_type="interruption",
                metadata={"source": "user_barge_in"},
            )

        collector.end_turn(session_id=session_id)
        await asyncio.sleep(0.02)

    # Outcome
    task_success = random.random() > 0.2 if profile == "good" else random.random() > 0.5
    collector.record_outcome(
        session_id=session_id,
        task_success=task_success,
        resolution_type=random.choice(["completed", "transferred", "abandoned", "escalated"]),
        metadata={"csat": random.randint(1, 5) if task_success else random.randint(1, 3)},
    )

    collector.end_session(session_id=session_id)
    return session_id


async def run_simulation(
    *,
    num_sessions: int = 20,
    agent_id: str = "demo-agent",
    concurrency: int = 5,
    use_redis: bool = False,
) -> None:
    """Run a batch of simulated sessions."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    exporters = []
    if use_redis:
        from voicemon.exporters.redis import RedisStreamsExporter
        redis_exp = RedisStreamsExporter()
        await redis_exp.connect()
        exporters.append(redis_exp)
    else:
        exporters.append(ConsoleExporter())

    collector = VoiceMonCollector(exporters=exporters)
    logger.info("Starting simulation: %d sessions, concurrency=%d", num_sessions, concurrency)

    sem = asyncio.Semaphore(concurrency)
    start = time.monotonic()

    async def bounded_session(i: int) -> str:
        async with sem:
            profile = random.choices(["good", "degraded", "bad"], weights=[0.7, 0.2, 0.1])[0]
            framework = random.choice(["livekit", "pipecat", "vapi"])
            return await simulate_session(
                collector, agent_id=agent_id, profile=profile, framework=framework,
            )

    session_ids = await asyncio.gather(*[bounded_session(i) for i in range(num_sessions)])
    elapsed = time.monotonic() - start

    logger.info(
        "✅ Simulation complete: %d sessions in %.1fs (%.0f sessions/s)",
        len(session_ids), elapsed, len(session_ids) / elapsed,
    )

    if use_redis:
        for exp in exporters:
            if hasattr(exp, "close"):
                await exp.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="VoiceMon Demo Simulator")
    parser.add_argument("--sessions", type=int, default=20, help="Number of sessions to simulate")
    parser.add_argument("--agent-id", type=str, default="demo-agent")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--redis", action="store_true", help="Export to Redis Streams")
    args = parser.parse_args()

    asyncio.run(run_simulation(
        num_sessions=args.sessions,
        agent_id=args.agent_id,
        concurrency=args.concurrency,
        use_redis=args.redis,
    ))


if __name__ == "__main__":
    main()
