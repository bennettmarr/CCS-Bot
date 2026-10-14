/**
 * Rebuilds the Raw Data tab from the conference week boxes on Group Stage,
 * then points every formula that reads Raw Data (Sorting) at the new table.
 *
 * Raw Data becomes one tidy table, two rows per game (one per team):
 *   A Week | B ID | C Team | D Wins | E Losses | F Series Result | G Opponent | H Conference
 * C-G keep the same order as before, so the standings formulas work unchanged
 * apart from their ranges, which now cover the whole table.
 *
 * Run: Extensions -> Apps Script -> paste -> choose rebuildRawData -> Run.
 * Safe to re-run any time the Group Stage boxes change.
 */
const GS_TAB = 'Group Stage';
const RAW_TAB = 'Raw Data';
const START_ROW = 108;   // every conference's first "Week" header
const HOME = 0, HOME_SCORE = 19, DASH = 21, AWAY_SCORE = 22, AWAY = 29;  // offsets in a box
const LAST_ROW = 3000;   // how far down the standings formulas look
const WINS_NEEDED = 3;

// Group Stage conference columns, left to right
const CONF_NAMES = [
  'Big 12', 'ACC', 'SEC', 'Big Ten', 'Pac-12', 'Big East', 'American',
  'Mountain West', 'C-USA', 'Sun Belt', 'MAC', 'Big Sky', 'Atlantic 10',
  'Big West', 'CAA', 'Horizon League', 'Summit League', 'Metro', 'Southland',
  'Atlantic Sun + UAC', 'Ohio Valley', 'Missouri Valley', 'America East',
  'Patriot League', 'West Coast',
];

function rebuildRawData() {
  const ss = SpreadsheetApp.getActive();
  const gs = ss.getSheetByName(GS_TAB);
  const raw = ss.getSheetByName(RAW_TAB);
  if (!gs || !raw) throw new Error('Need tabs named "' + GS_TAB + '" and "' + RAW_TAB + '"');

  // ---- 1. read every conference game off Group Stage
  const lastRow = gs.getLastRow(), lastCol = gs.getLastColumn();
  const v = gs.getRange(1, 1, lastRow, lastCol).getDisplayValues();
  const starts = [];
  for (let c = 0; c < lastCol; c++) if (/^Week \d+$/.test(v[START_ROW - 1][c])) starts.push(c);
  if (starts.length !== CONF_NAMES.length) {
    throw new Error('Expected ' + CONF_NAMES.length + ' conference columns on row ' + START_ROW +
                    ', found ' + starts.length + ' - nothing was changed.');
  }

  const out = [];
  starts.forEach((c, k) => {
    let week = '', firstOfWeek = false;
    for (let r = START_ROW - 1; r < lastRow; r++) {
      const cell = (v[r][c] || '').trim();
      const m = cell.match(/^Week (\d+)$/);
      if (m) { week = 'Week ' + m[1]; firstOfWeek = true; continue; }
      if (!cell || !(v[r][c + AWAY] || '').trim() || (v[r][c + DASH] || '').trim() !== '-') continue;
      const row = r + 1, i = out.length + 3;   // sheet row this game's first line lands on
      const ref = col => "='" + GS_TAB + "'!$" + colLetter(c + col + 1) + '$' + row;
      out.push([firstOfWeek ? week : '',
        '=IFERROR(VLOOKUP(C' + i + ', Teams!$F$3:$G$279, 2, false), "")',
        ref(HOME), ref(HOME_SCORE), '=D' + (i + 1),
        '=IFERROR(IFS(D' + i + '=' + WINS_NEEDED + ', "W", D' + (i + 1) + '=' + WINS_NEEDED + ', "L"), "")',
        '=C' + (i + 1), CONF_NAMES[k]]);
      out.push(['',
        '=IFERROR(VLOOKUP(C' + (i + 1) + ', Teams!$F$3:$G$279, 2, false), "")',
        ref(AWAY), ref(AWAY_SCORE), '=D' + i,
        '=IFS(F' + i + '="W", "L", F' + i + '="L", "W", F' + i + '="", "")',
        '=C' + i, CONF_NAMES[k]]);
      firstOfWeek = false;
    }
  });

  // ---- 2. remember where the old blocks were, so references can be moved
  const header = raw.getRange(2, 1, 1, raw.getLastColumn()).getValues()[0];
  const oldStarts = [];
  header.forEach((h, idx) => { if (String(h).trim() === 'Team') oldStarts.push(idx - 2); });

  // ---- 3. repoint every formula elsewhere that reads Raw Data
  let moved = 0;
  ss.getSheets().forEach(sh => {
    if (sh.getName() === RAW_TAB) return;
    const rng = sh.getDataRange();
    const f = rng.getFormulas();
    for (let r = 0; r < f.length; r++) {
      for (let c = 0; c < f[r].length; c++) {
        const old = f[r][c];
        if (!old || old.indexOf("'" + RAW_TAB + "'!") === -1) continue;
        const neu = old.replace(
          /'Raw Data'!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?/g,
          (all, c1, r1, c2, r2) => {
            const a = newCol(c1, oldStarts);
            if (!c2) return "'" + RAW_TAB + "'!$" + a + '$' + r1;
            return "'" + RAW_TAB + "'!$" + a + '$3:$' + newCol(c2, oldStarts) + '$' + LAST_ROW;
          });
        if (neu !== old) { sh.getRange(r + 1, c + 1).setFormula(neu); moved++; }
      }
    }
  });

  // ---- 4. write the new table
  raw.getRange(1, 1, raw.getMaxRows(), raw.getMaxColumns()).breakApart();
  raw.clear();
  if (raw.getMaxRows() < out.length + 10) raw.insertRowsAfter(raw.getMaxRows(), out.length + 10 - raw.getMaxRows());
  raw.getRange(2, 1, 1, 8).setValues([['Week', 'ID', 'Team', 'Wins', 'Losses', 'Series Result', 'Opponent', 'Conference']])
     .setFontWeight('bold');
  raw.getRange(3, 1, out.length, 8).setValues(out);
  raw.setFrozenRows(2);

  SpreadsheetApp.getActive().toast(
    'Raw Data rebuilt: ' + out.length / 2 + ' games across ' + starts.length +
    ' conferences. ' + moved + ' standings formulas repointed.', 'Done', 10);
}

