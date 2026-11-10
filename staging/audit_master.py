import pandas as pd
import numpy as np
import re
import datetime

CSV_FILE = "/Users/erb/VAULT_PHONE/Research/CERCET/California/python/ag.workspace/staging/master_sheet3_raw.csv"

def audit():
    df = pd.read_csv(CSV_FILE, dtype=str).fillna("")
    print(f"Total Rows: {len(df)}")
    print(f"Total Columns: {len(df.columns)}")
    
    # 1. Project Identifiers
    print("\n--- 1. PROJECT IDENTIFIERS ---")
    print(f"Unique Project IDs: {df['Project ID'].nunique()}")
    print(f"Empty Project IDs: {(df['Project ID'] == '').sum()}")
    
    # Check duplicate Project IDs
    id_counts = df[df['Project ID'] != '']['Project ID'].value_counts()
    dupe_ids = id_counts[id_counts > 1]
    print(f"Project IDs with multiple rows ({len(dupe_ids)} projects, total {dupe_ids.sum()} rows):")
    for pid, cnt in dupe_ids.items():
        sub = df[df['Project ID'] == pid]
        dirs = sub['Direction'].tolist()
        routes = sub['Route'].tolist()
        counties = sub['County'].tolist()
        print(f"  Project {pid}: {cnt} rows | Directions: {dirs} | Routes: {routes} | Counties: {counties}")
        
    # 2. Directions
    print("\n--- 2. DIRECTION AUDIT ---")
    print("Direction value counts:")
    print(df['Direction'].value_counts())
    multi_dir = df[df['Direction'].str.contains(',', na=False)]
    print(f"Multi-direction rows ({len(multi_dir)}):")
    for idx, r in multi_dir.iterrows():
        print(f"  Row {idx} | ID: {r['Project ID']} | Route: {r['Route']} | Dir: {r['Direction']} | Added Lanes: {r['Total Added Lanes (Lane Miles)']}")
        
    # 3. Postmiles (PM)
    print("\n--- 3. POSTMILE AUDIT ---")
    print("temp_pmtype counts:")
    print(df['temp_pmtype'].value_counts())
    blank_pmtype = df[(df['Project ID'] != '') & (df['temp_pmtype'] == '')]
    print(f"Projects with blank temp_pmtype ({len(blank_pmtype)}):")
    for idx, r in blank_pmtype.iterrows():
        print(f"  Row {idx} | ID: {r['Project ID']} | Rte: {r['Route']} | County: {r['County']} | Begin: {r['Begin PM']} | End: {r['End PM']}")
        
    # Check prefix in Begin PM / End PM
    ca_prefixes = df[df['Begin PM'].str.contains(r'^[A-Za-z]', na=False)]
    print(f"Begin PM with prefix ({len(ca_prefixes)}): e.g., {ca_prefixes['Begin PM'].head(5).tolist()}")
    
    # Check temp_abs_begin / end coverage
    has_temp_abs = (df['temp_abs_begin'] != '') & (df['temp_abs_end'] != '')
    print(f"Rows with temp_abs_begin/end: {has_temp_abs.sum()} / {len(df)}")
    
    # 4. Dates
    print("\n--- 4. DATES AUDIT ---")
    for col in ['Construction Begin Date', 'construction_begin_year', 'Open to Traffic Date', 'open_traffic_year']:
        vals = df[col][df[col] != '']
        print(f"[{col}] filled: {len(vals)}/{len(df)}")
        # Check numeric Excel serials
        serials = [v for v in vals if v.isdigit() and len(v) == 5]
        print(f"  -> Excel serial numbers count: {len(serials)} (sample: {serials[:3]})")
        # Check text dates
        text_dates = [v for v in vals if not v.isdigit() or len(v) != 4 and len(v) != 5]
        print(f"  -> Other text formats count: {len(text_dates)} (sample: {text_dates[:5]})")
        
    # 5. Lane Counts Before & After
    print("\n--- 5. LANE COUNTS AUDIT ---")
    for col in ['Number of Lanes Before Expansion', 'total_lanes_before', 'dir_lanes_before', 'total_lanes_after', 'dir_lanes_after']:
        filled = (df[col] != '').sum()
        print(f"[{col}] filled: {filled}/{len(df)}")
        if filled > 0:
            print(f"  Sample values: {df[col][df[col] != ''].head(5).tolist()}")
            
    # 6. Sensors
    print("\n--- 6. SENSORS AUDIT ---")
    for col in ['Number of Active Non-Ramp Sensors', 'Sensor Station 1 ID ', 'Sensor Station 2 ID ']:
        filled = (df[col] != '').sum()
        print(f"[{col}] filled: {filled}/{len(df)}")
    
    s1_vals = df['Sensor Station 1 ID '][df['Sensor Station 1 ID '] != ''].tolist()
    text_s1 = [v for v in s1_vals if not v.isdigit()]
    print(f"Sensor 1 values containing non-digits: {len(text_s1)}/{len(s1_vals)}")
    print(f"Sample text Sensor 1 IDs: {text_s1[:5]}")
    
    # 7. Competing columns
    print("\n--- 7. COMPETING COLUMNS ---")
    print(f"expansion_shown_google_earth filled: {(df['expansion_shown_google_earth'] != '').sum()}")
    print(f"Lane expansion shown in Google Earth? filled: {(df['Lane expansion shown in Google Earth?'] != '').sum()}")
    print(f"project_type filled: {(df['project_type'] != '').sum()}")
    print(f"Project Type (...) filled: {(df['Project Type (\"Capacity Expansion\" or \"New Roadway\")'] != '').sum()}")
    print(f"Facility Class checked by Lu filled: {(df['Facility Class checked by Lu'] != '').sum()}")
    print(f"Facility Class filled: {(df['Facility Class'] != '').sum()}")
    print(f"area_type filled: {(df['area_type'] != '').sum()}")

if __name__ == '__main__':
    audit()
