import os
import re
import pandas as pd
import numpy as np

WORKSPACE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN_CSV = os.path.join(WORKSPACE_PATH, "staging/Master_CA_Project_List_Cleaned.csv")
RAW_CSV = os.path.join(WORKSPACE_PATH, "staging/master_sheet3_raw.csv")

def parse_num(val):
    if not val: return 0.0
    val_str = str(val).strip().replace(',', '')
    m = re.search(r"[-+]?\d*\.?\d+", val_str)
    return float(m.group(0)) if m else 0.0

def parse_other_lane(val):
    if not val:
        return 0.0, 0.0, False
    s = str(val).strip().lower()
    if not s or s == "nan" or s == "0" or s == "0.0":
        return 0.0, 0.0, False
    if any(k in s for k in ['shared median', 'two-way left', 'twltl', 'center turn']):
        m = re.search(r"[-+]?\d*\.?\d+", s)
        n = float(m.group(0)) if m else 1.0
        return 0.0, n, False
    if 'ramp' in s or 'connector' in s:
        return 0.0, 0.0, True
    m = re.search(r"[-+]?\d*\.?\d+", s)
    n = float(m.group(0)) if m else 0.0
    return n, 0.0, False

def validate_lanes():
    df = pd.read_csv(CLEAN_CSV, dtype=str).fillna("")
    print(f"Loaded {len(df)} rows from {CLEAN_CSV}")
    
    audit_records = []
    
    for idx, r in df.iterrows():
        pid = r['Project ID'].strip()
        rte = r['Route'].strip()
        direction = r['Direction'].strip()
        co = r['County'].strip()
        
        # Sub-category before lanes
        gp_b = parse_num(r.get('Number of GP Lanes Before Expansion', '0'))
        hov_b = parse_num(r.get('Number of HOV Lanes Before Expansion', '0'))
        hot_b = parse_num(r.get('Number of HOT Lanes Before Expansion', '0'))
        oth_b = r.get('Number of other lane before (specific)', '0')
        oth_b_dir, oth_b_shared, _ = parse_other_lane(oth_b)
        sum_b = gp_b + hov_b + hot_b + oth_b_dir
        
        # Sub-category after lanes
        gp_a = parse_num(r.get('Number of GP Lanes After Expansion', '0'))
        hov_a = parse_num(r.get('Number of HOV Lanes After Expansion', '0'))
        hot_a = parse_num(r.get('Number of HOT Lanes After Expansion', '0'))
        oth_a = r.get('Number of other lane after (specific)', '0')
        oth_a_dir, oth_a_shared, _ = parse_other_lane(oth_a)
        sum_a = gp_a + hov_a + hot_a + oth_a_dir
        
        # Filled directional columns
        dir_b_val = parse_num(r.get('dir_lanes_before', '0'))
        dir_a_val = parse_num(r.get('dir_lanes_after', '0'))
        tot_b_val = parse_num(r.get('total_lanes_before', '0'))
        tot_a_val = parse_num(r.get('total_lanes_after', '0'))
        
        # Sensor station lanes
        s1_b_str = r.get('Sensor Station 1: number of lanes before ', '').strip()
        s1_a_str = r.get('Sensor Station 1: number of lanes after', '').strip()
        s1_b = parse_num(s1_b_str)
        s1_a = parse_num(s1_a_str)
        
        # Total added lane miles
        added_lm = parse_num(r.get('Total Added Lanes (Lane Miles)', '0'))
        gp_added_lm = parse_num(r.get('New Gen Pur-pose Lanes (Lane Miles)', '0'))
        hov_added_lm = parse_num(r.get('New HOV Lanes (Lane Miles)', '0'))
        
        # Discrepancy checks
        issues = []
        
        # Check 1: Does sum_b match dir_lanes_before?
        if sum_b > 0 and sum_b != dir_b_val:
            issues.append(f"Sub-columns sum before ({sum_b}) != dir_lanes_before ({dir_b_val})")
            
        # Check 2: Does sum_a match dir_lanes_after?
        if sum_a > 0 and sum_a != dir_a_val:
            issues.append(f"Sub-columns sum after ({sum_a}) != dir_lanes_after ({dir_a_val})")
            
        # Check 3: Sensor 1 comparison before
        # Note: sometimes sensor 1 is "5,0" (5 ML, 0 HOV = 5) or "5(5)"
        if s1_b > 0:
            if ',' in s1_b_str:
                s_sum = sum([parse_num(x) for x in s1_b_str.split(',')])
                if s_sum != dir_b_val:
                    issues.append(f"Sensor 1 compound lanes ({s1_b_str} -> {s_sum}) != dir_lanes_before ({dir_b_val})")
            elif '(' in s1_b_str:
                # e.g. "4(4)" -> 4
                s_main = parse_num(s1_b_str.split('(')[0])
                if s_main != dir_b_val:
                    issues.append(f"Sensor 1 lanes ({s1_b_str}) != dir_lanes_before ({dir_b_val})")
            elif s1_b != dir_b_val:
                issues.append(f"Sensor 1 lanes before ({s1_b}) != dir_lanes_before ({dir_b_val})")
                
        # Check 4: Sensor 1 comparison after
        if s1_a > 0:
            if ',' in s1_a_str:
                s_sum_a = sum([parse_num(x) for x in s1_a_str.split(',')])
                if s_sum_a != dir_a_val:
                    issues.append(f"Sensor 1 compound lanes after ({s1_a_str} -> {s_sum_a}) != dir_lanes_after ({dir_a_val})")
            elif '(' in s1_a_str:
                s_main_a = parse_num(s1_a_str.split('(')[0])
                if s_main_a != dir_a_val:
                    issues.append(f"Sensor 1 lanes after ({s1_a_str}) != dir_lanes_after ({dir_a_val})")
            elif s1_a != dir_a_val:
                issues.append(f"Sensor 1 lanes after ({s1_a}) != dir_lanes_after ({dir_a_val})")

        # Check 5: Did after increase if added lane miles > 0?
        if added_lm > 0 and dir_a_val > 0 and dir_b_val > 0:
            if dir_a_val <= dir_b_val:
                issues.append(f"Capacity expansion added {added_lm} lane-miles, but dir_lanes_after ({dir_a_val}) <= dir_lanes_before ({dir_b_val})")

        # Check 6: Explicit description lane count verification (e.g. 'widen from 2 lanes to 4 lanes')
        desc = r.get('Project Description', '').lower()
        m_widen = re.search(r"widen .*?from (\d+) lanes? to (\d+) lanes?", desc)
        if m_widen:
            exp_b = float(m_widen.group(1))
            exp_a = float(m_widen.group(2))
            if tot_b_val > 0 and tot_b_val != exp_b:
                issues.append(f"Description states {exp_b} lanes before, but total_lanes_before is {tot_b_val}")
            if tot_a_val > 0 and tot_a_val != exp_a:
                issues.append(f"Description states {exp_a} lanes after, but total_lanes_after is {tot_a_val}")
                
        audit_records.append({
            'row': idx,
            'project_id': pid,
            'route': rte,
            'dir': direction,
            'county': co,
            'gp_b': gp_b,
            'hov_b': hov_b,
            'hot_b': hot_b,
            'oth_b': oth_b,
            'sum_b': sum_b,
            'dir_b': dir_b_val,
            's1_b_str': s1_b_str,
            'gp_a': gp_a,
            'hov_a': hov_a,
            'hot_a': hot_a,
            'oth_a': oth_a,
            'sum_a': sum_a,
            'dir_a': dir_a_val,
            's1_a_str': s1_a_str,
            'tot_b': tot_b_val,
            'tot_a': tot_a_val,
            'added_lm': added_lm,
            'issues_count': len(issues),
            'issues': " | ".join(issues)
        })
        
    audit_df = pd.DataFrame(audit_records)
    out_path = os.path.join(WORKSPACE_PATH, "staging/lane_validation_detailed.csv")
    audit_df.to_csv(out_path, index=False)
    print(f"Saved detailed lane validation to {out_path}")
    
    print("\n================ LANE VALIDATION SUMMARY ================")
    print(f"Total projects evaluated: {len(audit_df)}")
    rows_with_issues = audit_df[audit_df['issues_count'] > 0]
    print(f"Rows with potential discrepancies: {len(rows_with_issues)} / {len(audit_df)}")
    
    for idx, r in rows_with_issues.iterrows():
        print(f"\nRow {r['row']} | PID {r['project_id']} | Route {r['route']} {r['dir']} ({r['county']}):")
        for iss in r['issues'].split(' | '):
            print(f"   - {iss}")

if __name__ == '__main__':
    validate_lanes()
