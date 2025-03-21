# Add main repo directory to include path to access common/ modules
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))


import streamlit as st
from src import route, ui
from src.model import source_data
from src.ui import explore
from common import auth, st_util


def run():
    """Main streamlit app entry point"""
    # Authenticate user
    user = auth.oidc_auth()
    if not user:
        return st.stop()

    # Read, parse, and cache (via @st.cache_data) source data
    with st.spinner("Initializing..."):
        src_data = source_data.from_s3()

    # Handle routing based on query parameters
    route_id = route.route_by_query(st.query_params)

    # Check for API resources first
    if route_id == route.CLEAR_CACHE:
        return st_util.st_clear_cache_page()

    # Render page based on the route
    if src_data is None:
        st_util.st_center_text("No data available. Please contact administrator.")
    else:
        return explore.show(src_data)


# st.set_page_config(
#     page_title="Data Explorer", layout="wide", initial_sidebar_state="auto"
# )
run()
