import streamlit as st
from agile_vbt_live_display import agile_vbt_live_display

st.title("Agile VBT Live Display development preview")
agile_vbt_live_display(
    source_mode="latest",
    recording_path=None,
    exercise="bench",
    history_seconds=90,
    paused=False,
)
