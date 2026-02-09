"""VoiceMon Streamlit Analytics Dashboard.

Rich analytical dashboard for voice AI observability with:
- Real-time latency breakdown (STT → LLM → TTS → E2E)
- Call replay & turn-by-turn inspector
- STT confidence drift detection
- Cost analysis by provider/model
- Session explorer with filtering

Usage:
    streamlit run voicemon/dashboards/streamlit_app.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

# ── Page config ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="VoiceMon Analytics",
    page_icon="🎙️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── Database connection ─────────────────────────────────────────────────
@st.cache_resource
def get_connection():
    dsn = os.getenv("VOICEMON_TIMESCALE_DSN", "postgresql://voicemon:voicemon@localhost:5432/voicemon")
    import psycopg2
    return psycopg2.connect(dsn)


def run_query(query: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    return pd.read_sql(query, conn, params=params)


# ── Sidebar ─────────────────────────────────────────────────────────────
st.sidebar.title("🎙️ VoiceMon Analytics")
st.sidebar.markdown("Voice AI Observability Dashboard")

time_range = st.sidebar.selectbox("Time Range", ["1h", "6h", "24h", "7d", "30d"], index=2)
time_map = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}
since = datetime.now(timezone.utc) - timedelta(hours=time_map[time_range])

# Agent filter
try:
    agents_df = run_query("SELECT DISTINCT agent_id FROM sessions ORDER BY agent_id")
    agent_list = ["All"] + agents_df["agent_id"].tolist()
except Exception:
    agent_list = ["All"]
selected_agent = st.sidebar.selectbox("Agent", agent_list)

agent_filter = "" if selected_agent == "All" else f"AND s.agent_id = '{selected_agent}'"

st.sidebar.markdown("---")
auto_refresh = st.sidebar.checkbox("Auto-refresh (30s)", value=False)
if auto_refresh:
    st.rerun()


# ── Tab Layout ──────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 Overview", "⏱️ Latency", "🎯 Quality", "📞 Sessions", "💰 Cost",
])

# ═══════════════════════════════════════════════════════════════════════
# TAB 1: Overview
# ═══════════════════════════════════════════════════════════════════════
with tab1:
    st.header("Pipeline Health Overview")

    try:
        # KPI row
        kpi_query = f"""
        SELECT
            COUNT(DISTINCT s.id) AS total_sessions,
            AVG(s.duration_ms) / 1000 AS avg_duration_s,
            (SELECT COUNT(*) FROM turns t WHERE t.started_at > %s) AS total_turns,
            (SELECT COUNT(*) FROM ux_events u WHERE u.event_type = 'interruption'
             AND u.time > %s) AS total_interruptions,
            (SELECT AVG(confidence) FROM stt_events WHERE time > %s AND is_final) AS avg_confidence
        FROM sessions s
        WHERE s.started_at > %s {agent_filter}
        """
        kpis = run_query(kpi_query, (since, since, since, since))

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Sessions", int(kpis["total_sessions"].iloc[0] or 0))
        c2.metric("Avg Duration", f"{(kpis['avg_duration_s'].iloc[0] or 0):.1f}s")
        c3.metric("Total Turns", int(kpis["total_turns"].iloc[0] or 0))
        c4.metric("Interruptions", int(kpis["total_interruptions"].iloc[0] or 0))
        c5.metric("Avg STT Confidence", f"{(kpis['avg_confidence'].iloc[0] or 0):.1%}")
    except Exception as e:
        st.warning(f"Could not load KPIs: {e}")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Sessions Over Time")
        try:
            sessions_ts = run_query(f"""
                SELECT time_bucket('1 hour', started_at) AS hour, COUNT(*) AS sessions
                FROM sessions s WHERE started_at > %s {agent_filter}
                GROUP BY hour ORDER BY hour
            """, (since,))
            if not sessions_ts.empty:
                st.line_chart(sessions_ts.set_index("hour"))
        except Exception as e:
            st.info(f"No session data: {e}")

    with col2:
        st.subheader("Outcome Distribution")
        try:
            outcomes = run_query(f"""
                SELECT
                    CASE WHEN o.task_success THEN 'Success' ELSE 'Failure' END AS outcome,
                    COUNT(*) AS count
                FROM outcome_events o
                JOIN sessions s ON o.session_id = s.id
                WHERE o.time > %s {agent_filter}
                GROUP BY outcome
            """, (since,))
            if not outcomes.empty:
                st.bar_chart(outcomes.set_index("outcome"))
        except Exception as e:
            st.info(f"No outcome data: {e}")


# ═══════════════════════════════════════════════════════════════════════
# TAB 2: Latency Breakdown
# ═══════════════════════════════════════════════════════════════════════
with tab2:
    st.header("⏱️ Pipeline Latency Analysis")

    # Latency budget breakdown
    try:
        latency_df = run_query(f"""
            SELECT
                (SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) FROM stt_events WHERE time > %s) AS stt_p50,
                (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FROM stt_events WHERE time > %s) AS stt_p95,
                (SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY ttft_ms) FROM llm_events WHERE time > %s AND provider != 'tool_call') AS llm_p50,
                (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms) FROM llm_events WHERE time > %s AND provider != 'tool_call') AS llm_p95,
                (SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY ttfb_ms) FROM tts_events WHERE time > %s) AS tts_p50,
                (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY ttfb_ms) FROM tts_events WHERE time > %s) AS tts_p95
        """, (since, since, since, since, since, since))

        if not latency_df.empty:
            st.subheader("Latency Budget (P50 / P95)")
            budget = pd.DataFrame({
                "Stage": ["STT", "LLM (TTFT)", "TTS (TTFB)", "Total"],
                "P50 (ms)": [
                    latency_df["stt_p50"].iloc[0] or 0,
                    latency_df["llm_p50"].iloc[0] or 0,
                    latency_df["tts_p50"].iloc[0] or 0,
                    (latency_df["stt_p50"].iloc[0] or 0) +
                    (latency_df["llm_p50"].iloc[0] or 0) +
                    (latency_df["tts_p50"].iloc[0] or 0),
                ],
                "P95 (ms)": [
                    latency_df["stt_p95"].iloc[0] or 0,
                    latency_df["llm_p95"].iloc[0] or 0,
                    latency_df["tts_p95"].iloc[0] or 0,
                    (latency_df["stt_p95"].iloc[0] or 0) +
                    (latency_df["llm_p95"].iloc[0] or 0) +
                    (latency_df["tts_p95"].iloc[0] or 0),
                ],
                "Budget (ms)": [300, 500, 200, 1000],
            })
            st.dataframe(budget, use_container_width=True, hide_index=True)

            # Stacked bar visualizing budget usage
            budget_pct = budget.iloc[:3].copy()
            budget_pct["P50 Usage %"] = (budget_pct["P50 (ms)"] / budget_pct["Budget (ms)"] * 100).round(1)
            budget_pct["P95 Usage %"] = (budget_pct["P95 (ms)"] / budget_pct["Budget (ms)"] * 100).round(1)
            st.bar_chart(budget_pct.set_index("Stage")[["P50 Usage %", "P95 Usage %"]])
    except Exception as e:
        st.info(f"No latency data: {e}")

    # Latency over time
    st.subheader("STT Latency Over Time")
    try:
        stt_ts = run_query("""
            SELECT time_bucket('5 minutes', time) AS bucket,
                   AVG(latency_ms) AS avg_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
            FROM stt_events WHERE time > %s
            GROUP BY bucket ORDER BY bucket
        """, (since,))
        if not stt_ts.empty:
            st.line_chart(stt_ts.set_index("bucket"))
    except Exception as e:
        st.info(f"No STT time series: {e}")


# ═══════════════════════════════════════════════════════════════════════
# TAB 3: Quality / Drift Detection
# ═══════════════════════════════════════════════════════════════════════
with tab3:
    st.header("🎯 STT Quality & Drift Detection")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Confidence Trend")
        try:
            conf_ts = run_query("""
                SELECT time_bucket('15 minutes', time) AS bucket,
                       AVG(confidence) AS avg_confidence,
                       MIN(confidence) AS min_confidence,
                       COUNT(*) FILTER (WHERE confidence < 0.7) AS low_count,
                       COUNT(*) AS total
                FROM stt_events WHERE time > %s AND is_final
                GROUP BY bucket ORDER BY bucket
            """, (since,))
            if not conf_ts.empty:
                st.line_chart(conf_ts.set_index("bucket")[["avg_confidence", "min_confidence"]])
                # Drift alert
                if len(conf_ts) > 4:
                    recent = conf_ts["avg_confidence"].tail(4).mean()
                    baseline = conf_ts["avg_confidence"].head(len(conf_ts) // 2).mean()
                    drift = baseline - recent
                    if drift > 0.05:
                        st.error(f"⚠️ STT confidence drift detected: -{drift:.1%} from baseline")
                    else:
                        st.success("✅ STT confidence stable")
        except Exception as e:
            st.info(f"No confidence data: {e}")

    with col2:
        st.subheader("Low Confidence Transcripts")
        try:
            low_conf = run_query("""
                SELECT time, confidence, transcript, provider, model
                FROM stt_events
                WHERE time > %s AND is_final AND confidence < 0.7
                ORDER BY confidence ASC LIMIT 20
            """, (since,))
            if not low_conf.empty:
                st.dataframe(low_conf, use_container_width=True, hide_index=True)
            else:
                st.success("No low-confidence transcripts")
        except Exception as e:
            st.info(f"No data: {e}")


# ═══════════════════════════════════════════════════════════════════════
# TAB 4: Session Explorer
# ═══════════════════════════════════════════════════════════════════════
with tab4:
    st.header("📞 Session Explorer")

    try:
        sessions = run_query(f"""
            SELECT s.id, s.agent_id, s.framework, s.started_at, s.duration_ms,
                   s.status, s.metadata
            FROM sessions s
            WHERE s.started_at > %s {agent_filter}
            ORDER BY s.started_at DESC LIMIT 50
        """, (since,))

        if not sessions.empty:
            st.dataframe(
                sessions[["id", "agent_id", "framework", "started_at", "duration_ms", "status"]],
                use_container_width=True, hide_index=True,
            )

            # Session drill-down
            st.subheader("Session Detail")
            session_id = st.selectbox("Select session", sessions["id"].tolist())

            if session_id:
                # Turn-by-turn view
                turns = run_query("""
                    SELECT turn_number, speaker, duration_ms, interrupted
                    FROM turns WHERE session_id = %s ORDER BY turn_number
                """, (str(session_id),))

                if not turns.empty:
                    st.markdown("**Turn-by-Turn Breakdown:**")
                    st.dataframe(turns, use_container_width=True, hide_index=True)

                # Pipeline events for this session
                st.markdown("**STT Events:**")
                stt_evts = run_query("""
                    SELECT time, provider, latency_ms, confidence, transcript
                    FROM stt_events WHERE session_id = %s AND is_final
                    ORDER BY time
                """, (str(session_id),))
                if not stt_evts.empty:
                    st.dataframe(stt_evts, use_container_width=True, hide_index=True)

                st.markdown("**LLM Events:**")
                llm_evts = run_query("""
                    SELECT time, provider, model, ttft_ms, total_ms, input_tokens, output_tokens
                    FROM llm_events WHERE session_id = %s ORDER BY time
                """, (str(session_id),))
                if not llm_evts.empty:
                    st.dataframe(llm_evts, use_container_width=True, hide_index=True)
        else:
            st.info("No sessions found in the selected time range")
    except Exception as e:
        st.info(f"No session data: {e}")


# ═══════════════════════════════════════════════════════════════════════
# TAB 5: Cost Analysis
# ═══════════════════════════════════════════════════════════════════════
with tab5:
    st.header("💰 Cost Analysis")

    # LLM token costs (estimated)
    st.subheader("LLM Token Usage by Model")
    try:
        token_usage = run_query("""
            SELECT provider, model,
                   SUM(input_tokens) AS total_input,
                   SUM(output_tokens) AS total_output,
                   COUNT(*) AS requests
            FROM llm_events
            WHERE time > %s AND provider != 'tool_call'
            GROUP BY provider, model
            ORDER BY total_input + total_output DESC
        """, (since,))

        if not token_usage.empty:
            # Estimated costs (configurable)
            COST_PER_1K_INPUT = {"gpt-4o": 0.0025, "gpt-4o-mini": 0.00015,
                                 "claude-3-5-sonnet": 0.003, "default": 0.001}
            COST_PER_1K_OUTPUT = {"gpt-4o": 0.01, "gpt-4o-mini": 0.0006,
                                  "claude-3-5-sonnet": 0.015, "default": 0.003}

            costs = []
            for _, row in token_usage.iterrows():
                model = row["model"]
                in_cost = (row["total_input"] / 1000) * COST_PER_1K_INPUT.get(model, COST_PER_1K_INPUT["default"])
                out_cost = (row["total_output"] / 1000) * COST_PER_1K_OUTPUT.get(model, COST_PER_1K_OUTPUT["default"])
                costs.append({
                    "Provider": row["provider"],
                    "Model": model,
                    "Input Tokens": int(row["total_input"]),
                    "Output Tokens": int(row["total_output"]),
                    "Requests": int(row["requests"]),
                    "Est. Cost ($)": round(in_cost + out_cost, 4),
                })
            cost_df = pd.DataFrame(costs)
            st.dataframe(cost_df, use_container_width=True, hide_index=True)
            st.metric("Total Estimated LLM Cost", f"${cost_df['Est. Cost ($)'].sum():.2f}")
        else:
            st.info("No LLM usage data")
    except Exception as e:
        st.info(f"No cost data: {e}")

    # Cost over time
    st.subheader("Token Usage Trend")
    try:
        token_ts = run_query("""
            SELECT time_bucket('1 hour', time) AS hour,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens
            FROM llm_events WHERE time > %s AND provider != 'tool_call'
            GROUP BY hour ORDER BY hour
        """, (since,))
        if not token_ts.empty:
            st.area_chart(token_ts.set_index("hour"))
    except Exception as e:
        st.info(f"No token trend: {e}")


# ── Footer ──────────────────────────────────────────────────────────────
st.sidebar.markdown("---")
st.sidebar.markdown("**VoiceMon** v0.1.0")
st.sidebar.markdown("4-Layer Voice Observability")
