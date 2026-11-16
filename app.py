"""
Pitcher Workload and Mechanics Monitor: Streamlit app.

Run locally:  streamlit run app.py
Needs data/workload.db, which pipeline.py builds.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path(__file__).parent / "data" / "workload.db"

GREEN = "#1E4D3A"     # deep scoreboard green
CHALK = "#FAFAF7"
CLAY = "#B5653A"      # infield clay, used for caution
RED = "#B3261E"       # alerts
GREY = "#8A948E"

SAFE_LOW, SAFE_HIGH, DANGER = 0.8, 1.3, 1.5

st.set_page_config(page_title="Pitcher Workload Monitor", page_icon="⚾", layout="wide")


@st.cache_data
def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    with sqlite3.connect(DB_PATH) as con:
        pitchers = pd.read_sql("SELECT * FROM pitchers ORDER BY total_pitches DESC", con)
        outings = pd.read_sql("SELECT * FROM outings", con, parse_dates=["game_date"])
    return pitchers, outings


def base_layout(fig: go.Figure, title: str, y_title: str, height: int = 320) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, x=0, font=dict(size=16)),
        height=height,
        margin=dict(l=10, r=10, t=50, b=10),
        plot_bgcolor="white",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        legend=dict(orientation="h", y=-0.2),
        yaxis_title=y_title,
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="#E6E9E4")
    return fig


def pitch_count_chart(o: pd.DataFrame) -> go.Figure:
    colors = [RED if f else GREEN for f in o["high_load_flag"]]
    fig = go.Figure(go.Bar(
        x=o["game_date"], y=o["pitches"], marker_color=colors, name="Pitches",
        customdata=o[["days_rest"]],
        hovertemplate="%{y} pitches<br>%{customdata[0]} days rest<extra></extra>",
    ))
    return base_layout(fig, "Pitches per outing", "Pitches")


def acwr_chart(o: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_hrect(y0=SAFE_LOW, y1=SAFE_HIGH, fillcolor=GREEN, opacity=0.10, line_width=0,
                  annotation_text="Target range", annotation_position="top left")
    fig.add_hrect(y0=DANGER, y1=max(DANGER + 0.5, o["acwr"].max() + 0.2), fillcolor=RED,
                  opacity=0.08, line_width=0, annotation_text="Above 1.5",
                  annotation_position="top left")

    early = o[o["acwr_reliable"] == 0]
    ready = o[o["acwr_reliable"] == 1]
    fig.add_trace(go.Scatter(
        x=early["game_date"], y=early["acwr"], mode="markers",
        name="Not enough history (first 28 days, or returning from a layoff)",
        marker=dict(color=GREY, size=7, symbol="circle-open"),
    ))
    fig.add_trace(go.Scatter(
        x=ready["game_date"], y=ready["acwr"], mode="lines+markers", name="Workload ratio",
        line=dict(color=GREEN, width=2), marker=dict(size=7, color=GREEN),
    ))
    return base_layout(fig, "Acute to chronic workload ratio (7 day vs 28 day), for context", "Ratio")


def velo_chart(o: pd.DataFrame, pitch: str) -> go.Figure:
    q = o.dropna(subset=["fb_velo"])
    mean, sd = q["fb_velo"].mean(), q["fb_velo"].std()
    fig = go.Figure()
    fig.add_hrect(y0=mean - 2 * sd, y1=mean + 2 * sd, fillcolor=GREEN, opacity=0.10, line_width=0,
                  annotation_text="Season range", annotation_position="top left")
    fig.add_hline(y=mean, line=dict(color=GREEN, dash="dot", width=1))
    fig.add_trace(go.Scatter(
        x=q["game_date"], y=q["fb_velo"], mode="lines+markers", name=f"{pitch} velocity",
        line=dict(color=GREEN, width=2),
        marker=dict(size=8, color=[RED if f else GREEN for f in q["velo_drop_flag"]]),
        hovertemplate="%{y:.1f} mph<extra></extra>",
    ))
    return base_layout(fig, f"Average {pitch} velocity by outing", "mph")


def release_chart(o: pd.DataFrame) -> go.Figure:
    q = o.dropna(subset=["fb_release_x", "fb_release_z"])
    flagged = q["release_drift_flag"] == 1
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=q.loc[~flagged, "fb_release_x"], y=q.loc[~flagged, "fb_release_z"], mode="markers",
        name="Normal outing", marker=dict(color=GREEN, size=8, opacity=0.6),
        text=q.loc[~flagged, "game_date"].dt.strftime("%b %d"),
        hovertemplate="%{text}<br>Side %{x:.2f} ft, height %{y:.2f} ft<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=q.loc[flagged, "fb_release_x"], y=q.loc[flagged, "fb_release_z"], mode="markers",
        name="Outside season range", marker=dict(color=RED, size=10, symbol="diamond"),
        text=q.loc[flagged, "game_date"].dt.strftime("%b %d"),
        hovertemplate="%{text}<br>Side %{x:.2f} ft, height %{y:.2f} ft<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[q["fb_release_x"].mean()], y=[q["fb_release_z"].mean()], mode="markers",
        name="Season average", marker=dict(color=CLAY, size=14, symbol="x"),
    ))
    fig = base_layout(fig, "Release point by outing (catcher's view)", "Height (ft)", height=380)
    fig.update_layout(hovermode="closest", xaxis_title="Side (ft)")
    return fig


# Page -----------------------------------------------------------------------

if not DB_PATH.exists():
    st.error("No data found. Run `python pipeline.py` first to build data/workload.db.")
    st.stop()

pitchers, outings = load()

with st.sidebar:
    st.header("Pitcher")
    name = st.selectbox("Choose a pitcher", pitchers["name"], label_visibility="collapsed")
    st.caption(
        "Data: Baseball Savant Statcast, regular season. "
        "Mechanics use each pitcher's most thrown fastball."
    )

p = pitchers[pitchers["name"] == name].iloc[0]
o = outings[outings["pitcher_id"] == p["pitcher_id"]].sort_values("game_date")
role = o["role"].iloc[0]

st.title(name)
st.write(f"{role}, throws {'right' if p['throws'] == 'R' else 'left'}, primary fastball {p['primary_fastball']}")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Outings", len(o))
c2.metric("Pitches", f"{int(o['pitches'].sum()):,}")
c3.metric("Median days of rest", f"{o['days_rest'].median():.0f}")
c4.metric("High workload outings", int(o["high_load_flag"].sum()))
c5.metric("Mechanics alerts", int(o["mechanics_flag"].sum()))

tab_load, tab_mech, tab_flags, tab_staff = st.tabs(
    ["Workload", "Mechanics", "Flagged outings", "Staff comparison"]
)

with tab_load:
    st.plotly_chart(pitch_count_chart(o), width="stretch")
    st.plotly_chart(acwr_chart(o), width="stretch")
    st.caption(
        "Red bars are flagged outings: short rest for starters, a third appearance in three days or "
        "five in a week for relievers, or a pitch count well above the pitcher's own normal. "
        "The ratio compares the last 7 days of pitches to the weekly average over 28 days. It is shown "
        "for context only, because a normal five day rotation keeps starters near 1.5 on a healthy schedule."
    )

with tab_mech:
    if o["z_velo"].notna().sum() == 0:
        st.info("Not enough fastball outings to set a season range for this pitcher.")
    st.plotly_chart(velo_chart(o, p["primary_fastball"]), width="stretch")
    st.plotly_chart(release_chart(o), width="stretch")
    st.caption("Red marks are outings more than 2 standard deviations from this pitcher's season average.")

with tab_flags:
    flagged = o[o["any_flag"] == 1].copy()
    if flagged.empty:
        st.success("No flagged outings this season.")
    else:
        flagged["Reason"] = flagged.apply(
            lambda r: ", ".join(
                label for col, label in [
                    ("short_rest_flag", "short rest"),
                    ("reliever_usage_flag", "heavy reliever usage"),
                    ("pitch_count_flag", "high pitch count"),
                    ("velo_drop_flag", "velocity drop"),
                    ("release_drift_flag", "release point drift"),
                ] if r[col]
            ),
            axis=1,
        )
        st.dataframe(
            flagged[["game_date", "pitches", "days_rest", "acwr", "fb_velo", "Reason"]]
            .rename(columns={"game_date": "Date", "pitches": "Pitches", "days_rest": "Days rest",
                             "acwr": "Workload ratio", "fb_velo": "Fastball mph"}),
            hide_index=True, width="stretch",
            column_config={"Date": st.column_config.DateColumn(format="MMM D, YYYY")},
        )

with tab_staff:
    staff = (
        outings.assign(acwr_ready=outings["acwr"].where(outings["acwr_reliable"] == 1))
        .merge(pitchers[["pitcher_id", "name"]], on="pitcher_id")
        .groupby("name")
        .agg(
            Outings=("pitches", "size"),
            Pitches=("pitches", "sum"),
            **{"Avg days rest": ("days_rest", "mean"),
               "Peak ratio": ("acwr_ready", "max"),
               "Workload flags": ("high_load_flag", "sum"),
               "Mechanics alerts": ("mechanics_flag", "sum")},
        )
        .sort_values("Pitches", ascending=False)
        .round(2)
        .reset_index()
        .rename(columns={"name": "Pitcher"})
    )
    st.dataframe(staff, hide_index=True, width="stretch")
