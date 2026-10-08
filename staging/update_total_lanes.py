import os
import re
import pandas as pd
import numpy as np

WORKSPACE_PATH = "/Users/erb/VAULT_PHONE/Research/CERCET/California/python/ag.workspace"
CLEAN_CSV = os.path.join(WORKSPACE_PATH, "staging/Master_CA_Project_List_Cleaned.csv")
CLEAN_XLSX = os.path.join(WORKSPACE_PATH, "staging/Master_CA_Project_List_Cleaned.xlsx")

def parse_num(val):
    if not val: return 0.0
    val_str = str(val).strip().replace(',', '')
    m = re.search(r"[-+]?\d*\.?\d+", val_str)
    return float(m.group(0)) if m else 0.0

def update_total_lanes():
    df = pd.read_csv(CLEAN_CSV, dtype=str).fillna("")
    print(f"Loaded {len(df)} rows from {CLEAN_CSV}")
    
    # Map project IDs to their rows
    grouped = df.groupby('Project ID')
    
    for pid, grp in grouped:
        indices = grp.index.tolist()
        if len(indices) == 2:
            # Pair found! Sum both directions directly
            idx1, idx2 = indices[0], indices[1]
            
            b1 = parse_num(df.at[idx1, 'dir_lanes_before'])
            b2 = parse_num(df.at[idx2, 'dir_lanes_before'])
            tot_b = b1 + b2
            
            a1 = parse_num(df.at[idx1, 'dir_lanes_after'])
            a2 = parse_num(df.at[idx2, 'dir_lanes_after'])
            tot_a = a1 + a2
            
            if tot_b > 0:
                df.at[idx1, 'total_lanes_before'] = str(int(tot_b))
                df.at[idx2, 'total_lanes_before'] = str(int(tot_b))
                
            if tot_a > 0:
                df.at[idx1, 'total_lanes_after'] = str(int(tot_a))
                df.at[idx2, 'total_lanes_after'] = str(int(tot_a))
        elif len(indices) == 1:
            # Single direction in sheet
            idx = indices[0]
            b = parse_num(df.at[idx, 'dir_lanes_before'])
            a = parse_num(df.at[idx, 'dir_lanes_after'])
            
            # Check if undivided highway (e.g. SR 49, SR 70, SR 1)
            desc = df.at[idx, 'Project Description'].lower()
            facility = df.at[idx, 'Facility Class'].lower()
            
            # If standard divided freeway, total = 2 * dir
            if b > 0:
                df.at[idx, 'total_lanes_before'] = str(int(b * 2))
            if a > 0:
                df.at[idx, 'total_lanes_after'] = str(int(a * 2))
                
    df.to_csv(CLEAN_CSV, index=False)
    print(f"Updated total_lanes in {CLEAN_CSV}")
    
    try:
        df.to_excel(CLEAN_XLSX, index=False, sheet_name="Master_Projects_Cleaned")
        print(f"Updated {CLEAN_XLSX}")
    except Exception as e:
        print(f"Excel export error: {e}")

if __name__ == '__main__':
    update_total_lanes()
