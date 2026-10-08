import zipfile
import xml.etree.ElementTree as ET
import csv
import os
import datetime

EXCEL_FILE = "/Users/erb/VAULT_PHONE/Research/CERCET/California/python/ag.workspace/staging/Master_CA_Project_List.xlsx"
OUT_CSV = "/Users/erb/VAULT_PHONE/Research/CERCET/California/python/ag.workspace/staging/master_sheet3_raw.csv"

def extract_sheet3():
    with zipfile.ZipFile(EXCEL_FILE) as z:
        # shared strings
        ss_tree = ET.fromstring(z.read('xl/sharedStrings.xml')) if 'xl/sharedStrings.xml' in z.namelist() else None
        shared_strings = []
        if ss_tree is not None:
            for si in ss_tree:
                t = ''.join([elem.text or '' for elem in si.iter() if elem.tag.endswith('}t')])
                shared_strings.append(t)
        
        # sheet1 is Sheet3 in workbook (as determined earlier)
        ws_tree = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        rows_elem = ws_tree.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheetData')
        
        all_rows = []
        for r in rows_elem:
            row_idx = int(r.attrib['r'])
            # We know data rows stop around row 117
            if row_idx > 120:
                continue
            row_dict = {}
            for c in r:
                cell_ref = c.attrib['r']
                col = ''.join([ch for ch in cell_ref if ch.isalpha()])
                t = c.attrib.get('t')
                v = c.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                val = v.text if (v is not None and v.text is not None) else ''
                if t == 's' and val:
                    val = shared_strings[int(val)]
                row_dict[col] = val
            all_rows.append((row_idx, row_dict))
            
    header_idx, header_dict = all_rows[0]
    # sort columns by Excel column order
    def col_to_int(col_str):
        num = 0
        for ch in col_str:
            num = num * 26 + (ord(ch) - ord('A') + 1)
        return num
    
    sorted_cols = sorted(header_dict.keys(), key=col_to_int)
    headers = [header_dict[c] for c in sorted_cols]
    
    # write to CSV
    with open(OUT_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row_idx, r_dict in all_rows[1:]:
            # check if row has any non-empty cells
            row_vals = [r_dict.get(c, '') for c in sorted_cols]
            if any(val.strip() for val in row_vals):
                writer.writerow(row_vals)
                
    print(f"Extracted {len(all_rows)-1} rows to {OUT_CSV}")

if __name__ == '__main__':
    extract_sheet3()
