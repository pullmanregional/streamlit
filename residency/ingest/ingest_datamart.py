import sys
import os
import shutil
import logging
import pandas as pd
import numpy as np
from dataclasses import dataclass
from sqlmodel import Session

# Add project root and repo roots so we can import common modules and from ../src/
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from src.model import db
from prw_common import db_utils
from prw_common import cli_utils
from prw_common.encrypt import encrypt_file
from prw_common.remote_utils import upload_file_to_s3


# -------------------------------------------------------
# Types
# -------------------------------------------------------
@dataclass
class SrcData:
    patients: pd.DataFrame
    encounters: pd.DataFrame
    notes_inpt: pd.DataFrame
    notes_ed: pd.DataFrame


@dataclass
class OutData:
    patients: pd.DataFrame
    encounters: pd.DataFrame
    notes_inpt: pd.DataFrame
    notes_ed: pd.DataFrame


# Residents by year
RESIDENTS_BY_YEAR = {
    "R3": [
        "OLAWUYI, DAMOLA BOLUTIFE",
        "WARD, JEFFREY LOREN",
        "YOUNES, MOHAMMED",
    ],
    "R2": [
        "MADER, KELSEY",
        "PERIN, KARLY",
        "SHAKIR, TUARUM N",
    ],
    "R1": [
        "KIRUI, RISPER",
        "PENDERGRAFT, COREY MARK",
        "SWALLOW, NATHAN",
    ],
}
ALL_RESIDENTS = (
    sorted(RESIDENTS_BY_YEAR["R3"])
    + sorted(RESIDENTS_BY_YEAR["R2"])
    + sorted(RESIDENTS_BY_YEAR["R1"])
)


# -------------------------------------------------------
# Extract
# -------------------------------------------------------
def read_source_tables(prw_engine) -> SrcData:
    """
    Read source tables from the warehouse DB
    """
    logging.info("Reading source tables")

    patients = pd.read_sql_table("prw_patients", prw_engine)
    encounters = pd.read_sql_table("prw_encounters_outpt", prw_engine)
    notes_inpt = pd.read_sql_table("prw_notes_inpt", prw_engine)
    notes_ed = pd.read_sql_table("prw_notes_ed", prw_engine)

    # Convert columns to datetime
    encounters["encounter_date"] = pd.to_datetime(encounters["encounter_date"])
    notes_inpt["service_date"] = pd.to_datetime(notes_inpt["service_date"])
    notes_ed["service_date"] = pd.to_datetime(notes_ed["service_date"])

    return SrcData(
        patients=patients,
        encounters=encounters,
        notes_inpt=notes_inpt,
        notes_ed=notes_ed,
    )


# -------------------------------------------------------
# Transform
# -------------------------------------------------------
def transform(src: SrcData) -> OutData:
    """
    Transform source data into datamart tables
    """
    # Filter to only include completed encounters and relevant notes for residents
    encounters = src.encounters[src.encounters["service_provider"].isin(ALL_RESIDENTS)]
    encounters = encounters[encounters["appt_status"] == "Completed"]

    # Rename columns to match datamart table names
    notes_columns = {
        "author_name": "signing_author",
        "first_author_name": "initial_author",
        "cosign_author_name": "cosign_author",
    }
    notes_inpt = src.notes_inpt.rename(columns=notes_columns)
    notes_ed = src.notes_ed.rename(columns=notes_columns)

    # Filter notes to just residents
    notes_inpt = filter_resident_notes(notes_inpt, ALL_RESIDENTS)
    notes_ed = filter_resident_notes(notes_ed, ALL_RESIDENTS)

    # Assign a resident to each note
    for resident in ALL_RESIDENTS:
        notes_inpt.loc[
            is_residents_note(notes_inpt, resident),
            "resident",
        ] = resident
        notes_ed.loc[is_residents_note(notes_ed, resident), "resident"] = resident

    # Set "academic_year" column to the year between July <year> to July <year+1>.
    # For example, both 7/1/2024 and 6/30/2025 should be "2024"
    academic_year = lambda date: np.where(
        date.dt.month >= 7,
        date.dt.year,
        (date.dt.year - 1),
    )
    encounters["academic_year"] = academic_year(encounters["encounter_date"])
    notes_inpt["academic_year"] = academic_year(notes_inpt["service_date"])
    notes_ed["academic_year"] = academic_year(notes_ed["service_date"])

    # If the encounter diagnosis or LOS codes contain one of the ob codes, mark the Ob column
    ob_codes = ["Z32.01", "Z33", "Z34", "Z3A", "O09"]
    ob_los = ["0500F", "0501F", "0502F", "0503F", "09888"]
    encounters["ob"] = encounters["diagnoses_icd"].str.contains("|".join(ob_codes)) | (
        encounters["level_of_service"].isin(ob_los)
    )

    # Mark peds and geriatrics encounters
    encounters["peds"] = encounters["encounter_age"] < 18
    encounters["geriatrics"] = encounters["encounter_age"] > 65
    notes_inpt["peds"] = notes_inpt["encounter_age"] < 18
    notes_ed["peds"] = notes_ed["encounter_age"] < 18

    # Sort by encounter date
    encounters = encounters.sort_values(by="encounter_date")
    notes_inpt = notes_inpt.sort_values(by="service_date")
    notes_ed = notes_ed.sort_values(by="service_date")

    # Mark ED notes
    notes_ed["ed"] = True
    notes_inpt["ed"] = False

    return OutData(
        patients=src.patients,
        encounters=encounters,
        notes_inpt=notes_inpt,
        notes_ed=notes_ed,
    )


