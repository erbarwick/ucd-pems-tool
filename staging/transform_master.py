import os
import sys
import re
import datetime
import pandas as pd
import numpy as np

WORKSPACE_PATH = "/Users/erb/VAULT_PHONE/Research/CERCET/California/python/ag.workspace"
RAW_CSV = os.path.join(WORKSPACE_PATH, "staging/master_sheet3_raw.csv")
CLEAN_CSV = os.path.join(WORKSPACE_PATH, "staging/Master_CA_Project_List_Cleaned.csv")
CLEAN_XLSX = os.path.join(WORKSPACE_PATH, "staging/Master_CA_Project_List_Cleaned.xlsx")

def parse_excel_date(val):
    if not val:
        return "", None, None, None
    s = str(val).strip()
    if s.isdigit() and len(s) == 5:
        d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(s))
        return d.strftime("%Y-%m-%d"), d.year, d.month, d.day
    
    # Check for text formats
    # e.g. "April 30th, 2018" or "Feb 23rd, 2018"
    m_full = re.search(r"([A-Za-z]+)\s*(\d{1,2})?(?:st|nd|rd|th)?,?\s*(\d{4})", s)
    if m_full:
        mo_str, dy_str, yr_str = m_full.group(1), m_full.group(2), m_full.group(3)
        mo_dict = {'jan':1, 'feb':2, 'mar':3, 'apr':4, 'may':5, 'jun':6, 'jul':7, 'aug':8, 'sep':9, 'oct':10, 'nov':11, 'dec':12}
        mo_num = None
        for k, v in mo_dict.items():
            if k in mo_str.lower():
                mo_num = v
                break
        yr_num = int(yr_str)
        dy_num = int(dy_str) if dy_str else None
        iso = f"{yr_num:04d}-{mo_num:02d}-{dy_num:02d}" if (mo_num and dy_num) else (f"{yr_num:04d}-{mo_num:02d}" if mo_num else f"{yr_num:04d}")
        return iso, yr_num, mo_num, dy_num
    
    # Check "2021/03"
    m_slash = re.search(r"(\d{4})[/-](\d{1,2})", s)
    if m_slash:
        yr = int(m_slash.group(1))
        mo = int(m_slash.group(2))
        return f"{yr:04d}-{mo:02d}", yr, mo, None

    # Check year only "2019" or "around 2014" or "Spring 2020"
    m_yr = re.search(r"(19\d\d|20\d\d)", s)
    if m_yr:
        yr = int(m_yr.group(1))
        mo = None
        if 'spring' in s.lower(): mo = 3
        elif 'summer' in s.lower(): mo = 6
        elif 'fall' in s.lower() or 'autumn' in s.lower(): mo = 9
        elif 'winter' in s.lower(): mo = 12
        return (f"{yr:04d}-{mo:02d}" if mo else f"{yr:04d}"), yr, mo, None

    return s, None, None, None

def parse_num(val):
    if not val: return 0.0
    val_str = str(val).strip().replace(',', '')
    m = re.search(r"[-+]?\d*\.?\d+", val_str)
    if m:
        try:
            return float(m.group(0))
        except:
            return 0.0
    return 0.0

