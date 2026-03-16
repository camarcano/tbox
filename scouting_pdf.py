#!/usr/bin/env python3
"""
PDF generation for scouting reports.

Produces a 2-page landscape PDF:
  Page 1: VS ALL PITCHERS  (full detail)
  Page 2: VS RHP (top) + VS LHP (bottom)
"""

from io import BytesIO

from fpdf import FPDF

from scouting_report import generate_scouting_report

# ---------------------------------------------------------------------------
# Color constants (R, G, B)
# ---------------------------------------------------------------------------

PURPLE = (102, 126, 234)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
LGRAY = (245, 245, 245)
MGRAY = (200, 200, 200)
DGRAY = (100, 100, 100)

ZONE_COLORS = {
    'hot':  (255, 107, 107),
    'warm': (255, 169, 77),
    'mid':  (255, 224, 102),
    'cool': (160, 216, 239),
    'cold': (116, 185, 255),
    'none': (230, 230, 230),
}


def _ba_color(ba):
    if ba is None:
        return ZONE_COLORS['none']
    if ba >= 0.350:
        return ZONE_COLORS['hot']
    if ba >= 0.300:
        return ZONE_COLORS['warm']
    if ba >= 0.250:
        return ZONE_COLORS['mid']
    if ba >= 0.200:
        return ZONE_COLORS['cool']
    return ZONE_COLORS['cold']


def _ba_text_white(ba):
    if ba is None:
        return False
    return ba >= 0.300 or ba < 0.200


def _fmt(val, fmt_type='ba'):
    if val is None:
        return '-'
    if fmt_type == 'ba':
        if val >= 1.0:
            return '1.000'
        return f'.{int(round(val * 1000)):03d}'
    if fmt_type == 'pct':
        return f'{val}%'
    return str(val)


# ---------------------------------------------------------------------------
# PDF class
# ---------------------------------------------------------------------------

