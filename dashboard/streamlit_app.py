"""Streamlit dashboard for the Devin Maintenance Orchestrator.

Filename note: this file is `streamlit_app.py`, NOT `app.py`. Streamlit
imports its target under the filename's stem as a module — calling it
`app.py` would register it as `app` and shadow our `app/` package on
`from app import db`. The rename avoids the conflict.

Reads directly from the SQLite DB the orchestrator writes to. The Streamlit
service mounts the same volume as the API service in docker-compose, so
the dashboard always reflects ground truth without an HTTP roundtrip.

Auto-refresh uses st.fragment(run_every=N) — the native mechanism since
Streamlit 1.37 (out of experimental in 1.40). Only the wrapped function's
output re-renders; the title, sidebar, and expanders stay put. No
external component, no frontend asset loading, no flicker.

Layout (top to bottom):
  1. Hero — the 5 numbers a VP of Eng would want to know  (live)
  2. Live sessions table — every work unit with drill-down links  (live)
  3. Throughput chart — sessions over time, stacked by status  (live)
  4. Raw /metrics payload + label config (static, in expanders)
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the app package is importable when streamlit runs this file directly.
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import db, metrics, settings  # noqa: E402

st.set_page_config(
    page_title="Devin Maintenance Orchestrator",
    page_icon=":shield:",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ---------- helpers ----------

# Emoji prefix renders consistently in dataframe text cells. The
# `:color-background[...]` markdown only works in st.markdown/write,
# not inside st.dataframe.
STATUS_EMOJI = {
    "reserving":       "⚪",
    "pending":         "🔵",
    "running":         "🔵",
    "blocked":         "🟡",
    "needs_attention": "🟡",
    "completed":       "🟢",
    "failed":          "🔴",
    "timeout":         "🔴",
    "cancelled":       "⚫",
}


def status_label(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '⚪')} {status}"


def fmt_duration(start: str | None, end: str | None, status: str) -> str:
    """Elapsed time, but only for sessions genuinely still running. A
    terminal row with no completed_at returns '—' rather than 'still
    running 11 hours.'"""
    if not start:
        return "—"
    is_terminal = status in db.TERMINAL_STATUSES
    if is_terminal and not end:
        return "—"
    try:
        start_ts = pd.to_datetime(start)
        end_ts = pd.to_datetime(end) if end else pd.Timestamp.now(tz=start_ts.tz)
        total_s = int((end_ts - start_ts).total_seconds())
        if total_s < 60:
            return f"{total_s}s"
        if total_s < 3600:
            return f"{total_s // 60}m {total_s % 60}s"
        return f"{total_s // 3600}h {(total_s % 3600) // 60}m"
    except Exception:
        return "—"


# ---------- sidebar (outside the fragment — controls the fragment's cadence) ----------

with st.sidebar:
    st.header("Controls")
    refresh_secs = st.slider("Auto-refresh (sec)", 2, 60, 10)
    show_terminal = st.checkbox("Include terminal sessions", value=True)
    label_filter = st.multiselect(
        "Filter by label",
        options=[settings.SECURITY_LABEL, settings.QUALITY_LABEL,
                 settings.CI_FIX_LABEL, settings.GATE_LABEL],
        default=[],
    )
    st.caption(f"DB: `{db.DB_PATH}`")
    st.caption(f"Fork: `{settings.FORK_REPO}`")


# ---------- static header ----------

st.title(":shield: Devin Maintenance Orchestrator")
st.caption(
    "Event-driven control plane that routes GitHub maintenance work to Devin. "
    "Dependabot stops when CI fails — Devin closes the loop."
)


# ---------- dynamic content: rerun on the slider's interval ----------

@st.fragment(run_every=refresh_secs)
def live_section():
    rows = db.sessions.list_recent(limit=500)
    m = metrics.compute_dashboard_metrics(rows)

    # ----- hero -----
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Active", m["active_sessions"],
              help="reserving + pending + running + blocked + needs_attention")
    c2.metric("PRs opened", m["pr_created"])
    c3.metric(
        "Success rate",
        f"{int(m['success_rate'] * 100)}%" if m["pr_created"] or m["total_sessions"] else "—",
        help="Only sessions that produced a PR count as success (terminal sessions only)",
    )
    c4.metric("ACUs spent", f"{m['total_acus_consumed']:.1f}")
    c5.metric(
        "Eng-hours saved",
        f"{int(m['estimated_hours_saved'])}",
        delta=f"≈ ${m['estimated_dollars_saved']:,}",
        help=(
            f"{settings.HOURS_SAVED_PER_COMPLETED_SESSION}h"
            f" × ${settings.ENGINEER_HOURLY_RATE_USD}/h × PRs created"
        ),
    )

    if m["needs_human"]:
        st.warning(
            f":construction: {m['needs_human']} session(s) need a human — "
            "check the table below."
        )
    if m["completed_without_pr"]:
        st.info(
            f":information_source: {m['completed_without_pr']} session(s) "
            "finished without opening a PR."
        )

    # ----- sessions table -----
    st.subheader("Sessions")
    visible = rows
    if not show_terminal:
        visible = [r for r in visible if r.status in db.ACTIVE_STATUSES]
    if label_filter:
        visible = [r for r in visible if r.label in label_filter]

    if visible:
        df = pd.DataFrame([{
            "Status":   status_label(r.status),
            "Trigger":  r.trigger_type,
            "Label":    r.label or "—",
            "Issue":    r.trigger_ref or "",
            "Devin":    r.devin_url or "",
            "PR":       r.pr_url or "",
            "ACUs":     f"{r.acus_consumed:.2f}" if r.acus_consumed else "—",
            "Duration": fmt_duration(r.started_at, r.completed_at, r.status),
            "Started":  r.started_at,
            "Detail":   (r.error_message or r.raw_status or "")[:60],
        } for r in visible])
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Status": st.column_config.TextColumn("Status", width="small"),
                "Issue":  st.column_config.LinkColumn("Issue", display_text=r"#(\d+)"),
                "Devin":  st.column_config.LinkColumn("Devin", display_text="open"),
                "PR":     st.column_config.LinkColumn("PR", display_text="open"),
            },
        )
    else:
        st.info("No sessions yet. File a GitHub issue with labels "
                "`devin-remediate` + `devin-security`.")

    # ----- throughput chart -----
    st.subheader("Throughput")
    if rows:
        df_t = pd.DataFrame([{
            "hour": pd.to_datetime(r.started_at).floor("h"),
            "status": r.status,
        } for r in rows])
        pivoted = (
            df_t.groupby(["hour", "status"]).size()
            .unstack(fill_value=0).sort_index()
        )
        st.bar_chart(pivoted, height=200)
    else:
        st.caption("No throughput data yet.")

    # Subtle timestamp so you can see refresh is happening.
    st.caption(f"Last refresh: {pd.Timestamp.now().strftime('%H:%M:%S')}")


live_section()


# ---------- static drill-down expanders ----------

with st.expander("Raw `/metrics` payload", expanded=False):
    st.json(metrics.compute_dashboard_metrics(db.sessions.list_recent(limit=500)))
with st.expander("Routing labels in use", expanded=False):
    st.write({
        "gate":     settings.GATE_LABEL,
        "security": settings.SECURITY_LABEL,
        "quality":  settings.QUALITY_LABEL,
        "ci_fix":   settings.CI_FIX_LABEL,
    })
