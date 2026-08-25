from docx.oxml import OxmlElement
from copy import deepcopy


def add_column_to_table(table, header_text="Комментарий"):
    tbl = table._tbl
    ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    tbl_grid = tbl.find(f'{ns}tblGrid')
    if tbl_grid is not None:
        grid_cols = tbl_grid.findall(f'{ns}gridCol')
        if grid_cols:
            tbl_grid.append(deepcopy(grid_cols[-1]))
        else:
            tbl_grid.append(OxmlElement('w:gridCol'))
    rows = tbl.findall(f'{ns}tr')
    for row_idx, tr in enumerate(rows):
        tcs = tr.findall(f'{ns}tc')
        if tcs:
            new_tc = deepcopy(tcs[-1])
            tc_pr = new_tc.find(f'{ns}tcPr')
            if tc_pr is not None:
                for tag in ['gridSpan', 'vMerge']:
                    elem = tc_pr.find(f'{ns}{tag}')
                    if elem is not None:
                        tc_pr.remove(elem)
            for p in list(new_tc.findall(f'{ns}p')):
                new_tc.remove(p)
        else:
            new_tc = OxmlElement('w:tc')
        new_p = OxmlElement('w:p')
        new_tc.append(new_p)
        if row_idx == 0:
            new_r = OxmlElement('w:r')
            new_t = OxmlElement('w:t')
            new_t.text = header_text
            new_t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            new_r.append(new_t)
            new_p.append(new_r)
        tr.append(new_tc)