class ScoutingPDF(FPDF):

    def __init__(self):
        super().__init__(orientation='L', unit='mm', format='Letter')
        self.set_auto_page_break(auto=False)
        self.set_margins(8, 8, 8)

    # -- Header bar -----------------------------------------------------------

    def draw_header(self, player, split_label, season):
        self.set_fill_color(*PURPLE)
        self.set_text_color(*WHITE)
        self.set_font('Helvetica', 'B', 13)
        self.cell(0, 9,
                  f'  {player["name"]}     {split_label}     Season {season}',
                  fill=True)
        self.ln(9)
        self.set_font('Helvetica', '', 7)
        self.cell(0, 4,
                  f'  Bats: {player["bats"]}  |  MLBAMID: {player["mlbamid"]}',
                  fill=True)
        self.ln(5)
        self.set_text_color(*BLACK)

    # -- Summary stats bar ----------------------------------------------------

    def draw_summary(self, s):
        self.set_font('Helvetica', '', 7)
        self.set_fill_color(*LGRAY)
        items = [
            ('Pitches', f'{s["pitches_charted"]:,}'),
            ('PA', str(s['pa'])),
            ('BA', _fmt(s['ba'])),
            ('SLG', _fmt(s['slg'])),
            ('K%', _fmt(s['k_pct'], 'pct')),
            ('BB%', _fmt(s['bb_pct'], 'pct')),
            ('GB%', _fmt(s['gb_pct'], 'pct')),
            ('FB%', _fmt(s['fb_pct'], 'pct')),
        ]
        w = (self.w - 16) / len(items)
        for label, val in items:
            self.set_font('Helvetica', 'B', 8)
            self.cell(w * 0.55, 6, val, align='R', fill=True)
            self.set_font('Helvetica', '', 6.5)
            self.cell(w * 0.45, 6, f' {label}', align='L', fill=True)
        self.ln(7)

    # -- Zone chart -----------------------------------------------------------

    def draw_zone_chart(self, zone_data, x, y, title, cw=13, ch=11):
        """Draw a 3x3 colored zone grid."""
        zones = zone_data.get('zones', {})
        if not zones:
            self.set_xy(x, y)
            self.set_font('Helvetica', 'I', 7)
            self.cell(cw * 3, ch * 3, 'No data', align='C')
            return y + 5 + ch * 3

        self.set_xy(x, y)
        self.set_font('Helvetica', 'B', 7)
        self.set_text_color(*BLACK)
        self.cell(cw * 3, 5, title, align='C')
        cy = y + 5

        for row_keys in [('1', '2', '3'), ('4', '5', '6'), ('7', '8', '9')]:
            for i, zk in enumerate(row_keys):
                z = zones.get(zk, {})
                ba = z.get('ba')
                hits = z.get('hits', 0)
                ab = z.get('ab', 0)

                color = _ba_color(ba)
                self.set_fill_color(*color)
                cx = x + i * cw

                # Filled rect + border
                self.rect(cx, cy, cw, ch, 'DF')
                self.set_draw_color(*MGRAY)
                self.rect(cx, cy, cw, ch, 'D')

                # Text color
                if _ba_text_white(ba):
                    self.set_text_color(*WHITE)
                else:
                    self.set_text_color(*BLACK)

                # BA
                self.set_font('Helvetica', 'B', 8)
                self.set_xy(cx, cy + 1)
                self.cell(cw, 4, _fmt(ba), align='C')

                # H/AB
                self.set_font('Helvetica', '', 5)
                self.set_xy(cx, cy + 5.5)
                self.cell(cw, 3, f'{hits}/{ab}', align='C')

            cy += ch

        self.set_text_color(*BLACK)
        self.set_draw_color(*BLACK)
        return cy + 2

    def draw_zones_row(self, zone_fb, zone_other):
        y = self.get_y() + 1
        left = self.l_margin
        right = left + 13 * 3 + 8
        bottom_fb = self.draw_zone_chart(zone_fb, left, y, 'vs. Fastballs')
        bottom_ot = self.draw_zone_chart(zone_other, right, y, 'vs. Other Pitches')
        return max(bottom_fb, bottom_ot)

    # -- Pitch type table -----------------------------------------------------

    def draw_pitch_table(self, rows, x_start=None, max_w=None):
        if x_start is None:
            x_start = self.l_margin
        if max_w is None:
            max_w = self.w - self.l_margin - self.r_margin

        if not rows:
            self.set_font('Helvetica', 'I', 7)
            self.cell(max_w, 6, 'No pitch type data', align='C')
            self.ln(7)
            return self.get_y()

        headers = ['Pitch Type', 'All', '1st Pitch', 'Early',
                   '2 Strikes', 'Ahead', 'Behind', 'RISP',
                   'Chase%', 'Take%']
        base_w = [25, 22, 22, 20, 22, 20, 20, 20, 17, 17]
        scale = min(max_w / sum(base_w), 1.0)
        cw = [w * scale for w in base_w]
        rh = 5

        y = self.get_y()

        # Header
        self.set_fill_color(*PURPLE)
        self.set_text_color(*WHITE)
        self.set_font('Helvetica', 'B', 6)
        x = x_start
        for i, h in enumerate(headers):
            self.set_xy(x, y)
            self.cell(cw[i], rh, h, border=1, fill=True, align='C')
            x += cw[i]
        y += rh

        self.set_text_color(*BLACK)

        sit_keys = ['all_counts', 'first_pitch', 'early_counts',
                     'two_strikes', 'hitter_ahead', 'hitter_behind',
                     'with_risp']

        for ri, r in enumerate(rows):
            bg = LGRAY if ri % 2 == 0 else WHITE
            self.set_fill_color(*bg)
            x = x_start

            self.set_xy(x, y)
            self.set_font('Helvetica', 'B', 6)
            self.cell(cw[0], rh, r['pitch_type'], border=1, fill=True)
            x += cw[0]

            self.set_font('Helvetica', '', 5.5)
            for si, sk in enumerate(sit_keys):
                s = r.get(sk, {})
                ba = s.get('ba')
                text = _fmt(ba) if ba is not None else '-'
                hab = f' {s.get("hits", 0)}/{s.get("ab", 0)}'
                self.set_xy(x, y)
                self.cell(cw[si + 1], rh, text + hab, border=1,
                          fill=True, align='C')
                x += cw[si + 1]

            for pk in ['chase_pct', 'take_pct']:
                v = r.get(pk)
                self.set_xy(x, y)
                self.cell(cw[8 if pk == 'chase_pct' else 9], rh,
                          _fmt(v, 'pct'), border=1, fill=True, align='C')
                x += cw[8 if pk == 'chase_pct' else 9]

            y += rh

        self.set_y(y)
        return y

    # -- By-count table -------------------------------------------------------

    def draw_by_count(self, data):
        if not data:
            return self.get_y()

        count_keys = ['0-0', '0-1', '0-2', '1-0', '1-1', '1-2',
                      '2-0', '2-1', '2-2', '3-0', '3-1', '3-2']
        all_keys = count_keys + ['all']
        headers = [''] + count_keys + ['All']

        total_w = self.w - self.l_margin - self.r_margin
        lw = 19
        ccw = (total_w - lw) / 13
        rh = 4.5

        y = self.get_y()
        xs = self.l_margin

        # Header
        self.set_fill_color(*PURPLE)
        self.set_text_color(*WHITE)
        self.set_font('Helvetica', 'B', 5.5)
        x = xs
        for i, h in enumerate(headers):
            w = lw if i == 0 else ccw
            self.set_xy(x, y)
            self.cell(w, rh, h, border=1, fill=True, align='C')
            x += w
        y += rh
        self.set_text_color(*BLACK)

        row_defs = [
            ('Swing%', lambda d: _fmt(d.get('swing_pct'), 'pct')),
            ('BA vs FB', lambda d: _fmt(d.get('ba_fb'))),
            ('BA vs Oth', lambda d: _fmt(d.get('ba_other'))),
            ('SLG%', lambda d: _fmt(d.get('slg'))),
            ('AB', lambda d: str(d.get('ab', '-'))),
            ('H', lambda d: str(d.get('h', '-'))),
        ]

        for ri, (label, fn) in enumerate(row_defs):
            bg = LGRAY if ri % 2 == 0 else WHITE
            self.set_fill_color(*bg)
            x = xs

            self.set_xy(x, y)
            self.set_font('Helvetica', 'B', 5)
            self.cell(lw, rh, label, border=1, fill=True)
            x += lw

            self.set_font('Helvetica', '', 5)
            for ck in all_keys:
                d = data.get(ck, {})
                self.set_xy(x, y)
                self.cell(ccw, rh, fn(d), border=1, fill=True, align='C')
                x += ccw

            y += rh

        self.set_y(y)
        return y

    # -- Full split section ---------------------------------------------------

    def draw_split(self, report, split_label, season, full=False):
        """Render one split (header + summary + zones + tables)."""
        self.draw_header(report['player'], split_label, season)
        self.draw_summary(report['summary'])

        zones_bottom = self.draw_zones_row(
            report['zone_fb'], report['zone_other'])
        self.set_y(zones_bottom + 1)

        self.set_font('Helvetica', 'B', 7)
        self.cell(0, 4, 'Pitch Type Performance & Tendencies')
        self.ln(4)
        self.draw_pitch_table(report['pitch_type_table'])

        if full:
            self.ln(2)
            self.set_font('Helvetica', 'B', 7)
            self.cell(0, 4, 'By Count')
            self.ln(4)
            self.draw_by_count(report['by_count'])

    def draw_no_data(self, split_label, season, player):
        self.draw_header(player, split_label, season)
        self.set_font('Helvetica', 'I', 9)
        self.set_text_color(*DGRAY)
        self.cell(0, 10, f'  No data available for {split_label}')
        self.ln(12)
        self.set_text_color(*BLACK)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_scouting_pdf(season, batter_id,
                           start_date=None, end_date=None):
    """Generate a 2-page PDF with VS ALL, VS RHP, VS LHP."""

    report_all = generate_scouting_report(
        season, batter_id, 'ALL', start_date, end_date)

    try:
        report_rhp = generate_scouting_report(
            season, batter_id, 'R', start_date, end_date)
    except (ValueError, FileNotFoundError):
        report_rhp = None

    try:
        report_lhp = generate_scouting_report(
            season, batter_id, 'L', start_date, end_date)
    except (ValueError, FileNotFoundError):
        report_lhp = None

    player = report_all['player']
    pdf = ScoutingPDF()

    # Page 1: VS ALL PITCHERS (full detail with by-count)
    pdf.add_page()
    pdf.draw_split(report_all, 'VS ALL PITCHERS', season, full=True)

    # Page 2: VS RHP + VS LHP (compact, no by-count)
    pdf.add_page()

    if report_rhp:
        pdf.draw_split(report_rhp, 'VS RHP', season, full=False)
    else:
        pdf.draw_no_data('VS RHP', season, player)

    pdf.ln(3)

    if report_lhp:
        pdf.draw_split(report_lhp, 'VS LHP', season, full=False)
    else:
        pdf.draw_no_data('VS LHP', season, player)

    buf = BytesIO()
    buf.write(pdf.output())
    buf.seek(0)
    return buf