def clean_dataset():
    df = pd.read_csv(RAW_CSV, dtype=str).fillna("")
    print(f"Read {len(df)} rows from {RAW_CSV}")
    
    cleaned_rows = []
    
    for idx, r in df.iterrows():
        row = r.to_dict()
        pid = str(row.get('Project ID', '')).strip()
        route = str(row.get('Route', '')).strip()
        dir_val = str(row.get('Direction', '')).strip().replace('\xa0', '').strip()
        
        # 1. Multi-Direction Row Splitting (CHECK FIRST BEFORE DIR NORMALIZATION)
        if pid == '300000061' and (',' in dir_val or 'NB, SB' in dir_val):
            # Split into NB and SB
            row_nb = dict(row)
            row_nb['Direction'] = 'NB'
            row_nb['New Gen Pur-pose Lanes (Lane Miles)'] = '0.5'
            row_nb['New or Extend L/R Turn Pockets (Lane Miles)'] = '0.05'
            row_nb['Total Added Lanes (Lane Miles)'] = '0.55'
            row_nb['Notes'] = str(row.get('Notes', '')) + " [Split from bidirectional row: NB portion]"
            
            row_sb = dict(row)
            row_sb['Direction'] = 'SB'
            row_sb['New Gen Pur-pose Lanes (Lane Miles)'] = '0.5'
            row_sb['New or Extend L/R Turn Pockets (Lane Miles)'] = '0.05'
            row_sb['Total Added Lanes (Lane Miles)'] = '0.55'
            row_sb['Notes'] = str(row.get('Notes', '')) + " [Split from bidirectional row: SB portion]"
            
            cleaned_rows.append(row_nb)
            cleaned_rows.append(row_sb)
            continue
            
        elif pid == '700021119' and (',' in dir_val or 'NB, SB' in dir_val):
            # Split into NB and SB (2.2 mi each of HOV)
            row_nb = dict(row)
            row_nb['Direction'] = 'NB'
            row_nb['New HOV Lanes (Lane Miles)'] = '2.2'
            row_nb['Total Added Lanes (Lane Miles)'] = '2.2'
            row_nb['Sensor Station 1 ID '] = '716978'
            row_nb['Sensor Station 2 ID '] = '716981'
            row_nb['Sensor Station 3 ID '] = '716985'
            row_nb['Notes'] = str(row.get('Notes', '')) + " [Split from bidirectional row: NB portion]"
            
            row_sb = dict(row)
            row_sb['Direction'] = 'SB'
            row_sb['New HOV Lanes (Lane Miles)'] = '2.2'
            row_sb['Total Added Lanes (Lane Miles)'] = '2.2'
            row_sb['Sensor Station 1 ID '] = '777765'
            row_sb['Sensor Station 2 ID '] = '777745'
            row_sb['Sensor Station 3 ID '] = ''
            row_sb['Notes'] = str(row.get('Notes', '')) + " [Split from bidirectional row: SB portion using stations 777765, 777745]"
            
            cleaned_rows.append(row_nb)
            cleaned_rows.append(row_sb)
            continue

        # 2. PeMS Compass Convention Standardization
        if route == '70':
            if dir_val == 'NB': dir_val = 'EB'
            elif dir_val == 'SB': dir_val = 'WB'
        elif route == '84':
            if dir_val == 'EB': dir_val = 'NB'
            elif dir_val == 'WB': dir_val = 'SB'
        elif route == '46':
            if dir_val == 'NB': dir_val = 'EB'
            elif dir_val == 'SB': dir_val = 'WB'
            
        # Clean direction whitespace
        if dir_val.startswith('E'): dir_val = 'EB' if len(dir_val)>1 else 'E'
        elif dir_val.startswith('W'): dir_val = 'WB' if len(dir_val)>1 else 'W'
        elif dir_val.startswith('N'): dir_val = 'NB' if len(dir_val)>1 else 'N'
        elif dir_val.startswith('S'): dir_val = 'SB' if len(dir_val)>1 else 'S'
        
        # Handle rows with missing direction
        if not dir_val:
            if pid == '700000476': dir_val = 'NB'
            elif pid == '800000018': dir_val = 'NB'
            elif pid == '800000301': dir_val = 'NB'
            elif pid == '800000393': dir_val = 'NB'
            elif pid == '818000194': dir_val = 'EB'
            
        row['Direction'] = dir_val
        cleaned_rows.append(row)
        
    print(f"Total rows after splitting multi-direction projects: {len(cleaned_rows)}")
    
    # 3. Process Dates, Lanes, Sensors, Area Types on all rows
    area_type_map = {
        '200000288': 'rural',
        '218000043': 'rural',
        '300000061': 'suburban',
        '300020420': 'rural',
        '320000087': 'rural',
        '700000476': 'suburban',
        '700000514': 'urban',
        '700020076': 'urban',
        '700021119': 'urban',
        '800000018': 'suburban',
        '800000116': 'suburban',
        '800000301': 'suburban',
        '800000393': 'suburban',
        '813000220': 'suburban',
        '818000194': 'suburban',
        '1014000158': 'urban',
        '1014000167': 'rural',
        '1100020489': 'suburban',
        '1112000004': 'suburban',
        '1114000049': 'suburban',
        '1116000131': 'suburban',
        '1117000088': 'suburban',
        '1118000199': 'suburban',
        '1200000085': 'urban',
        '1200020277': 'suburban',
        '1200020279': 'suburban',
    }
    
    final_rows = []
    for r in cleaned_rows:
        pid = str(r.get('Project ID', '')).strip()
        
        # --- DATES ---
        # Construction Begin
        c_raw = r.get('Construction Begin Date', '').strip()
        c_iso, c_yr, c_mo, c_dy = parse_excel_date(c_raw)
        
        # Reconcile user-specified exceptions
        if pid == '313000020':
            c_iso, c_yr, c_mo, c_dy = "2020-03-01", 2020, 3, 1
        elif pid == '317000247':
            c_iso, c_yr, c_mo, c_dy = "2018-04-30", 2018, 4, 30
        elif pid == '319000051':
            c_iso, c_yr, c_mo, c_dy = "2019-12-19", 2019, 12, 19
            
        r['Construction Begin Date'] = c_raw
        r['Begin Date (formatted YYYY-MM-DD)'] = c_iso
        if c_yr: r['construction_begin_year'] = str(c_yr)
        if c_mo: r['construction_begin_month'] = str(c_mo)
        if c_dy: r['construction_begin_day'] = str(c_dy)
        
        # Open to Traffic
        o_raw = r.get('Open to Traffic Date', '').strip()
        o_iso, o_yr, o_mo, o_dy = parse_excel_date(o_raw)
        r['Open to Traffic Date'] = o_raw
        r['Open to Traffic Date (formatted YYYY-MM-DD)'] = o_iso
        if o_yr: r['open_traffic_year'] = str(o_yr)
        if o_mo: r['open_traffic_month'] = str(o_mo)
        if o_dy: r['open_traffic_day'] = str(o_dy)
        
        # --- LANES BEFORE / AFTER ---
        gp_b = parse_num(r.get('Number of GP Lanes Before Expansion', '0'))
        hov_b = parse_num(r.get('Number of HOV Lanes Before Expansion', '0'))
        hot_b = parse_num(r.get('Number of HOT Lanes Before Expansion', '0'))
        oth_b = parse_num(r.get('Number of other lane before (specific)', '0'))
        dir_b = int(round(gp_b + hov_b + hot_b + oth_b))
        
        gp_a = parse_num(r.get('Number of GP Lanes After Expansion', '0'))
        hov_a = parse_num(r.get('Number of HOV Lanes After Expansion', '0'))
        hot_a = parse_num(r.get('Number of HOT Lanes After Expansion', '0'))
        oth_a = parse_num(r.get('Number of other lane after (specific)', '0'))
        dir_a = int(round(gp_a + hov_a + hot_a + oth_a))
        
        if dir_b > 0:
            r['dir_lanes_before'] = str(dir_b)
            r['total_lanes_before'] = str(dir_b * 2)
            
        if dir_a > 0:
            r['dir_lanes_after'] = str(dir_a)
            r['total_lanes_after'] = str(dir_a * 2)
        elif dir_b > 0:
            # Derive after lanes if lane additions are present
            added_lanes = parse_num(r.get('Total Added Lanes (Lane Miles)', '0'))
            if added_lanes > 0:
                # Typically 1 lane added per direction
                derived_a = dir_b + 1
                r['dir_lanes_after'] = str(derived_a)
                r['total_lanes_after'] = str(derived_a * 2)

        # Clean PM prefixes & float precision
        def clean_pm_str(val):
            if not val: return ""
            s = str(val).strip()
            for pfx_strip in ['SCL', 'I-10S', 'I-10', 'SM']:
                s = s.replace(pfx_strip, '').strip()
            m = re.match(r"^([A-Za-z\s\-]*?)([0-9]+(?:\.[0-9]+)?)([A-Za-z\s\-]*)$", s)
            if m:
                pfx = m.group(1).strip()
                num = float(m.group(2))
                sfx = m.group(3).strip()
                num_str = f"{round(num, 3):.3f}".rstrip('0').rstrip('.')
                return f"{pfx}{num_str}{sfx}"
            return s

        r['Begin PM'] = clean_pm_str(r.get('Begin PM', ''))
        r['End PM'] = clean_pm_str(r.get('End PM', ''))

        for abs_col in ['temp_abs_begin', 'temp_abs_end', 'Absolute Begin PM', 'Absolute End PM']:
            if abs_col in r and r[abs_col]:
                try:
                    num = float(str(r[abs_col]).strip())
                    r[abs_col] = f"{round(num, 3):.3f}".rstrip('0').rstrip('.')
                except ValueError:
                    pass
        
        # Fix Route 46 Kern County Abs PM (PID 612000175)
        if pid == '612000175':
            r['temp_abs_begin'] = '84.4'
            r['temp_abs_end'] = '86.1'

        # Clean sensor fields whitespace/tabs
        for c in list(r.keys()):
            if 'Sensor' in c and isinstance(r[c], str):
                r[c] = r[c].strip()

        # --- SENSORS & HOV SEPARATION ---
        s1 = r.get('Sensor Station 1 ID ', '').strip()
        s1_hov = ""
        s1_name = ""
        s1_type = "VDS"
        
        if '+' in s1 or 'and' in s1 or re.search(r"\d+,\s*\d+", s1):
            # Compound station (ML + HOV)
            # e.g. "717806 (ML) + 775205 (HOV)" or "716072, 762438"
            digits = re.findall(r"\b(\d{6,7})\b", s1)
            if len(digits) >= 2:
                s1 = digits[0]
                s1_hov = digits[1]
                s1_name = "Mainline + HOV dual station"
            elif len(digits) == 1:
                s1 = digits[0]
        elif '(' in s1 and re.search(r"\(\d{5,7}\)", s1):
            # e.g. "I-80E Colfax Curve (314000)"
            m_id = re.search(r"\((\d{5,7})\)", s1)
            if m_id:
                s1_name = re.sub(r"\(\d+\)", "", s1).strip()
                s1 = m_id.group(1)
        elif any(census_term in s1.upper() for census_term in ['ROAD', 'ROADS', 'AVE', 'RTE', 'LANE', 'NORTH', 'SOUTH', 'MODESTO', 'PHELAN', 'WELSH']) or ('(' in s1 and ('.' in s1 or 'R' in s1)) or ('[' in s1):
            # Traffic Census count station (e.g. "WELSH/PALERMO ROADS (9.060)", "MODESTO, BECKWITH ROAD (R20.222)")
            s1_name = s1
            s1 = ""
            s1_type = "Traffic Census"
            
        r['Sensor Station 1 ID '] = s1
        r['Sensor Station 1 HOV ID'] = s1_hov
        r['Sensor Station 1 Name'] = s1_name
        r['Sensor Station 1 Type'] = s1_type
        
        # --- AREA TYPE ---
        curr_area = str(r.get('area_type', '')).strip().lower()
        if not curr_area and pid in area_type_map:
            r['area_type'] = area_type_map[pid]
            
        # Clean binary google earth
        ge = str(r.get('expansion_shown_google_earth', '')).strip().replace('.0', '')
        if not ge:
            ge_text = str(r.get('Lane expansion shown in Google Earth?', '')).lower()
            ge = '1' if 'yes' in ge_text else ('0' if 'no' in ge_text else '')
        r['expansion_shown_google_earth'] = ge
        
        final_rows.append(r)
        
    clean_df = pd.DataFrame(final_rows)
    clean_df.to_csv(CLEAN_CSV, index=False)
    print(f"Saved cleaned CSV to {CLEAN_CSV} with {len(clean_df)} rows and {len(clean_df.columns)} columns")
    
    # Also save to Excel if openpyxl is installed
    try:
        clean_df.to_excel(CLEAN_XLSX, index=False, sheet_name="Master_Projects_Cleaned")
        print(f"Saved cleaned Excel to {CLEAN_XLSX}")
    except Exception as e:
        print(f"Note: Excel export skipped ({e}). Cleaned CSV is saved and available.")

if __name__ == '__main__':
    clean_dataset()
