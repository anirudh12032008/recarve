/* Scans notes.py, which is where every class name in this app lives -- the
   markup is Python strings, so there is nothing else to scan. */
module.exports = {
  content: ['../notes.py'],
  darkMode: 'media',        // the app follows the phone, and always has
  theme: {
    extend: {
      /* Every colour stays a CSS variable behind its token. The light/dark
         swap already lives in :root/@media, and --h is set from JS per subject
         at runtime -- Tailwind cannot know a subject's hue at build time. */
      colors: {
        bg:      'var(--bg)',
        surface: 'var(--surface)',
        fg:      'var(--fg)',
        mut:     'var(--mut)',
        line:    'var(--line)',
        accent:  { DEFAULT: 'var(--accent)', fg: 'var(--accent-fg)' },
        admin:   { DEFAULT: 'var(--admin)', fg: 'var(--admin-fg)' },
        warn:    { DEFAULT: 'var(--warn)', bg: 'var(--warn-bg)' },
        err:     'var(--err)',
        hue:            'hsl(var(--h) var(--sat) var(--lum))',
        'hue-chip':     'hsl(var(--h) var(--sat) var(--chip-lum))',
        'hue-chip-ink': 'hsl(var(--h) var(--sat) var(--chip-text))',
      },
      fontSize: {
        // line-heights included: a size is never chosen without one.
        micro: ['11px', { lineHeight: '1.3', letterSpacing: '.04em' }],
        small: ['13px', { lineHeight: '1.45' }],
        base:  ['16px', { lineHeight: '1.65' }],
        head:  ['20px', { lineHeight: '1.3', letterSpacing: '-.015em' }],
        big:   ['26px', { lineHeight: '1.2', letterSpacing: '-.022em' }],
      },
      fontWeight: { normal: '400', medium: '500', semibold: '600', bold: '700' },
      spacing: {
        // 4px grid. 'tap' is the floor for anything a thumb lands on.
        tap:  'var(--tap)',   // 44px
        hang: 'var(--hang)',  // 31px, where a row's words begin
      },
      borderRadius: {
        chip: '7px',     // code, tag, badge, flag
        DEFAULT: '11px', // every control and every row
        card: '14px',    // .blank, .mine, .cal, .job, #lock, #rec
        sheet: '18px',   // #sheet .card, #panel -- the top corners of a sheet
      },
      maxWidth: { read: '70ch' },
    },
  },
};