// column letter in the old layout -> same column in the new table
function newCol(letter, oldStarts) {
  const idx = colIndex(letter);
  let start = 0;
  oldStarts.forEach(s => { if (s <= idx) start = s; });
  return colLetter(idx - start + 1);
}

function colIndex(letter) {   // "A" -> 0
  let n = 0;
  for (let i = 0; i < letter.length; i++) n = n * 26 + (letter.charCodeAt(i) - 64);
  return n - 1;
}

function colLetter(n) {       // 1 -> "A"
  let s = '';
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}

/**
 * Resets the counting formulas on every team row of Sorting, so every
 * conference section works the same way the Big 12's does.
 *
 * A team row is any row whose column B pulls a team name from the Teams tab
 * (=Teams!B...). On those rows only columns C-H are rewritten:
 *   C Series Wins  D Series Losses  E Game Wins  F Game Losses
 *   G Series Diff  H Game Diff
 * Everything else on Sorting (IDs, the sorted copies, the CONCATENATE columns)
 * is left alone.
 *
 * Run: choose fixSortingFormulas -> Run.
 */
function fixSortingFormulas() {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName('Sorting');
  if (!sh) throw new Error('No tab named "Sorting"');
  const last = sh.getLastRow();
  const b = sh.getRange(1, 2, last, 1).getFormulas();
  const R = "'" + RAW_TAB + "'!$C$3:$F$" + LAST_ROW;
  const T = "'" + RAW_TAB + "'!$C$3:$C$" + LAST_ROW;
  let fixed = 0;
  for (let r = 0; r < last; r++) {
    if (!/^=\s*Teams!\$?B\$?\d+\s*$/i.test(b[r][0])) continue;
    const n = r + 1;
    const rows = 'FILTER(' + R + ',' + T + '=$B' + n + ')';
    sh.getRange(n, 3, 1, 6).setFormulas([[
      '=IFERROR(COUNTIF(INDEX(' + rows + ',0,4),"W"),0)',
      '=IFERROR(COUNTIF(INDEX(' + rows + ',0,4),"L"),0)',
      '=IFERROR(SUM(INDEX(' + rows + ',0,2)),0)',
      '=IFERROR(SUM(INDEX(' + rows + ',0,3)),0)',
      '=C' + n + '-D' + n,
      '=E' + n + '-F' + n,
    ]]);
    fixed++;
  }
  ss.toast('Reset the formulas on ' + fixed + ' team rows in Sorting.', 'Done', 10);
}

/**
 * Lines up every standings box on Group Stage with Sorting.
 *
 * In each standings row the team name reads =Sorting!$B$<n>; the Series,
 * Games and GD cells in that same row must read row <n> of Sorting too.
 * Some boxes were copied with the wrong rows (all pointing at row 437, say).
 * This sets every =Sorting!$X$<m> cell in a standings row to row <n>.
 *
 * Run: choose fixStandingsLinks -> Run. It reports how many cells it changed.
 */