def filter_resident_notes(notes, residents):
    """
    Find all relevant provider notes where the resident is the author or initial author
    """
    ret = notes[is_residents_note(notes, residents)]

    # Remove birthplace notes, which don't count towards Inpatient
    ret = ret[~ret["dept"].isin(["CC WPL NURSERY", "CC WPL LABOR AND DELIVERY"])]

    # Limit to: ED, H&P, progress notes, discharge summaries, consults, procedure notes, delivery, SNF, Significant Event
    ret = ret[
        ret["note_type"].isin(
            [
                "ED Provider Notes",
                "ED Notes",
                "ED Observation Notes",
                "H&P",
                "Interval H&P Note",
                "Progress Notes",
                "Assessment & Plan Note",
                "Hospital Course",
                "Interim Summary - Physician",
                "Discharge Summary",
                "Consults",
                "Procedures",
                "L&D Delivery Note",
                "SNF Transfer",
                "Significant Event",
            ]
        )
    ]

    return ret


def is_residents_note(note, residents):
    if not isinstance(residents, list):
        residents = [residents]
    return (note["signing_author"].isin(residents)) | (
        note["initial_author"].isin(residents)
    )


def calc_stats(out: OutData, residents: list[str]):
    """
    Calculate stats, including ACGME stats, for each resident
    """
    stats = {}
    for resident in residents:
        stats[resident] = calc_acgme_for_resident(
            resident, out.encounters, out.notes_inpt, out.notes_ed, out.patients
        )
    stats["Overall"] = calc_acgme_for_resident(
        "", out.encounters, out.notes_inpt, out.notes_ed, out.patients
    )
    return stats


def calc_acgme_for_resident(resident, encounters, notes_inpt, notes_ed, patients):
    """
    Calculate ACGME stats for a single resident
    """
    stats = {}

    # Filter to resident specified
    if resident == "":
        resident_encounters = encounters
        resident_notes_inpt = notes_inpt
        resident_notes_ed = notes_ed
    else:
        resident_encounters = encounters[encounters["service_provider"] == resident]
        resident_notes_inpt = notes_inpt[is_residents_note(notes_inpt, resident)]
        resident_notes_ed = notes_ed[is_residents_note(notes_ed, resident)]

    # Filter notes to academic years where this resident saw patients in clinic to exclude med student notes
    years = sorted(resident_encounters["academic_year"].unique(), reverse=True)
    all_encounters = encounters[encounters["academic_year"].isin(years)]
    resident_notes_inpt = resident_notes_inpt[
        resident_notes_inpt["academic_year"].isin(years)
    ]
    resident_notes_ed = resident_notes_ed[
        resident_notes_ed["academic_year"].isin(years)
    ]

    # Iterate over each academic year
    for year in years:
        year = int(year)
        year_encounters = encounters[encounters["academic_year"] == year]
        resident_year_encounters = resident_encounters[
            resident_encounters["academic_year"] == year
        ]
        resident_year_notes_inpt = resident_notes_inpt[
            resident_notes_inpt["academic_year"] == year
        ]
        resident_year_notes_ed = resident_notes_ed[
            resident_notes_ed["academic_year"] == year
        ]
        stats[year] = calc_acgme_for_resident_year(
            year_encounters,
            resident_year_encounters,
            resident_year_notes_inpt,
            resident_year_notes_ed,
        )
        stats[year]["year"] = year

    # Add a total row
    stats["Total"] = calc_acgme_for_resident_year(
        all_encounters, resident_encounters, resident_notes_inpt, resident_notes_ed
    )
    stats["Total"]["year"] = "Total"

    # Calculate stats that are only in totals, not per year
    if resident == "":
        panel_df = patients[patients["pcp"].isin(ALL_RESIDENTS)]
    else:
        panel_df = patients[patients["pcp"] == resident]

    panel_peds = panel_df[panel_df["age"] < 18]
    panel_geri = panel_df[panel_df["age"] > 65]
    totals = stats["Total"]
    totals["num_paneled_patients"] = len(panel_df)
    totals["num_paneled_peds"] = len(panel_peds)
    totals["num_paneled_peds_percent"] = (
        f"{len(panel_peds) / len(panel_df) if len(panel_df) > 0 else 0:.0%}"
    )
    totals["num_paneled_peds_comment"] = (
        f"{len(panel_peds)}/{len(panel_df) if len(panel_df) > 0 else 0} pts"
    )
    totals["num_paneled_geri"] = len(panel_geri)
    totals["num_paneled_geri_percent"] = (
        f"{len(panel_geri) / len(panel_df) if len(panel_df) > 0 else 0:.0%}"
    )
    totals["num_paneled_geri_comment"] = (
        f"{len(panel_geri)}/{len(panel_df) if len(panel_df) > 0 else 0} pts"
    )

    return stats


