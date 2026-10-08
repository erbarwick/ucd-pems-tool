import sys
import os
import pandas as pd
import numpy as np
import datetime
import re

# Insert workspace path
WORKSPACE_PATH = "/Users/erb/VAULT_PHONE/Research/CERCET/California/python/ag.workspace"
sys.path.insert(0, WORKSPACE_PATH)

RAW_CSV = os.path.join(WORKSPACE_PATH, "staging/Master_CA_Project_List_Cleaned.csv")
REF_PATH = os.path.join(WORKSPACE_PATH, "downloads/merged_geo_pm.csv")
REPORT_PATH = os.path.join(WORKSPACE_PATH, "staging/discrepancies_report.md")

def clean_dir(d):
    d = str(d).strip().upper()
    if d.startswith('E'): return 'E'
    if d.startswith('W'): return 'W'
    if d.startswith('N'): return 'N'
    if d.startswith('S'): return 'S'
    return d

def excel_serial_to_date(val):
    if not val:
        return ""
    val_str = str(val).strip()
    if val_str.isdigit() and len(val_str) == 5:
        days = int(val_str)
        # Excel 1900 date system
        d = datetime.date(1899, 12, 30) + datetime.timedelta(days=days)
        return d.strftime("%Y-%m-%d")
    return val_str

def parse_num(val):
    if not val: return 0.0
    val_str = str(val).strip().replace(',', '')
    # extract first float
    m = re.search(r"[-+]?\d*\.?\d+", val_str)
    if m:
        try:
            return float(m.group(0))
        except:
            return 0.0
    return 0.0