function fixStandingsLinks() {
  const ss = SpreadsheetApp.getActive();
  const gs = ss.getSheetByName(GS_TAB);
  const f = gs.getRange(1, 1, Math.min(gs.getLastRow(), START_ROW - 1), gs.getLastColumn()).getFormulas();
  let changed = 0, rows = 0;
  const lastPattern = {};   // name column -> {offset: Sorting column} from a good row
  for (let r = 0; r < f.length; r++) {
    for (let c = 0; c < f[r].length; c++) {
      const m = /^=Sorting!\$B\$(\d+)$/.exec(f[r][c]);
      if (!m) continue;
      rows++;
      const n = m[1];
      const pattern = {};
      // the rest of this standings box's row: up to the next team-name cell
      for (let k = c + 1; k < f[r].length; k++) {
        if (/^=Sorting!\$B\$\d+$/.test(f[r][k])) break;
        const s = /^=Sorting!\$([A-Z]+)\$(\d+)$/.exec(f[r][k]);
        if (s) {
          pattern[k - c] = s[1];
          if (s[2] !== n) {
            gs.getRange(r + 1, k + 1).setFormula('=Sorting!$' + s[1] + '$' + n);
            changed++;
          }
        } else if (/#REF!/.test(f[r][k]) && lastPattern[c] && lastPattern[c][k - c]) {
          // a broken link: rebuild it from the same column of the row above
          gs.getRange(r + 1, k + 1).setFormula('=Sorting!$' + lastPattern[c][k - c] + '$' + n);
          pattern[k - c] = lastPattern[c][k - c];
          changed++;
        }
      }
      if (Object.keys(pattern).length) lastPattern[c] = pattern;
    }
  }
  ss.toast('Checked ' + rows + ' standings rows, fixed ' + changed + ' cells.', 'Done', 10);
}

/**
 * Read-only check of the whole Group Stage. Writes what it finds to a tab
 * called "Audit" (created if missing) and changes nothing else.
 *
 * Per conference it checks:
 *  - the standings box lists each team once, and only that conference's teams
 *  - every team in the week boxes is in the standings box, and vice versa
 *  - every team plays the same number of games
 *  - nobody plays twice in a game slot, nobody plays itself
 *  - weeks run in order with no gaps, ending at week 16
 *  - each standings row reads one Sorting row (no crossed links)
 * And across the sheet:
 *  - no team is in two conferences
 *  - Raw Data has exactly two rows per game, each matching its box
 *  - Sorting has a counted row for every team
 *
 * Run: choose auditGroupStage -> Run, then open the Audit tab.
 */