def calc_acgme_for_resident_year(
    encounters_in_year_df,
    resident_encounters_in_year_df,
    resident_notes_inpt_year_df,
    resident_notes_ed_year_df,
):
    """
    Calculate ACGME stats for a single resident in a single year
    """
    total_visits = len(resident_encounters_in_year_df)

    prov_continuity_visits = len(
        resident_encounters_in_year_df[resident_encounters_in_year_df["with_pcp"]]
    )
    prov_continuity_percent = (
        f"{prov_continuity_visits / total_visits if total_visits > 0 else 0:.0%}"
    )
    prov_continuity_comment = f"{prov_continuity_visits}/{total_visits} visits"

    peds_visits = len(
        resident_encounters_in_year_df[resident_encounters_in_year_df["peds"]]
    )
    peds_percent = f"{peds_visits / total_visits if total_visits > 0 else 0:.0%}"
    peds_comment = f"{peds_visits}/{total_visits} visits"

    geriatrics_visits = len(
        resident_encounters_in_year_df[resident_encounters_in_year_df["geriatrics"]]
    )
    geriatrics_percent = (
        f"{geriatrics_visits / total_visits if total_visits > 0 else 0:.0%}"
    )
    geriatrics_comment = f"{geriatrics_visits}/{total_visits} visits"

    ob_visits = len(
        resident_encounters_in_year_df[resident_encounters_in_year_df["ob"]]
    )
    ob_percent = f"{ob_visits / total_visits if total_visits > 0 else 0:.0%}"
    ob_comment = f"{ob_visits}/{total_visits} visits"

    # Filter telehealth visits
    th_visits = len(
        resident_encounters_in_year_df[
            resident_encounters_in_year_df["encounter_type"] == "CVV VIRTUAL VISIT"
        ]
    )
    th_percent = f"{th_visits / total_visits if total_visits > 0 else 0:.0%}"
    th_comment = f"{th_visits}/{total_visits} visits"

    # For patient sided continuity, we'll look at all the visits that the provider where With PCP is set.
    # Then take all the unique MRNs for those visits, and find all the visits for those MRNs in the same year.
    # Finally, calculate the number of visits with the provider and With PCP set divided by total visits calculated.
    with_pcp_visits = resident_encounters_in_year_df[
        resident_encounters_in_year_df["with_pcp"]
    ]
    with_pcp_mrns = with_pcp_visits["prw_id"].unique()
    pt_continuity_visits = len(
        encounters_in_year_df[encounters_in_year_df["prw_id"].isin(with_pcp_mrns)]
    )
    pt_continuity_percent = f"{len(with_pcp_visits) / pt_continuity_visits if pt_continuity_visits > 0 else 0:.0%}"
    pt_continuity_comment = f"{len(with_pcp_visits)}/{pt_continuity_visits} visits"

    # Calculate number of adult/peds ED and inpatient encounters. An encounter is defined as any
    # interaction where a note was left, even if multiple notes were left for the same patient on the same day,
    # e.g. a lac repair note + ED note would be 2 encounters.
    num_ed_adult_encounters = len(
        resident_notes_ed_year_df[~resident_notes_ed_year_df["peds"]]
    )
    num_ed_peds_encounters = len(
        resident_notes_ed_year_df[resident_notes_ed_year_df["peds"]]
    )
    num_inpt_adult_encounters = len(
        resident_notes_inpt_year_df[~resident_notes_inpt_year_df["peds"]]
    )
    num_inpt_peds_encounters = len(
        resident_notes_inpt_year_df[resident_notes_inpt_year_df["peds"]]
    )

    stats = {
        "total_visits": total_visits,
        "pt_continuity_visits": pt_continuity_visits,
        "pt_continuity_percent": pt_continuity_percent,
        "pt_continuity_comment": pt_continuity_comment,
        "prov_continuity_visits": prov_continuity_visits,
        "prov_continuity_percent": prov_continuity_percent,
        "prov_continuity_comment": prov_continuity_comment,
        "peds_visits": peds_visits,
        "peds_percent": peds_percent,
        "peds_comment": peds_comment,
        "geriatrics_visits": geriatrics_visits,
        "geriatrics_percent": geriatrics_percent,
        "geriatrics_comment": geriatrics_comment,
        "ob_visits": ob_visits,
        "ob_percent": ob_percent,
        "ob_comment": ob_comment,
        "th_visits": th_visits,
        "th_percent": th_percent,
        "th_comment": th_comment,
        "ed_adult_encounters": num_ed_adult_encounters,
        "ed_peds_encounters": num_ed_peds_encounters,
        "inpt_adult_encounters": num_inpt_adult_encounters,
        "inpt_peds_encounters": num_inpt_peds_encounters,
    }
    return stats