def run_verification():
    import main
    
    df = pd.read_csv(RAW_CSV, dtype=str).fillna("")
    print(f"Loaded {len(df)} rows from {RAW_CSV}")
    
    # Load reference table once
    ref_df = main._load_merged_geo_pm(REF_PATH)
    print(f"Loaded {len(ref_df)} reference rows from {REF_PATH}")
    
    # Results collectors
    discrepancies = []
    
    for idx, r in df.iterrows():
        ea = r.get('EA', '').strip()
        pid = r.get('Project ID', '').strip()
        dist = r.get('Dist', '').strip()
        county = r.get('County', '').strip()
        route = r.get('Route', '').strip()
        dir_val = r.get('Direction', '').strip()
        begin_pm = r.get('Begin PM', '').strip()
        end_pm = r.get('End PM', '').strip()
        pm_type = r.get('temp_pmtype', '').strip()
        temp_abs_begin = r.get('temp_abs_begin', '').strip()
        temp_abs_end = r.get('temp_abs_end', '').strip()
        start_coord = r.get('Start Coordinates', '').strip()
        end_coord = r.get('End Coordinates', '').strip()
        
        # 1. Direction issues
        dir_issue = None
        if not dir_val:
            dir_issue = "Missing direction"
        elif ',' in dir_val:
            dir_issue = f"Multi-direction: '{dir_val}'"
        elif dir_val not in ['NB', 'SB', 'EB', 'WB', 'N', 'S', 'E', 'W']:
            dir_issue = f"Invalid direction code: '{dir_val}'"
            
        # 2. Postmile matching
        d_1char = clean_dir(dir_val)
        calc_abs_begin, calc_coord_begin, begin_pm_err = None, None, None
        calc_abs_end, calc_coord_end, end_pm_err = None, None, None
        
        if route and d_1char and begin_pm:
            # Check route in ref_df
            r_sub = ref_df[ref_df['Route'].astype(str).str.strip() == str(route).strip()]
            if r_sub.empty:
                begin_pm_err = f"Route {route} not found in PeMS reference"
            else:
                rd_sub = r_sub[r_sub['Direction'].astype(str).str.strip().str.upper() == d_1char]
                if rd_sub.empty:
                    begin_pm_err = f"Route {route} Dir {d_1char} not found in PeMS reference"
                else:
                    # Lookup begin
                    try:
                        # CA mode
                        prefix, num = main._split_ca_pm(begin_pm)
                        if pd.isna(num):
                            begin_pm_err = f"Cannot parse Begin PM: {begin_pm}"
                        else:
                            # match county
                            c_sub = rd_sub[rd_sub['County'].str.lower() == county.lower()]
                            if c_sub.empty:
                                begin_pm_err = f"County '{county}' not found on Route {route} {d_1char}"
                            else:
                                pm_series = c_sub['CA PM'].apply(lambda x: main._split_ca_pm(x)[1])
                                delta = (pm_series - num).abs()
                                min_delta = delta.min()
                                if min_delta <= 1.0: # within 1 mile
                                    best_row = c_sub.loc[delta.idxmin()]
                                    calc_abs_begin = best_row['Abs PM']
                                    lat = float(best_row['Latitude'])
                                    lon = float(best_row['Longitude'])
                                    calc_coord_begin = f"{lat:.5f}, {lon:.5f}"
                                else:
                                    begin_pm_err = f"Begin PM {begin_pm} not within range in {county} (min delta {min_delta:.2f})"
                    except Exception as e:
                        begin_pm_err = str(e)
                        
                    # Lookup end
                    if end_pm:
                        try:
                            prefix, num = main._split_ca_pm(end_pm)
                            if not pd.isna(num):
                                c_sub = rd_sub[rd_sub['County'].str.lower() == county.lower()]
                                if not c_sub.empty:
                                    pm_series = c_sub['CA PM'].apply(lambda x: main._split_ca_pm(x)[1])
                                    delta = (pm_series - num).abs()
                                    min_delta = delta.min()
                                    if min_delta <= 1.0:
                                        best_row = c_sub.loc[delta.idxmin()]
                                        calc_abs_end = best_row['Abs PM']
                                        lat_e = float(best_row['Latitude'])
                                        lon_e = float(best_row['Longitude'])
                                        calc_coord_end = f"{lat_e:.5f}, {lon_e:.5f}"
                                    else:
                                        end_pm_err = f"End PM {end_pm} not within range in {county} (min delta {min_delta:.2f})"
                        except Exception as e:
                            end_pm_err = str(e)
                            
        # Compare temp_abs with calculated
        abs_begin_diff = None
        if calc_abs_begin is not None and temp_abs_begin:
            try:
                abs_begin_diff = round(abs(float(calc_abs_begin) - float(temp_abs_begin)), 2)
            except:
                pass
                
        abs_end_diff = None
        if calc_abs_end is not None and temp_abs_end:
            try:
                abs_end_diff = round(abs(float(calc_abs_end) - float(temp_abs_end)), 2)
            except:
                pass

        # 3. Dates
        c_begin_date = r.get('Construction Begin Date', '').strip()
        c_begin_parsed = excel_serial_to_date(c_begin_date)
        c_begin_yr = r.get('construction_begin_year', '').strip()
        c_begin_mo = r.get('construction_begin_month', '').strip()
        c_begin_dy = r.get('construction_begin_day', '').strip()
        
        o_traffic_date = r.get('Open to Traffic Date', '').strip()
        o_traffic_parsed = excel_serial_to_date(o_traffic_date)
        o_traffic_yr = r.get('open_traffic_year', '').strip()
        o_traffic_mo = r.get('open_traffic_month', '').strip()
        o_traffic_dy = r.get('open_traffic_day', '').strip()
        
        date_issue = []
        if c_begin_date and c_begin_date != c_begin_parsed:
            date_issue.append(f"Construction Date was Excel serial: {c_begin_date} -> {c_begin_parsed}")
        if o_traffic_date and o_traffic_date != o_traffic_parsed:
            date_issue.append(f"Open Date was Excel serial: {o_traffic_date} -> {o_traffic_parsed}")
            
        # 4. Lanes Before and After
        gp_before = parse_num(r.get('Number of GP Lanes Before Expansion', '0'))
        hov_before = parse_num(r.get('Number of HOV Lanes Before Expansion', '0'))
        hot_before = parse_num(r.get('Number of HOT Lanes Before Expansion', '0'))
        other_before = parse_num(r.get('Number of other lane before (specific)', '0'))
        calc_dir_lanes_before = gp_before + hov_before + hot_before + other_before
        
        gp_after = parse_num(r.get('Number of GP Lanes After Expansion', '0'))
        hov_after = parse_num(r.get('Number of HOV Lanes After Expansion', '0'))
        hot_after = parse_num(r.get('Number of HOT Lanes After Expansion', '0'))
        other_after = parse_num(r.get('Number of other lane after (specific)', '0'))
        calc_dir_lanes_after = gp_after + hov_after + hot_after + other_after
        
        # 5. Sensors
        s1 = r.get('Sensor Station 1 ID ', '').strip()
        s1_issue = None
        if s1 and not s1.isdigit():
            s1_issue = f"Sensor 1 contains non-digit: '{s1}'"
            
        # Compile record
        discrepancies.append({
            'row': idx,
            'ea': ea,
            'project_id': pid,
            'county': county,
            'route': route,
            'dir': dir_val,
            'dir_issue': dir_issue,
            'begin_pm': begin_pm,
            'end_pm': end_pm,
            'temp_pmtype': pm_type,
            'temp_abs_begin': temp_abs_begin,
            'calc_abs_begin': calc_abs_begin,
            'abs_begin_diff': abs_begin_diff,
            'begin_pm_err': begin_pm_err,
            'temp_abs_end': temp_abs_end,
            'calc_abs_end': calc_abs_end,
            'abs_end_diff': abs_end_diff,
            'end_pm_err': end_pm_err,
            'calc_coord_begin': calc_coord_begin,
            'calc_coord_end': calc_coord_end,
            'c_begin_date': c_begin_date,
            'c_begin_parsed': c_begin_parsed,
            'c_begin_yr': c_begin_yr,
            'o_traffic_date': o_traffic_date,
            'o_traffic_parsed': o_traffic_parsed,
            'o_traffic_yr': o_traffic_yr,
            'date_issues': "; ".join(date_issue),
            'calc_dir_lanes_before': calc_dir_lanes_before,
            'calc_dir_lanes_after': calc_dir_lanes_after,
            's1': s1,
            's1_issue': s1_issue,
            'area_type': r.get('area_type', '').strip(),
            'notes': r.get('Notes', '').strip()
        })
        
    res_df = pd.DataFrame(discrepancies)
    out_csv = os.path.join(WORKSPACE_PATH, "staging/audit_verification_results.csv")
    res_df.to_csv(out_csv, index=False)
    print(f"Saved full audit results to {out_csv}")
    
    # Summary of findings
    print("\n================ SUMMARY OF AUDIT FINDINGS ================")
    print(f"Total projects evaluated: {len(res_df)}")
    dir_problems = res_df[res_df['dir_issue'].notna() & (res_df['dir_issue'] != "")]
    print(f"Direction problems: {len(dir_problems)}")
    for _, r in dir_problems.iterrows():
        print(f"  Row {r['row']} | PID {r['project_id']} | Issue: {r['dir_issue']}")
        
    pm_begin_errs = res_df[res_df['begin_pm_err'].notna() & (res_df['begin_pm_err'] != "")]
    print(f"\nBegin PM lookup errors/out-of-range: {len(pm_begin_errs)}")
    for _, r in pm_begin_errs.iterrows():
        print(f"  Row {r['row']} | PID {r['project_id']} | Route {r['route']} {r['dir']} {r['county']} | PM: {r['begin_pm']} | Err: {r['begin_pm_err']}")
        
    pm_diffs = res_df[(res_df['abs_begin_diff'] > 1.0) | (res_df['abs_end_diff'] > 1.0)]
    print(f"\nLarge delta between temp_abs and calculated Abs PM (>1.0 mile): {len(pm_diffs)}")
    for _, r in pm_diffs.iterrows():
        print(f"  Row {r['row']} | PID {r['project_id']} | Route {r['route']} | Begin temp={r['temp_abs_begin']} vs calc={r['calc_abs_begin']} (diff {r['abs_begin_diff']}) | End temp={r['temp_abs_end']} vs calc={r['calc_abs_end']} (diff {r['abs_end_diff']})")

    s1_issues = res_df[res_df['s1_issue'].notna() & (res_df['s1_issue'] != "")]
    print(f"\nSensor 1 formatting issues: {len(s1_issues)}")
    for _, r in s1_issues.head(10).iterrows():
        print(f"  Row {r['row']} | PID {r['project_id']} | {r['s1_issue']}")
        
    missing_area = res_df[res_df['area_type'] == ""]
    print(f"\nMissing Area Type: {len(missing_area)} projects")
    print(f"Sample PIDs missing Area Type: {missing_area['project_id'].tolist()[:10]}")

if __name__ == '__main__':
    run_verification()