function auditGroupStage() {
  const ss = SpreadsheetApp.getActive();
  const gs = ss.getSheetByName(GS_TAB), raw = ss.getSheetByName(RAW_TAB), so = ss.getSheetByName('Sorting');
  const lastRow = gs.getLastRow(), lastCol = gs.getLastColumn();
  const v = gs.getRange(1, 1, lastRow, lastCol).getDisplayValues();
  const f = gs.getRange(1, 1, START_ROW - 1, lastCol).getFormulas();
  const issues = [], summary = [];
  const say = (where, what) => issues.push([where, what]);

  const starts = [];
  for (let c = 0; c < lastCol; c++) if (/^Week \d+$/.test(v[START_ROW - 1][c])) starts.push(c);
  if (starts.length !== CONF_NAMES.length) say('Group Stage', 'found ' + starts.length + ' conference columns, expected ' + CONF_NAMES.length);

  const confOf = {};
  let totalGames = 0;
  starts.forEach((c, k) => {
    const name = CONF_NAMES[k] || ('column ' + colLetter(c + 1));
    const end = k + 1 < starts.length ? starts[k + 1] : lastCol;

    // standings box: name cells above the week boxes in this column band
    const standing = [];
    for (let r = 0; r < START_ROW - 1; r++) {
      for (let cc = c; cc < end; cc++) {
        const m = /^=Sorting!\$B\$(\d+)$/.exec(f[r][cc] || '');
        if (!m) continue;
        standing.push(v[r][cc].trim());
        for (let k2 = cc + 1; k2 < end; k2++) {
          if (/^=Sorting!\$B\$\d+$/.test(f[r][k2] || '')) break;
          if (/#REF!/.test(f[r][k2] || '') || /^#(REF|N\/A|VALUE|ERROR)/.test(v[r][k2] || ''))
            say(name, 'standings row for ' + v[r][cc] + ' has a broken cell at ' + colLetter(k2 + 1) + (r + 1));
          const s = /^=Sorting!\$[A-Z]+\$(\d+)$/.exec(f[r][k2] || '');
          if (s && s[1] !== m[1]) say(name, 'standings row for ' + v[r][cc] + ' reads Sorting row ' + s[1] + ' instead of ' + m[1] + ' (' + colLetter(k2 + 1) + (r + 1) + ')');
        }
      }
    }
    const inStanding = {};
    standing.forEach(t => {
      if (!t) return;
      if (inStanding[t]) say(name, t + ' is listed twice in the standings');
      inStanding[t] = true;
    });

    // week boxes
    const played = {}, weeks = [];
    let week = null;
    for (let r = START_ROW - 1; r < lastRow; r++) {
      const cell = (v[r][c] || '').trim();
      const m = cell.match(/^Week (\d+)$/);
      if (m) { week = +m[1]; weeks.push(week); continue; }
      if (!cell || (v[r][c + DASH] || '').trim() !== '-') continue;
      const home = cell, away = (v[r][c + AWAY] || '').trim();
      totalGames++;
      if (!away) { say(name, 'week ' + week + ': ' + home + ' has no opponent (row ' + (r + 1) + ')'); continue; }
      if (home === away) say(name, 'week ' + week + ': ' + home + ' plays itself (row ' + (r + 1) + ')');
      [home, away].forEach(t => {
        played[t] = (played[t] || 0) + 1;
        if (confOf[t] && confOf[t] !== name) say(name, t + ' also plays in ' + confOf[t]);
        confOf[t] = name;
        if (standing.length && !inStanding[t]) say(name, t + ' plays in the week boxes but is not in the standings');
      });
    }
    standing.forEach(t => { if (t && !played[t]) say(name, t + ' is in the standings but has no games'); });

    const counts = Object.keys(played).map(t => played[t]);
    if (counts.length && Math.min.apply(null, counts) !== Math.max.apply(null, counts)) {
      Object.keys(played).forEach(t => say(name, t + ' plays ' + played[t] + ' games'));
    }
    for (let i = 1; i < weeks.length; i++) {
      if (weeks[i] !== weeks[i - 1] + 1) say(name, 'weeks jump from ' + weeks[i - 1] + ' to ' + weeks[i]);
    }
    if (weeks.length && weeks[weeks.length - 1] !== 16) say(name, 'last week is ' + weeks[weeks.length - 1] + ', not 16');
    summary.push([name, Object.keys(played).length + ' teams', counts.reduce((a, b) => a + b, 0) / 2 + ' games',
                  'weeks ' + (weeks[0] || '?') + '-' + (weeks[weeks.length - 1] || '?')]);
  });

  // Raw Data: two rows per game, each pair naming the two teams the box does
  if (raw) {
    const rv = raw.getRange(3, 1, Math.max(raw.getLastRow() - 2, 1), 8).getDisplayValues().filter(x => x[2] !== '');
    if (rv.length !== totalGames * 2) say('Raw Data', rv.length + ' rows for ' + totalGames + ' games (expected ' + totalGames * 2 + ')');
    for (let i = 0; i + 1 < rv.length; i += 2) {
      if (rv[i][2] !== rv[i + 1][6] || rv[i + 1][2] !== rv[i][6]) say('Raw Data', 'rows ' + (i + 3) + '-' + (i + 4) + ' are not a matched pair');
      const conf = confOf[rv[i][2]];
      if (conf && rv[i][7] !== conf) say('Raw Data', 'row ' + (i + 3) + ': ' + rv[i][2] + ' labelled ' + rv[i][7] + ', plays in ' + conf);
    }
  } else say('Raw Data', 'tab not found');

  // Sorting: a counted row for every team
  if (so) {
    const sf = so.getRange(1, 2, so.getLastRow(), 2).getFormulas();
    const sv = so.getRange(1, 2, so.getLastRow(), 1).getDisplayValues();
    const counted = {};
    for (let r = 0; r < sf.length; r++) {
      if (/^=\s*Teams!/i.test(sf[r][0]) && /Raw Data/.test(sf[r][1])) counted[sv[r][0].trim()] = true;
    }
    Object.keys(confOf).forEach(t => { if (!counted[t]) say('Sorting', t + ' has no counted row'); });
  } else say('Sorting', 'tab not found');

  // write the report
  let out = ss.getSheetByName('Audit') || ss.insertSheet('Audit');
  out.clear();
  const rows = [['Group Stage audit', new Date().toLocaleString()], ['', ''],
                [issues.length ? issues.length + ' issue(s) found' : 'No issues found', ''], ['', '']];
  issues.forEach(x => rows.push(x));
  rows.push(['', ''], ['Conference', 'Teams / games / weeks']);
  summary.forEach(s => rows.push([s[0], s.slice(1).join(' · ')]));
  out.getRange(1, 1, rows.length, 2).setValues(rows);
  out.getRange('A1').setFontWeight('bold').setFontSize(14);
  out.getRange('A3').setFontWeight('bold').setFontColor(issues.length ? '#c00000' : '#008000');
  out.autoResizeColumns(1, 2);
  ss.setActiveSheet(out);
  ss.toast(issues.length ? issues.length + ' issue(s) - see the Audit tab' : 'All clear', 'Audit', 10);
}
