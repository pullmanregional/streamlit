# Add main repo directory to include path to access common/ modules
import sys, os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


import streamlit as st
from src import route
from src.model import source_data
from src.ui import dashboard
from common import auth, st_util


def run():
    """Main streamlit app entry point"""
    # # Authenticate user with single sign-on
    # user = auth.oidc_auth()
    # if not user:
    #     return st.stop()

    # 2nd layer password auth
    if not auth.simple_auth():
        return st.stop()

    # Read, parse, and cache (via @st.cache_data) source data
    src_data = source_data.read()

    # Handle routing based on query parameters
    route_id = route.route_by_query(st.query_params)

    # Check for API resources first
    if route_id == route.CLEAR_CACHE:
        return st_util.st_clear_cache_page()

    # Render page based on the route
    if src_data is None:
        st_util.st_center_text("No data available. Please contact administrator.")
    else:
        return dashboard.show(src_data)


st.set_page_config(
    page_title="RVU Dashboard - Pediatrics",
    page_icon=":material/leaderboard:",
    layout="wide",
    menu_items=None,
)
run()
