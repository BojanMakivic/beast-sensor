import streamlit as st
from beast_live_display import beast_live_display

st.title("Beast Live Display development preview")
beast_live_display(
    source_mode="latest",
    recording_path=None,
    exercise="bench",
    history_seconds=90,
    paused=False,
)
