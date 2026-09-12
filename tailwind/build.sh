#!/bin/bash
# Build the stylesheet and drop it where notes.py reads it from.
#
# Tailwind proper, compiled -- not the play CDN, which ships ~300KB of
# JavaScript that has to run before anything paints. This writes a plain
# stylesheet of only the classes notes.py actually uses, which notes.py inlines
# into the page it serves. Nothing is fetched at runtime and the app still
# works with no network.
#
# Run this after changing any class name in notes.py, and commit web/app.css.
set -e
cd "$(dirname "$0")"
[ -d node_modules ] || npm install
./node_modules/.bin/tailwindcss -c tailwind.config.js -i input.css -o ../web/app.css --minify
echo "web/app.css: $(wc -c < ../web/app.css) bytes"