# -------------------------------------------------------
# Main entry point
# -------------------------------------------------------
def parse_arguments():
    parser = cli_utils.cli_parser(
        description="Ingest data from PRW warehouse to datamart.",
        require_prw=True,
        require_out=True,
    )
    cli_utils.add_s3_args(parser)
    parser.add_argument(
        "--key",
        help="Encrypt with given key. Must be specified to upload to S3. Defaults to no encryption if not specified.",
    )
    return parser.parse_args()


def error_exit(msg):
    logging.error(msg)
    exit(1)


def main():
    args = parse_arguments()
    prw_db_url = args.prw
    output_db_file = args.out
    encrypt_key = None if args.key is None or args.key.lower() == "none" else args.key
    s3_url = args.s3url
    s3_auth = args.s3auth
    tmp_db_file = "datamart.sqlite3"

    logging.info(
        f"Input: {db_utils.mask_conn_pw(prw_db_url)}, output: {output_db_file}, encrypt: {encrypt_key is not None}, upload: {s3_url}",
        flush=True,
    )

    # Create the sqlite output database and create the tables as defined in ../src/model/db.py
    out_engine = db_utils.get_db_connection(f"sqlite:///{tmp_db_file}")
    db.DatamartModel.metadata.create_all(out_engine)

    # Read from PRW warehouse (MSSQL in prod, sqlite in dev)
    prw_engine = db_utils.get_db_connection(prw_db_url)
    src = read_source_tables(prw_engine)
    if src is None:
        error_exit("ERROR: failed to read source data (see above)")

    # Transform data
    out = transform(src)

    # Calculate key/value data
    kv_data = {
        "residents": RESIDENTS_BY_YEAR,
        "stats": calc_stats(out, ALL_RESIDENTS),
    }

    # Write tables to datamart
    session = Session(out_engine)
    db_utils.clear_tables_and_insert_data(
        session,
        [
            db_utils.TableData(table=db.Encounters, df=out.encounters),
            db_utils.TableData(
                table=db.Notes, df=pd.concat([out.notes_inpt, out.notes_ed])
            ),
        ],
    )
    db_utils.write_kv_table(kv_data, session, db.KvTable)

    # Update last ingest time and modified times for source data files
    db_utils.write_meta(session, db.Meta)
    session.commit()

    # Finally encrypt output files, or just copy if no encryption key is provided
    if encrypt_key and encrypt_key.lower() != "none":
        encrypt_file(tmp_db_file, output_db_file, encrypt_key)
    else:
        shutil.copy(tmp_db_file, output_db_file)

    # Cleanup
    os.remove(tmp_db_file)
    prw_engine.dispose()
    out_engine.dispose()

    # Upload to S3. Only upload encrypted content.
    if encrypt_key and s3_url and s3_auth:
        upload_file_to_s3(s3_url, s3_auth, output_db_file)

    logging.info("Done")


if __name__ == "__main__":
    main()
