/* Scans notes.py, which is where every class name in this app lives -- the
   markup is Python strings, so there is nothing else to scan. */
module.exports = {
  content: ['../notes.py'],
  darkMode: 'media',        // the app follows the phone, and always has
  theme: { extend: {} },
};
