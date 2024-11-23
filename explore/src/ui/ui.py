"""
Methods to show build streamlit UI
"""
import streamlit as st
from pandasai import SmartDatalake
from pandasai.llm.openai import OpenAI
from .streamlit_response import StreamlitResponse
from ..model import source_data, app_data, settings

def show_settings(src_data: source_data.SourceData):
    return settings.Settings()


def show_content(settings: settings.Settings, data: app_data.AppData):
    dataset = st.selectbox(
        "Choose a dataset to explore:",
        ["Patients and Encounters", "Volumes", "Financial"],
    )

    st.subheader("Analysis")

    analysis_tabs = st.tabs(["Analysis", "Full Data"])

    with analysis_tabs[0]:

        # Get the currently active dataframe based on dataset selection
        active_dfs, dataset_prompt = None, ""
        if dataset == "Patients and Encounters":
            dataset_prompt = data.encounters.ai_prompt
            active_dfs = [data.encounters.patients_df, data.encounters.encounters_df]
        elif dataset == "Volumes":
            active_dfs = [
                data.volumes.volumes_df,
                data.volumes.uos_df,
                data.volumes.hours_df,
                data.volumes.contracted_hours_df,
            ]
        else:
            active_dfs = [data.finance.budget_df, data.finance.income_stmt_df]

        # Chat input and clear button in columns
        container = st.container()
        query = container.text_input(
            "Ask a question:", placeholder="e.g. How many patients were seen in 2023?", 
        )
        if query:
            container.chat_message("user").write(query)

            # Prefix our query with custom prompts to help with context
            query = construct_prompt("", dataset_prompt, query)

            try:
                llm = OpenAI(
                    api_token=settings.openai_api_key, model="gpt-4o-mini", verbose=True
                )
                tabs = container.tabs(["Result", "Debug"])
                smart_df = SmartDatalake(
                    active_dfs,
                    config={
                        "llm": llm,
                        "custom_whitelisted_dependencies": ["plotly"],
                        "response_parser": StreamlitResponse.parser_class_factory(
                            tabs[0]
                        ),
                    },
                )

                with st.spinner("Analyzing..."):
                    response = smart_df.chat(query)
                    if response is not None:
                        tabs[0].write(response)
                    with tabs[1]:
                        st.write("Prompt:")
                        st.code(query, language="text")
                        st.write("Code:")
                        st.code(smart_df.last_code_generated)

            except Exception as e:
                st.error(f"Error analyzing data: {str(e)}")

    with analysis_tabs[1]:
        if dataset == "Patients and Encounters":
            tabs = st.tabs(["Patients", "Encounters"])
            with tabs[0]:
                st.dataframe(
                    data.encounters.patients_df.pandas_df,
                    column_config={
                        "prw_id": st.column_config.NumberColumn(format="%d")
                    },
                    hide_index=True,
                )
            with tabs[1]:
                st.dataframe(
                    data.encounters.encounters_df.pandas_df,
                    column_config={
                        "prw_id": st.column_config.NumberColumn(format="%d"),
                        "encounter_date": st.column_config.DateColumn(
                            "Date", format="MM/DD/YYYY"
                        ),
                    },
                    hide_index=True,
                )

        elif dataset == "Volumes":
            tabs = st.tabs(["Volumes", "Units of Service", "Hours", "Contracted Hours"])
            with tabs[0]:
                st.dataframe(data.volumes.volumes_df)
            with tabs[1]:
                st.dataframe(data.volumes.uos_df)
            with tabs[2]:
                st.dataframe(data.volumes.hours_df)
            with tabs[3]:
                st.dataframe(data.volumes.contracted_hours_df)

        elif dataset == "Financial":
            tabs = st.tabs(["Budget", "Income Statement"])
            with tabs[0]:
                st.dataframe(data.finance.budget_df)
            with tabs[1]:
                st.dataframe(data.finance.income_stmt_df)


def construct_prompt(global_prompt, dataset_prompt, query):
    if "plot" in query.lower() or "graph" in query.lower() or "chart" in query.lower():
        query = (
            "When creating visualizations, return a plotly object to display in Streamlit.\n\n"
            + query
        )
    else:
        query = "Do not try to plot anything or create visualizations.\n\n" + query

    query = f"{global_prompt}\n\n{dataset_prompt}\n\n{query}"

    return query